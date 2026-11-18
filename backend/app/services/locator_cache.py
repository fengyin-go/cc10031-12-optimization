"""定位缓存与执行台账 — 动态页面定位策略的失效缓存、指纹校验与记录数据集。

设计约束（对应定位工作台与运行时恢复链路）：

* 失效选择器必须和**页面结构指纹**一起缓存；指纹变化后旧缓存立即失效，
  失败恢复也不能沿用旧命中。
* 重试只处理仍失效的候选路径，不重新扫描整组选择器。
* 结构变化且旧回退仍能命中时，旧命中不能直接发布为新记录，
  冲突统一落到第 12 号处理终态（``STRUCTURE_CONFLICT``）。
* 所有页面结构摘要、选择器边界解析、指纹计算集中在本模块，
  供 element_selector / page_model / runtime_session 共用，避免各处重复判定。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator


# ---------------------------------------------------------------------------
# 处理终态：12 号终态专指“结构变化但旧回退仍命中”的冲突。
# ---------------------------------------------------------------------------

PROCESS_STATUS: dict[int, str] = {
    1: "待执行",
    2: "主策略命中",
    3: "回退命中",
    4: "缓存命中",
    5: "全部未命中",
    6: "结构未变-仅重试候选",
    7: "结构变化-批量重解析",
    8: "首胜提交",
    9: "并发落败",
    10: "旧命中已作废",
    11: "可重试边界",
    12: "结构冲突待重建",
}

STATUS_STRUCTURE_CONFLICT = 12
TERMINAL_STATUSES = frozenset({2, 3, 4, 5, 8, 9, 10, 11, 12})

OUTCOME_HIT = "hit"
OUTCOME_FALLBACK_HIT = "fallback_hit"
OUTCOME_MISS = "miss"
OUTCOME_CONFLICT = "conflict"

# 触发结构冲突时的两种可选处理策略，两种都不会直接发布旧命中。
POLICY_FORCE_REBUILD = "force_rebuild"
POLICY_PROBE_THEN_DECIDE = "probe_then_decide"
SUPPORTED_POLICIES = (POLICY_FORCE_REBUILD, POLICY_PROBE_THEN_DECIDE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 页面结构摘要 + 指纹 + 选择器边界
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_][\w-]*")

_IGNORED_TOKENS = {
    "div", "span", "table", "tbody", "thead", "tr", "td", "th", "ul", "li",
    "a", "p", "form", "input", "button", "select", "option", "img", "body",
    "html", "class", "type", "name", "value", "href", "src", "style",
    "and", "or", "not", "contains", "text", "xpath", "css", "id",
}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def summarize_page_model(model: dict[str, Any]) -> dict[str, Any]:
    """读取页面模型的结构摘要，并计算稳定的结构指纹。

    指纹只覆盖会影响定位的结构字段；页面名称、状态等展示字段不参与，
    这样“同一个页面模型换状态”不会误伤缓存。
    """
    structure = _clean(model.get("页面结构"))
    dynamic = _clean(model.get("动态元素"))
    pagination = _clean(model.get("分页模式"))
    load_strategy = _clean(model.get("加载策略"))
    version = _clean(model.get("版本号"))

    fingerprint_payload = {
        "structure": " ".join(structure.split()),
        "dynamic": " ".join(dynamic.split()),
        "pagination": pagination,
        "load_strategy": load_strategy,
        "version": version,
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    return {
        "model_id": model.get("id"),
        "model_name": _clean(model.get("模型名称")),
        "target_site": _clean(model.get("目标站点")),
        "structure": structure,
        "dynamic_elements": dynamic,
        "pagination_mode": pagination,
        "load_strategy": load_strategy,
        "version": version,
        "fingerprint": digest[:16],
        "fingerprint_full": digest,
        "summary": (
            f"{_clean(model.get('模型名称')) or '未命名页面'}｜"
            f"{structure or '结构未定义'}｜分页={pagination or 'none'}｜"
            f"加载={load_strategy or 'lazy'}｜v{version or '1.0'}"
        ),
    }


def summarize_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    """为缺少页面模型上下文的旧版定位策略生成兼容摘要。

    旧版本消息只带选择器表达式，没有 page_model_id/fingerprint；
    这里用策略字段生成稳定的合成摘要，保证旧边界仍可执行且缓存键自洽。
    """
    expression = _clean(strategy.get("expression"))
    fallback = _clean(strategy.get("fallback"))
    selector_type = _clean(strategy.get("selector_type"))
    digest = hashlib.sha256(
        json.dumps(
            {"expression": expression, "fallback": fallback, "type": selector_type},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "model_id": strategy.get("page_model_id"),
        "model_name": _clean(strategy.get("bot_ref")) or "未绑定页面模型",
        "target_site": "",
        "structure": expression,
        "dynamic_elements": "",
        "pagination_mode": "",
        "load_strategy": "",
        "version": _clean(strategy.get("version")) or "legacy",
        "fingerprint": digest[:16],
        "fingerprint_full": digest,
        "summary": f"旧版策略｜{expression or '空表达式'}",
    }


def selector_tokens(expression: Any) -> set[str]:
    """把选择器表达式拆成可比较的边界 token。

    只保留有辨识度的标识（id/class/属性/字段名），忽略标签名与运算符，
    从而把 ``.list .row .name`` 与页面结构里的同名边界视为同一可复用路径。
    """
    raw = _clean(expression)
    if not raw:
        return set()
    tokens: set[str] = set()
    for match in _IDENT_RE.findall(raw):
        bare = match.strip()
        if not bare or bare.casefold() in _IGNORED_TOKENS:
            continue
        tokens.add(bare.casefold())
    return tokens


def parse_selector_boundaries(*texts: Any) -> list[dict[str, str]]:
    """解析页面结构摘要中当前页面可复用的选择器边界。"""
    boundaries: list[dict[str, str]] = []
    seen: set[str] = set()
    for text in texts:
        raw = _clean(text)
        if not raw:
            continue
        for match in re.findall(r"[^\s,;|>]+", raw):
            for kind, token in _classify_selector_token(match):
                key = f"{kind}:{token.casefold()}"
                if key in seen:
                    continue
                seen.add(key)
                boundaries.append({"kind": kind, "value": token})
    return boundaries


def _classify_selector_token(raw: str) -> Iterable[tuple[str, str]]:
    token = raw.strip()
    if not token:
        return []
    lowered = token.casefold()
    if lowered in _IGNORED_TOKENS:
        return []
    if token.startswith("#") and len(token) > 1:
        return [("id", token[1:])]
    if token.startswith(".") and len(token) > 1:
        return [("css", token[1:])]
    if token.startswith("//") or lowered.startswith("xpath:"):
        return [("xpath", token.removeprefix("xpath:"))]
    if "=" in token:
        name = token.split("=", 1)[0].split("[")[-1].strip()
        if name:
            return [("attribute", name)]
    if token.startswith("[") and token.endswith("]"):
        return [("attribute", token.strip("[]"))]
    if "[" in token and token.endswith("]"):
        return [("attribute", token.split("[", 1)[0])]
    if re.fullmatch(r"[A-Za-z_][\w-]*", token):
        return [("css", token)]
    return []


def boundary_values(boundaries: list[dict[str, str]]) -> set[str]:
    return {str(item.get("value", "")).casefold() for item in boundaries if item.get("value")}


def probe_expression(expression: Any, boundaries: list[dict[str, str]]) -> bool:
    """检测一条选择器表达式是否落在当前页面可复用的边界集合内。"""
    tokens = selector_tokens(expression)
    return bool(tokens) and tokens <= boundary_values(boundaries)


def status_for_outcome(outcome: str, *, cached: bool = False) -> int:
    """把探测结果映射为处理状态编号，所有调用方共用一套判断。"""
    if outcome == OUTCOME_HIT:
        return 4 if cached else 2
    if outcome == OUTCOME_FALLBACK_HIT:
        return 4 if cached else 3
    if outcome == OUTCOME_CONFLICT:
        return STATUS_STRUCTURE_CONFLICT
    return 5


def probe_candidates(strategy: dict[str, Any],
                     boundaries: list[dict[str, str]]) -> tuple[str, str, int]:
    """按候选路径顺序探测：主策略 →（仅主策略未命中时）回退。

    整组选择器不会重复扫描：每个元素最多两次探测。

    Returns:
        (outcome, matched_selector, probes)，probes 为实际执行的探测次数。
    """
    probes = 1
    primary = _clean(strategy.get("expression"))
    if probe_expression(primary, boundaries):
        return OUTCOME_HIT, primary, probes

    fallback = _clean(strategy.get("fallback"))
    if fallback:
        probes += 1
        if probe_expression(fallback, boundaries):
            return OUTCOME_FALLBACK_HIT, fallback, probes
    return OUTCOME_MISS, "", probes


def decide_processing(
    item: dict[str, Any],
    summary: dict[str, Any],
    cached: "LocatorCacheEntry | None",
    *,
    policy: str = POLICY_FORCE_REBUILD,
    retry_only: bool = False,
) -> dict[str, Any]:
    """统一处理路径：决定一次元素执行命中哪条策略、是否复用缓存、是否需要重建。

    这是运行时重试、定位工作台重试和批量重解析共用的唯一判定入口，
    各调用方不再各自复制“结构是否变化 / 缓存是否可用 / 是否算冲突”的判断。

    规则：
    * 结构未变且缓存有效 → 直接复用，不再扫描任何选择器；
    * 结构未变但缓存为失效 → 从主策略开始只重试候选路径，不扫描整组；
    * 结构变化 → 旧命中一律不发布；旧回退仍命中即冲突（12 号终态），
      否则按策略重建。
    """
    fingerprint = _clean(summary.get("fingerprint"))
    item_fingerprint = _clean(item.get("page_fingerprint"))
    structure_changed = bool(item_fingerprint and item_fingerprint != fingerprint)

    if structure_changed:
        # 结构变化后必须用新摘要重新解析边界，不能沿用旧策略里的边界集合。
        boundaries = parse_selector_boundaries(
            summary.get("structure"),
            summary.get("dynamic_elements"),
            summary.get("pagination_mode"),
        )
    else:
        boundaries = item.get("boundaries")
        if not boundaries:
            boundaries = parse_selector_boundaries(
                summary.get("structure"), summary.get("dynamic_elements"))

    strategy = {
        "expression": item.get("expression", ""),
        "fallback": item.get("fallback", ""),
    }

    if not structure_changed:
        if cached is not None and cached.outcome in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT):
            return _decision(
                cached.outcome, cached.matched_selector, 4, probes=0,
                structure_changed=False, cached_reused=True, rebuild=False,
                policy=policy, note="结构未变且缓存有效，直接复用命中",
            )
        outcome, matched, probes = probe_candidates(strategy, boundaries)
        return _decision(
            outcome, matched, status_for_outcome(outcome), probes=probes,
            structure_changed=False, cached_reused=False, rebuild=False,
            policy=policy,
            note="结构未变，仅重试候选路径" if cached is not None or retry_only
            else "首次执行候选路径",
        )

    # ---- 结构已变化：旧命中不能发布 ----
    stale_selector = ""
    if cached is not None and cached.outcome in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT):
        stale_selector = cached.matched_selector
    if not stale_selector:
        stale_selector = _clean(item.get("last_matched_selector"))

    probes = 0
    old_fallback = _clean(item.get("fallback"))
    old_hit_probe = ""
    if old_fallback:
        probes += 1
        if probe_expression(old_fallback, boundaries):
            old_hit_probe = old_fallback

    if old_hit_probe:
        if policy == POLICY_FORCE_REBUILD:
            probes += 1
            probe_candidates(strategy, boundaries)
            note = "结构变化：强制重建全部定位，旧回退命中不发布"
        else:
            note = "结构变化：旧回退仍命中，先探测后决定重建，旧命中不发布"
        return _decision(
            OUTCOME_CONFLICT, old_hit_probe, STATUS_STRUCTURE_CONFLICT,
            probes=probes, structure_changed=True, cached_reused=False,
            rebuild=True, policy=policy, note=note,
            stale_published=False, stale_selector=stale_selector,
        )

    outcome, matched, extra = probe_candidates(strategy, boundaries)
    probes += extra
    # 结构变化后重新解析：落在边界内的匹配视为对重建结果的一次首胜提交。
    status_code = status_for_outcome(outcome)
    if outcome == OUTCOME_HIT and policy == POLICY_PROBE_THEN_DECIDE:
        status_code = 8
    return _decision(
        outcome, matched, status_code, probes=probes,
        structure_changed=True, cached_reused=False, rebuild=True,
        policy=policy, note="结构变化：批量重新解析选择器边界",
        stale_published=False, stale_selector=stale_selector,
    )


def _decision(outcome: str, matched: str, status_code: int, *,
              probes: int, structure_changed: bool, cached_reused: bool,
              rebuild: bool, policy: str, note: str,
              stale_published: bool = False,
              stale_selector: str = "") -> dict[str, Any]:
    return {
        "outcome": outcome,
        "matched_selector": matched,
        "status_code": status_code,
        "status_label": PROCESS_STATUS.get(status_code, f"状态{status_code}"),
        "probes": probes,
        "structure_changed": structure_changed,
        "cached_reused": cached_reused,
        "rebuild": rebuild,
        "policy": policy,
        "stale_published": stale_published,
        "stale_selector": stale_selector,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 缓存条目
# ---------------------------------------------------------------------------

@dataclass
class LocatorCacheEntry:
    model_id: int | None
    fingerprint: str
    rule_id: int
    rule_name: str
    outcome: str
    matched_selector: str
    attempts: int = 1
    stale: bool = False
    invalidated_at: str | None = None
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "fingerprint": self.fingerprint,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "outcome": self.outcome,
            "matched_selector": self.matched_selector,
            "attempts": self.attempts,
            "stale": self.stale,
            "invalidated_at": self.invalidated_at,
            "updated_at": self.updated_at,
        }


class LocatorCache:
    """以 (page_model_id, fingerprint, rule_id) 为键的定位结果缓存。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[int | None, str, int], LocatorCacheEntry] = {}
        self._model_state: dict[int, dict[str, Any]] = {}

    # ---- 页面结构观测 ----

    def observe(self, model: dict[str, Any]) -> dict[str, Any]:
        """观测页面模型，指纹变化时让旧缓存整体失效。

        ``structure_changed`` 表示“结构变了但还没重建”：一旦观测到新指纹就置位，
        由 ``consume_change`` 在批量重解析/重试完成后清除。重复读取不会清除它，
        所以工作台能看到“待重建”状态，而重建后重试会走快速分支。
        """
        summary = summarize_page_model(model)
        model_id = model.get("id")
        fingerprint = summary["fingerprint"]
        invalidated = 0
        with self._lock:
            if model_id is None:
                return {
                    **summary, "previous_fingerprint": None,
                    "structure_changed": False, "invalidated_entries": 0,
                }
            state = self._model_state.get(int(model_id))
            if state is None:
                self._model_state[int(model_id)] = {
                    "fingerprint": fingerprint,
                    "previous_fingerprint": None,
                    "structure_changed": False,
                    "observed_at": _now(),
                }
            elif state["fingerprint"] != fingerprint:
                invalidated = self._invalidate_locked(
                    int(model_id), state["fingerprint"])
                state.update({
                    "fingerprint": fingerprint,
                    "previous_fingerprint": state["fingerprint"],
                    "structure_changed": True,
                    "observed_at": _now(),
                })
            state = dict(self._model_state[int(model_id)])
        return {
            **summary,
            "previous_fingerprint": state["previous_fingerprint"],
            "structure_changed": state["structure_changed"],
            "invalidated_entries": invalidated,
        }

    def consume_change(self, model_id: int | None) -> None:
        """标记该页面模型的结构变化已被重建处理。"""
        if model_id is None:
            return
        with self._lock:
            state = self._model_state.get(int(model_id))
            if state is not None:
                state["structure_changed"] = False

    def _invalidate_locked(self, model_id: int, previous: str | None) -> int:
        count = 0
        for entry in self._entries.values():
            if entry.model_id == model_id and entry.fingerprint != previous:
                continue
            if entry.model_id == model_id and not entry.stale:
                entry.stale = True
                entry.invalidated_at = _now()
                count += 1
        return count

    def invalidate_model(self, model_id: int) -> int:
        with self._lock:
            state = self._model_state.get(int(model_id), {})
            return self._invalidate_locked(int(model_id), state.get("fingerprint"))

    def state_for(self, model_id: int | None) -> dict[str, Any]:
        if model_id is None:
            return {}
        with self._lock:
            return dict(self._model_state.get(int(model_id), {}))

    # ---- 缓存读写 ----

    def get(self, model_id: int | None, fingerprint: str,
            rule_id: int) -> LocatorCacheEntry | None:
        with self._lock:
            entry = self._entries.get((model_id, fingerprint, rule_id))
            if entry is None or entry.stale:
                return None
            return entry

    def put(self, model_id: int | None, fingerprint: str, rule_id: int,
            rule_name: str, outcome: str, matched_selector: str,
            attempts: int = 1) -> LocatorCacheEntry:
        with self._lock:
            # 同一元素在新结构上已重新命中，旧结构的失效条目被取代，
            # 避免增量重试反复处理同一条历史失效。
            superseded = [
                key for key, entry in self._entries.items()
                if entry.model_id == model_id and entry.rule_id == rule_id
                and entry.fingerprint != fingerprint
            ]
            for key in superseded:
                del self._entries[key]
            entry = LocatorCacheEntry(
                model_id=model_id,
                fingerprint=fingerprint,
                rule_id=rule_id,
                rule_name=rule_name,
                outcome=outcome,
                matched_selector=matched_selector,
                attempts=attempts,
            )
            self._entries[(model_id, fingerprint, rule_id)] = entry
            return entry

    def entries_for_model(self, model_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [
                e.as_dict() for e in self._entries.values()
                if e.model_id == int(model_id)
            ]

    def stale_entries(self, model_id: int | None = None) -> list[LocatorCacheEntry]:
        """返回已失效条目（可按页面模型过滤），供增量重试只处理失效元素。"""
        with self._lock:
            return [
                e for e in self._entries.values()
                if e.stale
                and (model_id is None or e.model_id == int(model_id))
            ]

    def latest_for_rule(self, model_id: int, rule_id: int) -> LocatorCacheEntry | None:
        with self._lock:
            candidates = [
                e for e in self._entries.values()
                if e.model_id == int(model_id) and e.rule_id == rule_id
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.updated_at)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries.values())
        return {
            "total": len(entries),
            "valid": sum(1 for e in entries if not e.stale),
            "stale": sum(1 for e in entries if e.stale),
            "by_outcome": {
                outcome: sum(1 for e in entries if e.outcome == outcome)
                for outcome in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT, OUTCOME_MISS, OUTCOME_CONFLICT)
            },
        }

    def reset(self) -> None:
        """清空缓存与结构观测状态（测试与人工重置用）。"""
        with self._lock:
            self._entries.clear()
            self._model_state.clear()


