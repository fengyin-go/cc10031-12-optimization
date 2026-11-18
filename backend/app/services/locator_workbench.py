"""定位工作台服务 — 处理项展开、窗口历史重算与工作台视图。

职责边界：
* ``expand_work_item``：接收处理项承载的定位策略执行记录，按约定数据集展开，
  补全页面结构指纹、候选路径与状态编号。
* ``recompute_window``：重算窗口内定位策略执行历史，解析当前页面可复用的
  选择器边界，决定哪些元素仍需重试（不会重复扫描整组选择器）。
* ``snapshot``：按页面模型输出缓存指纹、失效策略与最近执行记录；
  处理清单与结果列表来自同一份台账，始终一起变化。
* ``retry`` / ``run``：调用 runtime_session 的共用处理路径，不复制判定。

本服务不继承 BaseModuleService：它是工作台聚合层，不是独立业务模块。
"""
from __future__ import annotations

from typing import Any

from app.services.element_selector import ElementSelectorService
from app.services.locator_cache import (
    OUTCOME_CONFLICT,
    OUTCOME_FALLBACK_HIT,
    OUTCOME_HIT,
    PROCESS_STATUS,
    decide_processing,
    locator_cache,
    locator_ledger,
    parse_selector_boundaries,
    summarize_page_model,
)
from app.services.runtime_session import runtime_session
from app.store import store


