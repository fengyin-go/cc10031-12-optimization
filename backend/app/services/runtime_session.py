"""运行时会话服务 — 消费定位策略并执行自动化操作。

这是元素定位链路的末端消费者，也是“定位策略执行记录数据集”的生产者。

数据流:
    channel("rpa.locator_strategies") ← element_selector
    → runtime_session 消费 → 定位缓存 / 页面结构指纹校验 → 模拟页面自动化执行
    → 执行记录台账（定位工作台可查）+ channel("rpa.automation_executed") → execution_log

优化要点：
* 失效选择器与页面结构指纹一起缓存：指纹变化后缓存立即失效，
  失败恢复不会沿用旧命中。
* 结构未变时只重试候选路径（主策略 / 回退），不再重复扫描整组选择器。
* 结构变化时批量重新解析；旧回退仍命中则落到 12 号处理终态，旧命中不发布。
* 并发执行复用同一首胜闸门，只接受首个成功结果。
* 所有“结构是否变化 / 缓存能否复用 / 是否冲突 / 状态编号”的判断
  统一走 ``decide_processing``，运行时与工作台不再各写一遍。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.locator_cache import (
    OUTCOME_CONFLICT,
    OUTCOME_FALLBACK_HIT,
    OUTCOME_HIT,
    OUTCOME_MISS,
    POLICY_FORCE_REBUILD,
    PROCESS_STATUS,
    SUPPORTED_POLICIES,
    decide_processing,
    first_success_gate,
    locator_cache,
    locator_ledger,
    summarize_strategy,
)
from app.store import store


class RuntimeSessionService:
    """运行时会话管理 — 不继承 BaseModuleService，是纯跨模块消费者。"""

    SERVICE_NAME = "runtime_session"

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._pending: dict[tuple[int | None, int], dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 消费入口
    # ------------------------------------------------------------------

    def consume_strategies_and_execute(
        self,
        *,
        policy: str = POLICY_FORCE_REBUILD,
    ) -> dict[str, Any]:
        """消费定位策略并执行模拟自动化操作。

        每条策略都先查页面结构指纹缓存：结构未变且缓存有效时直接复用，
        不再扫描选择器；缓存失效时只重试候选路径。

        Returns:
            {"executed": int, "session_id": str, "results": [...], ...}
        """
        policy = self._normalize_policy(policy)
        msgs = store.channel.consume(
            consumer=self.SERVICE_NAME,
            topic="rpa.locator_strategies",
        )
        if not msgs:
            return {"executed": 0, "session_id": None, "results": []}

        session_id = str(uuid.uuid4())[:8]
        results = []
        with first_success_gate.round():
            for msg in msgs:
                record = self.execute_element(
                    msg.payload, session_id=session_id, policy=policy,
                    source="channel",
                )
                results.append(record)
                if record["outcome"] not in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT):
                    store.recovery.log(
                        module=self.SERVICE_NAME,
                        batch_ref=session_id,
                        step=f"apply_locator:{msg.payload.get('rule_name', 'unknown')}",
                        error_detail=record.get("error", "定位未命中"),
                        fingerprint=record.get("page_fingerprint"),
                    )
                store.channel.confirm(msg.msg_id, self.SERVICE_NAME)

        session = self._summarize(session_id, policy, results)
        self._publish_session(session_id, session, results)
        self._sessions[session_id] = session
        return {
            "executed": session["actions_executed"],
            "session_id": session_id,
            "total": len(msgs),
            "errors": session["errors"],
            "conflicts": session["conflicts"],
            "results": results,
        }

    @staticmethod
    def _summarize(session_id: str, policy: str,
                   results: list[dict[str, Any]]) -> dict[str, Any]:
        """统一汇总一轮执行：批量消费与增量重试共用同一份统计口径。"""
        accepted = [
            r for r in results
            if r.get("accepted") and r["outcome"] in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT)
        ]
        return {
            "session_id": session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "policy": policy,
            "strategies_applied": len(results),
            "actions_executed": len(accepted),
            "errors": len(results) - len(accepted),
            "conflicts": sum(
                1 for r in results if r["outcome"] == OUTCOME_CONFLICT),
            "probe_total": sum(int(r.get("probes", 0)) for r in results),
        }

    def execute_element(
        self,
        strategy: dict[str, Any],
        *,
        session_id: str,
        policy: str = POLICY_FORCE_REBUILD,
        source: str = "retry",
        retry_only: bool = False,
    ) -> dict[str, Any]:
        """执行单个元素的定位：缓存判定 → 候选边界探测 → 台账记录。

        这是运行时唯一的一处元素执行实现，批量消费和增量重试都复用它。
        """
        policy = self._normalize_policy(policy)
        context = self._resolve_context(strategy)
        summary = context["summary"]
        model_id = summary.get("model_id")
        rule_id = int(strategy.get("rule_id") or 0)
        fingerprint = summary.get("fingerprint", "")

        cached = context["cached"]
        item = {
            **strategy,
            # 结构变化检测以“上次缓存/发布的指纹”为准，
            # 新发布的策略即使已带新指纹也不能掩盖变化。
            "page_fingerprint": context["item_fingerprint"],
            "boundaries": strategy.get("boundaries")
            or context.get("boundaries", []),
        }

        decision = decide_processing(
            item, summary, cached, policy=policy, retry_only=retry_only)

        claimed = True
        gate_owner = None
        if decision["outcome"] in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT):
            gate_key = f"{model_id}:{fingerprint}:{rule_id}"
            claimed, winner = first_success_gate.claim(gate_key, session_id)
            if not claimed:
                gate_owner = winner.get("owner") if winner else None

        record = self._build_record(
            strategy=strategy,
            summary=summary,
            decision=decision,
            session_id=session_id,
            source=source,
            policy=policy,
            cached=cached,
            accepted=claimed,
            gate_owner=gate_owner,
            retry_only=retry_only,
        )

        # 只有被接受的成功结果才写入新缓存；冲突/落败/未命中都不发布。
        if claimed and decision["outcome"] in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT):
            locator_cache.put(
                model_id=model_id,
                fingerprint=fingerprint,
                rule_id=rule_id,
                rule_name=str(strategy.get("rule_name") or ""),
                outcome=decision["outcome"],
                matched_selector=decision["matched_selector"],
                attempts=decision["probes"],
            )
            self._forget_pending(model_id, rule_id)
            self._sync_rule_status(rule_id, recovered=True)
        elif decision["outcome"] == OUTCOME_MISS:
            self._remember_pending(strategy, summary, decision)
            self._sync_rule_status(rule_id, recovered=False)
        elif decision["outcome"] == OUTCOME_CONFLICT:
            self._remember_pending(strategy, summary, decision)
            self._sync_rule_status(rule_id, recovered=False)
        elif not claimed:
            record["status_code"] = 9
            record["status_label"] = PROCESS_STATUS[9]

        if (
            decision["structure_changed"]
            and decision["outcome"] != OUTCOME_CONFLICT
            and claimed
            and model_id is not None
            and self._rebuild_complete(model_id)
        ):
            locator_cache.consume_change(model_id)

        # 单元素执行本身就是“定位策略执行记录数据集”的一条记录，
        # 批量消费与增量重试都通过这里写台账，避免两处各写一遍。
        stored = locator_ledger.add(record)
        record["record_id"] = stored["record_id"]
        return record

    def _rebuild_complete(self, model_id: int | None) -> bool:
        """结构变化后的重建是否完成：该模型已无失效缓存与待重试元素。"""
        if model_id is None:
            return False
        with self._lock:
            if any(key[0] == int(model_id) for key in self._pending):
                return False
        return not locator_cache.stale_entries(model_id)

    # ------------------------------------------------------------------
    # 结构变化后的重试：只处理仍失效的元素
    # ------------------------------------------------------------------

    def invalid_boundaries(self, page_model_id: int | None = None) -> list[dict[str, Any]]:
        """返回可重试边界：仍未命中的元素（按页面模型过滤）。"""
        entries: list[dict[str, Any]] = []
        with self._lock:
            pending = list(self._pending.items())
        for (model_id, rule_id), item in pending:
            if page_model_id is not None and model_id != int(page_model_id):
                continue
            entries.append({
                "page_model_id": model_id,
                "rule_id": rule_id,
                "rule_name": item.get("rule_name"),
                "expression": item.get("expression"),
                "fallback": item.get("fallback"),
                "last_fingerprint": item.get("page_fingerprint"),
                "status_code": item.get("status_code", 5),
                "status_label": PROCESS_STATUS.get(
                    int(item.get("status_code", 5)), "全部未命中"),
                "attempted_at": item.get("attempted_at"),
            })
        return entries

    def retry_invalid_elements(
        self,
        *,
        page_model_id: int | None = None,
        policy: str = POLICY_FORCE_REBUILD,
    ) -> dict[str, Any]:
        """增量重试：只重跑仍失效的元素，不重新扫描整组选择器。

        失效范围包括两类：最近执行未命中的元素，以及页面结构变化后
        被整体作废的缓存命中。结构未变时仅重试候选路径（每个元素至多
        主策略 + 回退两次探测）；结构变化时按策略批量重新解析，旧命中不发布。
        """
        policy = self._normalize_policy(policy)
        targets = self._retry_targets(page_model_id)
        if not targets:
            return {
                "retried": 0, "recovered": 0, "conflicts": 0,
                "session_id": None, "results": [], "policy": policy,
                "probes": 0, "scanned_whole_group": False,
                "remaining_invalid": 0,
            }

        session_id = str(uuid.uuid4())[:8]
        results = []
        with first_success_gate.round():
            for item in targets:
                results.append(self.execute_element(
                    item, session_id=session_id, policy=policy,
                    source="retry", retry_only=True,
                ))

        session = self._summarize(session_id, policy, results)
        self._publish_session(session_id, session, results)
        self._sessions[session_id] = session

        # 该模型下已没有失效元素时，视为结构变化已完成重建。
        for model_id in {record.get("page_model_id") for record in results}:
            if self._rebuild_complete(model_id):
                locator_cache.consume_change(model_id)

        return {
            "retried": len(targets),
            "recovered": session["actions_executed"],
            "conflicts": session["conflicts"],
            "session_id": session_id,
            "policy": policy,
            "probes": session["probe_total"],
            # 每个元素最多探测主策略 + 回退，绝不重复扫描整组选择器。
            "scanned_whole_group": False,
            "remaining_invalid": len(self._retry_targets(page_model_id)),
            "results": results,
        }

    def _retry_targets(self, page_model_id: int | None) -> list[dict[str, Any]]:
        """汇总仍失效的元素：未命中项 + 结构变化后作废的缓存项。"""
        from app.services.element_selector import ElementSelectorService

        with self._lock:
            pending = {
                key: dict(item) for key, item in self._pending.items()
                if page_model_id is None or key[0] == int(page_model_id)
            }

        targets: list[dict[str, Any]] = []
        seen: set[tuple[int | None, int]] = set()
        for key, item in pending.items():
            seen.add(key)
            # 重试以最新的规则定义为准（候选路径可能已修订），
            # 但保留上次失效时的指纹作为结构变化对比基线。
            rule = store.find("element_selector", key[1]) if key[1] else None
            if rule is not None:
                refresh = ElementSelectorService().build_locator_strategy(rule)
                refresh["page_fingerprint"] = item.get("page_fingerprint")
                targets.append(refresh)
            else:
                targets.append(item)

        for entry in locator_cache.stale_entries(page_model_id):
            key = (entry.model_id, entry.rule_id)
            if key in seen:
                continue
            seen.add(key)
            rule = store.find("element_selector", entry.rule_id)
            if rule is None:
                continue
            strategy = ElementSelectorService().build_locator_strategy(rule)
            strategy["page_fingerprint"] = entry.fingerprint
            targets.append(strategy)
        return targets

    # ------------------------------------------------------------------
    # 上下文与记录
    # ------------------------------------------------------------------

    def _resolve_context(self, strategy: dict[str, Any]) -> dict[str, Any]:
        """解析页面结构摘要、指纹变化与缓存命中情况。"""
        model_id = strategy.get("page_model_id")
        model = store.find("page_model", int(model_id)) if model_id else None
        if model is None:
            summary = summarize_strategy(strategy)
            return {
                "summary": summary,
                "cached": None,
                "item_fingerprint": summary["fingerprint"],
                "boundaries": strategy.get("boundaries", []),
            }

        observed = locator_cache.observe(model)
        summary = {
            "model_id": observed["model_id"],
            "model_name": observed["model_name"],
            "structure": observed["structure"],
            "dynamic_elements": observed["dynamic_elements"],
            "fingerprint": observed["fingerprint"],
            "summary": observed["summary"],
        }
        rule_id = int(strategy.get("rule_id") or 0)
        latest = locator_cache.latest_for_rule(int(model_id), rule_id)
        cached = None
        if latest is not None and not latest.stale \
                and latest.fingerprint == observed["fingerprint"]:
            cached = latest

        item_fingerprint = strategy.get("page_fingerprint") or ""
        if latest is not None:
            # 以历史命中/失效记录的指纹作为对比基线：与当前摘要不同即结构变化。
            item_fingerprint = latest.fingerprint
        elif not item_fingerprint:
            item_fingerprint = observed.get("previous_fingerprint") or observed["fingerprint"]
        if observed.get("structure_changed"):
            item_fingerprint = observed.get("previous_fingerprint") or item_fingerprint

        return {
            "summary": summary,
            "cached": cached,
            "item_fingerprint": item_fingerprint,
            "boundaries": strategy.get("boundaries", []),
        }

    def _build_record(
        self,
        *,
        strategy: dict[str, Any],
        summary: dict[str, Any],
        decision: dict[str, Any],
        session_id: str,
        source: str,
        policy: str,
        cached: Any,
        accepted: bool,
        gate_owner: str | None,
        retry_only: bool,
    ) -> dict[str, Any]:
        success = decision["outcome"] in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT)
        record = {
            "record_id": None,
            "session_id": session_id,
            "source": source,
            "retry_only": retry_only,
            "rule_id": strategy.get("rule_id"),
            "rule_name": strategy.get("rule_name"),
            "page_model_id": summary.get("model_id"),
            "page_model_name": summary.get("model_name"),
            "page_fingerprint": summary.get("fingerprint"),
            "previous_fingerprint": None,
            "expression": strategy.get("expression", ""),
            "fallback": strategy.get("fallback", ""),
            "outcome": decision["outcome"],
            "success": success and accepted,
            "status_code": decision["status_code"],
            "status_label": decision["status_label"],
            "matched_selector": decision["matched_selector"],
            "probes": decision["probes"],
            "structure_changed": decision["structure_changed"],
            "cached_reused": decision["cached_reused"],
            "rebuild": decision["rebuild"],
            "policy": policy,
            "stale_published": decision["stale_published"],
            "stale_selector": decision["stale_selector"],
            "accepted": accepted,
            "gate_owner": gate_owner,
            "note": decision["note"],
            "error": "" if success else decision["note"],
            "cache_attempts": getattr(cached, "attempts", 0) if cached else 0,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        # 结构变化但没有缓存条目时，仍要留下上次命中的旧指纹作对比证据。
        if decision["structure_changed"]:
            record["previous_fingerprint"] = (
                getattr(cached, "fingerprint", None)
                or strategy.get("page_fingerprint")
                or None
            )
        return record

    def _remember_pending(self, strategy: dict[str, Any],
                          summary: dict[str, Any],
                          decision: dict[str, Any]) -> None:
        model_id = summary.get("model_id")
        rule_id = int(strategy.get("rule_id") or 0)
        with self._lock:
            self._pending[(model_id, rule_id)] = {
                **strategy,
                "page_model_id": model_id,
                "page_fingerprint": summary.get("fingerprint"),
                "status_code": decision["status_code"],
                "attempted_at": datetime.now(timezone.utc).isoformat(),
            }

    def _forget_pending(self, model_id: int | None, rule_id: int) -> None:
        with self._lock:
            self._pending.pop((model_id, rule_id), None)

    def _sync_rule_status(self, rule_id: int, *, recovered: bool) -> None:
        """把执行结果回写到定位规则状态，让元素定位与工作台一起刷新。"""
        rule = store.find("element_selector", int(rule_id)) if rule_id else None
        if rule is None:
            return
        if recovered:
            if rule.get("status") == "定位失效":
                rule["status"] = "已启用"
                rule["pending"] = False
                rule["abnormal"] = False
        elif rule.get("status") in ("已启用", "已测试", "草稿"):
            rule["status"] = "定位失效"
            rule["pending"] = True
            rule["abnormal"] = True

    def _publish_session(self, session_id: str, session: dict[str, Any],
                         results: list[dict[str, Any]]) -> None:
        """把本轮执行结果发布到通道，供 execution_log 与工作台消费。

        单条记录已在 ``execute_element`` 写入台账，这里只做会话级汇总，
        避免同一份执行记录被写两遍。
        """
        accepted = [r for r in results if r.get("accepted") and r.get("success")]
        store.channel.publish(
            topic="rpa.automation_executed",
            source=self.SERVICE_NAME,
            payload={
                "session_id": session_id,
                "policy": session.get("policy"),
                "total_strategies": len(results),
                "successful": session.get("actions_executed", 0),
                "failed": session.get("errors", 0),
                "conflicts": session.get("conflicts", 0),
                "probe_total": session.get("probe_total", 0),
                "accepted": len(accepted),
                "results": results,
            },
        )

    @staticmethod
    def _normalize_policy(policy: str | None) -> str:
        return policy if policy in SUPPORTED_POLICIES else POLICY_FORCE_REBUILD

    # ------------------------------------------------------------------
    # 会话查询
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有运行中的会话。"""
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def reset(self) -> None:
        """清空会话、待重试边界与首胜闸门（测试与人工重置用）。"""
        with self._lock:
            self._sessions.clear()
            self._pending.clear()
        first_success_gate.reset()

    def domain_snapshot(self) -> dict[str, Any]:
        return {
            "module": self.SERVICE_NAME,
            "entity": "运行时会话",
            "artifact": "自动化执行记录",
            "active_sessions": len(self._sessions),
            "actions_executed": sum(
                s.get("actions_executed", 0) for s in self._sessions.values()),
            "automation_events": len(
                store.channel.snapshot(topic="rpa.automation_executed")),
            "locator_cache": locator_cache.snapshot(),
            "pending_retry": len(self.invalid_boundaries()),
            "execution_ledger": locator_ledger.snapshot(),
        }


# 全局运行时实例
runtime_session = RuntimeSessionService()