# ---------------------------------------------------------------------------
# 执行记录台账
# ---------------------------------------------------------------------------

class LocatorExecutionLedger:
    """定位策略执行记录数据集：运行时会话和定位工作台共用一份历史。"""

    def __init__(self, max_records: int = 1000) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._records: list[dict[str, Any]] = []
        self._max_records = max_records

    def add(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            item = {"record_id": self._seq, "created_at": _now(), **record}
            self._records.append(item)
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]
            return item

    def records(self, *, page_model_id: int | None = None,
                window: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._records)
        if page_model_id is not None:
            rows = [r for r in rows if r.get("page_model_id") == int(page_model_id)]
        if window is not None:
            rows = rows[-max(0, window):]
        return rows

    def latest_success(self, page_model_id: int | None, rule_id: int) -> dict[str, Any] | None:
        for record in reversed(self.records(page_model_id=page_model_id)):
            if record.get("rule_id") != rule_id:
                continue
            if record.get("outcome") in (OUTCOME_HIT, OUTCOME_FALLBACK_HIT) \
                    and not record.get("stale"):
                return record
        return None

    def latest_conflict(self, page_model_id: int | None, rule_id: int) -> dict[str, Any] | None:
        for record in reversed(self.records(page_model_id=page_model_id)):
            if record.get("rule_id") == rule_id and record.get("outcome") == OUTCOME_CONFLICT:
                return record
        return None

    def snapshot(self) -> dict[str, Any]:
        records = self.records()
        by_status: dict[int, int] = {}
        for record in records:
            code = int(record.get("status_code", 0))
            by_status[code] = by_status.get(code, 0) + 1
        return {
            "total": len(records),
            "window": 50,
            "by_status": {
                code: {"label": PROCESS_STATUS.get(code, f"状态{code}"), "count": count}
                for code, count in sorted(by_status.items())
            },
            "conflicts": sum(1 for r in records if r.get("outcome") == OUTCOME_CONFLICT),
            "stale_published": sum(1 for r in records if r.get("stale_published")),
        }

    def reset(self) -> None:
        """清空执行台账（测试与人工重置用）。"""
        with self._lock:
            self._records.clear()
            self._seq = 0