class LocatorWorkbenchService:
    SERVICE_NAME = "locator_workbench"
    DATASET_NAME = "定位策略执行记录数据集"

    def __init__(self) -> None:
        self._element_svc = ElementSelectorService()

    # ------------------------------------------------------------------
    # 处理项：接收 + 展开
    # ------------------------------------------------------------------

    def expand_work_item(self, work_item: dict[str, Any]) -> dict[str, Any]:
        """展开处理项承载的定位策略执行记录。

        处理项约定的数据集：``work_item_id`` + ``page_model_id`` +
        ``records``（每条至少含 rule_id 或定位表达式）。展开后每条记录都带
        页面结构指纹、候选路径和最近一次处理状态，运行时可直接消费。
        """
        model_id = _as_int(work_item.get("page_model_id"))
        model = store.find("page_model", model_id) if model_id is not None else None
        summary = summarize_page_model(model) if model is not None else None
        raw_records = work_item.get("records") or []
        if not isinstance(raw_records, list):
            raw_records = []

        expanded: list[dict[str, Any]] = []
        seen: set[tuple[Any, str]] = set()
        for raw in raw_records:
            if not isinstance(raw, dict):
                continue
            rule = self._hydrate_rule(raw, model_id)
            key = (raw.get("rule_id"), str(raw.get("expression") or ""))
            if key in seen:
                continue
            seen.add(key)
            expanded.append(self._expand_record(raw, rule, summary))

        return {
            "work_item_id": work_item.get("work_item_id"),
            "page_model_id": model_id,
            "page_model_name": (
                summary.get("model_name") if summary else work_item.get("page_model_name")
            ),
            "page_fingerprint": summary.get("fingerprint") if summary else
            work_item.get("page_fingerprint"),
            "dataset": self.DATASET_NAME,
            "record_count": len(expanded),
            "records": expanded,
        }

    def execute_work_item(
        self,
        work_item: dict[str, Any],
        *,
        policy: str = "force_rebuild",
    ) -> dict[str, Any]:
        """展开并执行处理项：结构未变只重试候选路径，结构变化批量重解析。"""
        expanded = self.expand_work_item(work_item)
        results = []
        for record in expanded["records"]:
            results.append(runtime_session.execute_element(
                record,
                session_id=str(work_item.get("work_item_id") or "work-item"),
                policy=policy,
                source="work_item",
                retry_only=bool(work_item.get("retry_only")),
            ))
        accepted = [
            r for r in results if r.get("accepted") and r.get("success")]
        return {
            "work_item_id": expanded["work_item_id"],
            "dataset": expanded["dataset"],
            "record_count": expanded["record_count"],
            "accepted": len(accepted),
            "conflicts": sum(1 for r in results if r.get("outcome") == OUTCOME_CONFLICT),
            "probes": sum(int(r.get("probes", 0)) for r in results),
            "scanned_whole_group": False,
            "records": results,
        }

    def _hydrate_rule(self, raw: dict[str, Any],
                      model_id: int | None) -> dict[str, Any] | None:
        rule_id = _as_int(raw.get("rule_id"))
        if rule_id is not None:
            return store.find("element_selector", rule_id)
        for rule in store.rows("element_selector"):
            if str(rule.get("定位表达式", "")) == str(raw.get("expression", "")):
                return rule
        return None

    def _expand_record(self, raw: dict[str, Any],
                       rule: dict[str, Any] | None,
                       summary: dict[str, Any] | None) -> dict[str, Any]:
        expression = str(
            raw.get("expression")
            or (rule or {}).get("定位表达式", "")
        )
        fallback = str(
            raw.get("fallback")
            or (rule or {}).get("回退方案", "")
        )
        fingerprint = (
            raw.get("page_fingerprint")
            or (summary or {}).get("fingerprint")
            or ""
        )
        boundaries = (
            raw.get("boundaries")
            or (
                parse_selector_boundaries(
                    summary.get("structure"), summary.get("dynamic_elements"))
                if summary else []
            )
        )
        cached = locator_cache.latest_for_rule(
            int(summary["model_id"]), int((rule or {}).get("id") or 0)
        ) if summary else None
        decision = decide_processing(
            {
                "expression": expression,
                "fallback": fallback,
                "page_fingerprint": raw.get("page_fingerprint") or fingerprint,
                "boundaries": boundaries,
            },
            summary or {
                "fingerprint": fingerprint,
                "structure": expression,
                "dynamic_elements": "",
            },
            cached if cached is not None and not cached.stale else None,
            policy="force_rebuild",
        )
        return {
            "rule_id": (rule or {}).get("id", raw.get("rule_id")),
            "rule_name": raw.get("rule_name") or (rule or {}).get("规则名称"),
            "expression": expression,
            "fallback": fallback,
            "candidate_paths": _candidate_paths(expression, fallback),
            "page_model_id": (summary or {}).get("model_id"),
            "page_fingerprint": fingerprint,
            "boundaries": boundaries,
            "boundary_count": len(boundaries),
            "status_code": decision["status_code"],
            "status_label": decision["status_label"],
            "structure_changed": decision["structure_changed"],
            "reuse_cache": decision["cached_reused"],
            "rebuild": decision["rebuild"],
        }

    # ------------------------------------------------------------------
    # 窗口历史重算
    # ------------------------------------------------------------------

    def recompute_window(self, page_model_id: int, window: int = 50) -> dict[str, Any]:
        """重算窗口内定位策略执行历史，并解析当前可复用边界。

        重算只读取台账与当前页面结构，不会重新扫描整组选择器：
        命中历史按 (rule_id, fingerprint) 聚合，指纹不一致的历史直接判废。
        """
        model = store.find("page_model", page_model_id)
        if model is None:
            return {"page_model_id": page_model_id, "error": "页面模型不存在"}
        summary = summarize_page_model(model)
        fingerprint = summary["fingerprint"]
        records = locator_ledger.records(page_model_id=page_model_id, window=window)

        boundaries = parse_selector_boundaries(
            summary["structure"], summary["dynamic_elements"])
        history: dict[int, dict[str, Any]] = {}
        for record in records:
            rule_id = int(record.get("rule_id") or 0)
            entry = history.setdefault(rule_id, {
                "rule_id": rule_id,
                "rule_name": record.get("rule_name"),
                "attempts": 0,
                "probes": 0,
                "last_outcome": None,
                "last_status_code": None,
                "last_status_label": None,
                "last_fingerprint": None,
                "reusable": False,
            })
            entry["attempts"] += 1
            entry["probes"] += int(record.get("probes", 0))
            entry["last_outcome"] = record.get("outcome")
            entry["last_status_code"] = record.get("status_code")
            entry["last_status_label"] = record.get("status_label")
            entry["last_fingerprint"] = record.get("page_fingerprint")
            entry["reusable"] = (
                record.get("outcome") in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT)
                and record.get("page_fingerprint") == fingerprint
                and not record.get("stale_published")
            )

        stale_history = [
            item for item in history.values() if not item["reusable"]
        ]
        return {
            "page_model_id": page_model_id,
            "page_model_name": summary["model_name"],
            "fingerprint": fingerprint,
            "window": window,
            "records_examined": len(records),
            "history": list(history.values()),
            "reusable_boundaries": boundaries,
            "reusable_boundary_count": len(boundaries),
            "stale_rules": stale_history,
            "stale_rule_count": len(stale_history),
            "retry_candidates": len(stale_history),
            "scanned_whole_group": False,
        }

    # ------------------------------------------------------------------
    # 工作台视图
    # ------------------------------------------------------------------

    def snapshot(self, page_model_id: int | None = None) -> dict[str, Any]:
        """按页面模型输出缓存指纹、失效策略与最近执行记录。"""
        models = store.rows("page_model")
        if page_model_id is not None:
            models = [m for m in models if int(m.get("id", 0)) == int(page_model_id)]

        panels = []
        for model in models:
            mid = int(model.get("id", 0))
            summary = summarize_page_model(model)
            observed = locator_cache.observe(model)
            cache_entries = locator_cache.entries_for_model(mid)
            pending = runtime_session.invalid_boundaries(mid)
            pending_ids = {item["rule_id"] for item in pending}
            stale_ids = {e["rule_id"] for e in cache_entries if e["stale"]}

            invalid_rules = []
            for rule in store.rows("element_selector"):
                if self._element_svc.resolve_page_model_id(rule) != mid:
                    continue
                rule_id = rule.get("id")
                if not (rule_id in stale_ids or rule_id in pending_ids
                        or rule.get("status") == "定位失效"):
                    continue
                reason = (
                    "缓存已失效" if rule_id in stale_ids
                    else "最近执行未命中" if rule_id in pending_ids
                    else "状态为定位失效"
                )
                invalid_rules.append({
                    "rule_id": rule_id,
                    "rule_name": rule.get("规则名称"),
                    "expression": rule.get("定位表达式"),
                    "fallback": rule.get("回退方案"),
                    "reason": reason,
                    "retryable": rule_id in pending_ids,
                })

            records = locator_ledger.records(page_model_id=mid, window=20)
            recomputed = self.recompute_window(mid, window=50)
            panels.append({
                "model_id": mid,
                "model_name": summary["model_name"],
                "target_site": summary["target_site"],
                "fingerprint": summary["fingerprint"],
                "previous_fingerprint": observed.get("previous_fingerprint"),
                "structure_changed": observed.get("structure_changed", False),
                "summary": summary["summary"],
                "boundaries": recomputed["reusable_boundaries"],
                "boundary_count": recomputed["reusable_boundary_count"],
                "cache": {
                    "entries": cache_entries,
                    "valid": sum(1 for e in cache_entries if not e["stale"]),
                    "stale": sum(1 for e in cache_entries if e["stale"]),
                },
                "invalid_rules": invalid_rules,
                "retry_boundary": pending,
                "retry_candidate_count": len(pending),
                "recent_executions": records,
                "window_history": recomputed["history"],
                "status_labels": dict(PROCESS_STATUS),
            })

        return {
            "dataset": self.DATASET_NAME,
            "count": len(panels),
            "panels": panels,
            "ledger": locator_ledger.snapshot(),
            "cache": locator_cache.snapshot(),
            # 处理清单与结果列表来自同一份快照，前端一次刷新即可保持一致。
            "invalid_total": sum(len(p["invalid_rules"]) for p in panels),
            "result_total": sum(len(p["recent_executions"]) for p in panels),
        }

    def executions(self, page_model_id: int | None = None,
                   window: int = 50) -> dict[str, Any]:
        records = locator_ledger.records(
            page_model_id=page_model_id, window=window)
        return {
            "dataset": self.DATASET_NAME,
            "count": len(records),
            "window": window,
            "records": records,
            "status_labels": dict(PROCESS_STATUS),
            "summary": locator_ledger.snapshot(),
        }

    def retry(self, page_model_id: int | None = None,
              policy: str = "force_rebuild") -> dict[str, Any]:
        return runtime_session.retry_invalid_elements(
            page_model_id=page_model_id, policy=policy)

    def simulate_page_refresh(self, page_model_id: int) -> dict[str, Any]:
        """模拟动态页面发布新版本：结构摘要指纹变化，可复用边界保持不变。

        用于回归“结构变化但旧回退仍能命中”的分支：指纹变化后旧缓存整体失效，
        再执行就会落到 12 号结构冲突终态，而不是把旧命中发布成新记录。
        """
        model = store.find("page_model", page_model_id)
        if model is None:
            return {"ok": False, "message": "页面模型不存在"}
        before = summarize_page_model(model)
        version = str(model.get("版本号") or "1.0")
        try:
            major, minor = (int(part) for part in version.split(".")[:2])
        except (TypeError, ValueError):
            major, minor = 1, 0
        model["版本号"] = f"{major}.{minor + 1}"
        after = locator_cache.observe(model)
        return {
            "ok": True,
            "page_model_id": page_model_id,
            "previous_fingerprint": before["fingerprint"],
            "fingerprint": after["fingerprint"],
            "structure_changed": after["structure_changed"],
            "invalidated_entries": after.get("invalidated_entries", 0),
            "version": model["版本号"],
        }

    def run(self, policy: str = "force_rebuild") -> dict[str, Any]:
        """发布启用规则的策略并执行一轮元素定位。"""
        published = 0
        for rule in store.rows("element_selector"):
            if rule.get("status") not in ("草稿", "已测试", "已启用"):
                continue
            if self._element_svc.publish_locator_strategy(rule["id"]) is None:
                store.channel.publish(
                    topic="rpa.locator_strategies",
                    source=self._element_svc.MODULE,
                    payload=self._element_svc.build_locator_strategy(rule),
                )
            published += 1
        result = runtime_session.consume_strategies_and_execute(policy=policy)
        return {"published": published, "policy": policy, "result": result}

    def domain_snapshot(self) -> dict[str, Any]:
        return {
            "module": self.SERVICE_NAME,
            "entity": "定位工作台",
            "artifact": self.DATASET_NAME,
            "cache": locator_cache.snapshot(),
            "ledger": locator_ledger.snapshot(),
            "pending_retry": sum(
                len(p["retry_boundary"]) for p in self.snapshot()["panels"]),
        }


def _candidate_paths(expression: str, fallback: str) -> list[dict[str, str]]:
    paths = []
    if expression:
        paths.append({"stage": "primary", "expression": expression})
    if fallback:
        paths.append({"stage": "fallback", "expression": fallback})
    return paths


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


locator_workbench = LocatorWorkbenchService()