# ---------------------------------------------------------------------------
# 并发首胜闸门
# ---------------------------------------------------------------------------

class FirstSuccessGate:
    """并发执行只接受首个成功结果；后到的成功记录标记为并发落败。

    以 ``round`` 划定并发窗口：重叠执行的调用共享同一批认领，
    串行执行的下一轮会重新开始，因此正常重试不会永远被判为落败。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed: dict[str, dict[str, Any]] = {}
        self._active_rounds = 0

    @contextmanager
    def round(self) -> Iterator[None]:
        with self._lock:
            self._active_rounds += 1
            if self._active_rounds == 1:
                self._claimed.clear()
        try:
            yield
        finally:
            with self._lock:
                self._active_rounds = max(0, self._active_rounds - 1)

    def claim(self, key: str, owner: str) -> tuple[bool, dict[str, Any] | None]:
        with self._lock:
            # 不在并发窗口内（串行调用 / 单次重试）一律接受，避免上一轮认领误伤。
            if self._active_rounds == 0:
                return True, None
            existing = self._claimed.get(key)
            if existing is not None:
                return False, existing
            self._claimed[key] = {"owner": owner, "claimed_at": _now()}
            return True, None

    def reset(self) -> None:
        with self._lock:
            self._claimed.clear()


# 全局单例：缓存与台账属于进程级基础设施，运行时/工作台共享。
locator_cache = LocatorCache()
locator_ledger = LocatorExecutionLedger()
first_success_gate = FirstSuccessGate()
