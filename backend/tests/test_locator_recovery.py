"""定位缓存与失败恢复回归测试。

覆盖需求要点：
1. 结构未变时重复执行命中缓存、探测次数为 0（不重复扫描整组选择器）。
2. 失效选择器与页面结构指纹一起缓存，结构变化后缓存立即失效。
3. 结构变化且旧回退仍命中时落到 12 号终态，旧命中不发布。
4. 重试只处理仍失效的元素，每个元素至多探测主策略 + 回退。
5. 处理项展开按约定数据集去重，并携带指纹与候选路径。
6. 窗口历史重算可区分可复用历史与失效历史。
7. 并发执行只接受首个成功结果。
8. 既有版本边界（未绑定页面模型的旧版策略）仍可执行。
"""
from __future__ import annotations

import sys
import threading
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.seed import SEED_ROWS
from app.services.element_selector import ElementSelectorService
from app.services.locator_cache import (
    OUTCOME_CONFLICT,
    PROCESS_STATUS,
    first_success_gate,
    locator_cache,
    locator_ledger,
    summarize_page_model,
)
from app.services.page_model import PageModelService
from app.services.runtime_session import runtime_session
from app.store import store


MODEL_STRUCTURE = (
    "div.goods-list #goods-table .goods-row .goods-name .price .title "
    "button[data-action=next]"
)


@pytest.fixture(autouse=True)
def clean_state():
    """每个用例都恢复干净的缓存 / 台账 / 运行时状态与示例数据。"""
    locator_cache.reset()
    locator_ledger.reset()
    runtime_session.reset()
    store._tables["element_selector"] = deepcopy(SEED_ROWS["element_selector"])
    store._tables["page_model"] = deepcopy(SEED_ROWS["page_model"])
    yield
    locator_cache.reset()
    locator_ledger.reset()
    runtime_session.reset()
    store._tables["element_selector"] = deepcopy(SEED_ROWS["element_selector"])
    store._tables["page_model"] = deepcopy(SEED_ROWS["page_model"])


@pytest.fixture()
def page():
    model = store.rows("page_model")[0]
    model.update({
        "模型名称": "商品列表页",
        "目标站点": "shop.example.com",
        "页面结构": MODEL_STRUCTURE,
        "动态元素": ".goods-row .lazy-placeholder",
        "分页模式": "click-next",
        "加载策略": "lazy",
        "版本号": "2.0",
    })
    rule = store.rows("element_selector")[0]
    rule.update({
        "规则名称": "商品名称",
        "选择器类型": "css",
        "定位表达式": "#goods-table .goods-row .goods-name",
        "匹配策略": "first_match",
        "等待条件": "visible",
        "回退方案": ".goods-row .title",
        "所属流程": "商品列表页",
        "page_model_id": model["id"],
        "status": "已启用",
    })
    return model, rule


def _strategy(rule) -> dict:
    return ElementSelectorService().build_locator_strategy(rule)


class TestFingerprintCache:
    def test_fingerprint_is_stable_and_structure_sensitive(self) -> None:
        model = {"id": 1, "模型名称": "x", "页面结构": MODEL_STRUCTURE, "版本号": "1.0"}
        first = summarize_page_model(model)
        assert first["fingerprint"] == summarize_page_model(dict(model))["fingerprint"]
        changed = summarize_page_model({**model, "页面结构": "div.new #other"})
        assert changed["fingerprint"] != first["fingerprint"]

    def test_cached_hit_costs_zero_probes(self, page) -> None:
        _, rule = page
        strategy = _strategy(rule)
        first = runtime_session.execute_element(strategy, session_id="S1")
        assert first["outcome"] == "hit" and first["probes"] == 1

        second = runtime_session.execute_element(strategy, session_id="S1")
        assert second["outcome"] == "hit"
        assert second["probes"] == 0
        assert second["cached_reused"] is True
        assert second["status_code"] == 4

    def test_cache_entry_records_fingerprint(self, page) -> None:
        model, rule = page
        strategy = _strategy(rule)
        summary = PageModelService().page_structure_summary(model["id"])
        runtime_session.execute_element(strategy, session_id="S1")
        entries = locator_cache.entries_for_model(model["id"])
        assert len(entries) == 1
        assert entries[0]["fingerprint"] == summary["fingerprint"]
        assert entries[0]["stale"] is False


class TestStructureChange:
    def test_structure_change_invalidates_cache(self, page) -> None:
        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        model["页面结构"] = "div.list .order-row .order-no"
        summary = PageModelService().page_structure_summary(model["id"])
        assert summary["structure_changed"] is True
        assert summary["invalidated_entries"] == 1
        assert locator_cache.snapshot()["stale"] == 1

    def test_old_fallback_hit_becomes_conflict(self, page) -> None:
        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        # 主策略失效，但旧回退 .goods-row .title 仍在新的结构摘要里
        model["页面结构"] = (
            "div.goods-grid #goods-table-v2 .goods-row .goods-name .title .price"
        )
        record = runtime_session.execute_element(_strategy(rule), session_id="S2")
        assert record["outcome"] == OUTCOME_CONFLICT
        assert record["status_code"] == 12
        assert record["status_label"] == PROCESS_STATUS[12] == "结构冲突待重建"
        assert record["stale_published"] is False
        assert record["previous_fingerprint"]
        # 冲突结果不会写入新缓存
        assert all(entry["stale"] for entry in
                   locator_cache.entries_for_model(model["id"]))
        # 定位规则回到失效状态，与工作台一起刷新
        assert rule["status"] == "定位失效"

    def test_both_policies_keep_stale_out_of_new_records(self, page) -> None:
        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        model["页面结构"] = "div.grid #goods-table-v2 .goods-row .goods-name .title"
        for policy in ("force_rebuild", "probe_then_decide"):
            record = runtime_session.execute_element(
                _strategy(rule), session_id="S2", policy=policy)
            assert record["outcome"] == OUTCOME_CONFLICT
            assert record["stale_published"] is False
            assert record["policy"] == policy

    def test_rebuild_hit_is_accepted_after_structure_change(self, page) -> None:
        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        model["页面结构"] = "div.grid #goods-table-v2 .goods-row .sku-name .price"
        record = runtime_session.execute_element(_strategy(rule), session_id="S2")
        assert record["structure_changed"] is True
        assert record["rebuild"] is True
        assert record["stale_published"] is False
        assert record["outcome"] in ("hit", "miss")


class TestRetryOnlyInvalid:
    def test_retry_only_touches_invalid_elements(self, page) -> None:
        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")

        missing = store.rows("element_selector")[1]
        missing.update({
            "规则名称": "缺失字段",
            "定位表达式": ".not-on-page-node",
            "回退方案": ".ghost-node",
            "所属流程": "商品列表页",
            "page_model_id": model["id"],
            "status": "已启用",
        })
        miss = runtime_session.execute_element(_strategy(missing), session_id="S1")
        assert miss["outcome"] == "miss" and miss["status_code"] == 5

        boundary = runtime_session.invalid_boundaries(model["id"])
        assert [item["rule_name"] for item in boundary] == ["缺失字段"]

        result = runtime_session.retry_invalid_elements(page_model_id=model["id"])
        assert result["retried"] == 1
        assert result["scanned_whole_group"] is False
        assert {r["rule_name"] for r in result["results"]} == {"缺失字段"}
        # 每个元素至多主策略 + 回退两次探测
        assert result["probes"] <= 2

    def test_retry_noop_when_nothing_invalid(self, page) -> None:
        _, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        result = runtime_session.retry_invalid_elements()
        assert result["retried"] == 0
        assert result["scanned_whole_group"] is False

    def test_retry_recovers_and_clears_boundary(self, page) -> None:
        model, rule = page
        source = store.rows("element_selector")[2]
        source.update({
            "规则名称": "订单号",
            "定位表达式": ".order-row .not-yet",
            "回退方案": ".order-row .order-code",
            "所属流程": "订单列表页",
            "page_model_id": model["id"],
            "status": "已启用",
        })
        assert runtime_session.execute_element(
            _strategy(source), session_id="S1")["outcome"] == "miss"

        # 页面结构变化并补上候选路径：重试时按新边界重新解析
        model["页面结构"] += " .order-row .order-no"
        locator_cache.observe(model)
        source["定位表达式"] = ".order-row .order-no"
        result = runtime_session.retry_invalid_elements(page_model_id=model["id"])
        assert result["recovered"] == 1
        assert runtime_session.invalid_boundaries(model["id"]) == []
        assert source["status"] == "已启用"


class TestWorkItemAndWindow:
    def test_work_item_expansion_dedupes_and_shapes_dataset(self, page) -> None:
        from app.services.locator_workbench import locator_workbench

        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        item = {
            "work_item_id": "WI-1",
            "page_model_id": model["id"],
            "records": [
                {"rule_id": rule["id"]},
                {"rule_id": rule["id"]},
            ],
        }
        expanded = locator_workbench.expand_work_item(item)
        assert expanded["dataset"] == "定位策略执行记录数据集"
        assert expanded["record_count"] == 1
        record = expanded["records"][0]
        assert record["page_fingerprint"]
        assert [path["stage"] for path in record["candidate_paths"]] == [
            "primary", "fallback"]
        assert record["status_label"] == "缓存命中"

    def test_window_recompute_separates_reusable_history(self, page) -> None:
        from app.services.locator_workbench import locator_workbench

        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        model["版本号"] = "2.1"
        PageModelService().page_structure_summary(model["id"])
        runtime_session.execute_element(_strategy(rule), session_id="S2")

        window = locator_workbench.recompute_window(model["id"], window=10)
        assert window["scanned_whole_group"] is False
        assert window["records_examined"] >= 2
        history = {item["rule_id"]: item for item in window["history"]}
        assert history[rule["id"]]["last_fingerprint"] == window["fingerprint"]

    def test_snapshot_keeps_lists_in_sync(self, page) -> None:
        from app.services.locator_workbench import locator_workbench

        model, rule = page
        runtime_session.execute_element(_strategy(rule), session_id="S1")
        missing = store.rows("element_selector")[1]
        missing.update({
            "规则名称": "缺失字段",
            "定位表达式": ".not-on-page-node",
            "回退方案": "",
            "所属流程": "商品列表页",
            "page_model_id": model["id"],
            "status": "已启用",
        })
        runtime_session.execute_element(_strategy(missing), session_id="S1")

        snapshot = locator_workbench.snapshot(model["id"])
        panel = snapshot["panels"][0]
        assert len(panel["invalid_rules"]) == 1
        assert panel["invalid_rules"][0]["retryable"] is True
        assert len(panel["retry_boundary"]) == 1
        # 处理清单与结果列表同一份快照，数量保持一致
        assert snapshot["invalid_total"] == 1
        assert snapshot["result_total"] == len(panel["recent_executions"])


class TestConcurrencyAndCompat:
    def test_first_success_gate_rejects_later_winner(self, page) -> None:
        model, rule = page
        strategy = _strategy(rule)
        results: list[dict] = []
        lock = threading.Lock()

        def worker(owner: str) -> None:
            record = runtime_session.execute_element(strategy, session_id=owner)
            with lock:
                results.append(record)

        with first_success_gate.round():
            threads = [threading.Thread(target=worker, args=(f"T{i}",)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        accepted = [r for r in results if r["accepted"]]
        rejected = [r for r in results if not r["accepted"]]
        assert len(accepted) == 1
        assert rejected, "并发执行应只接受首个成功结果"
        assert all(r["status_code"] == 9 for r in rejected)
        # 落败结果不发布旧命中，也不会写入缓存
        assert not any(r["stale_published"] for r in results)
        assert len(locator_cache.entries_for_model(model["id"])) == 1

    def test_legacy_strategy_without_page_model_still_runs(self) -> None:
        legacy = {
            "rule_id": 99,
            "rule_name": "旧版定位",
            "selector_type": "css",
            "expression": ".legacy-node",
            "fallback": "",
            "bot_ref": "旧流程",
        }
        record = runtime_session.execute_element(legacy, session_id="L1")
        # 旧版消息没有页面模型，按合成摘要执行，保持既有版本边界可用
        assert record["page_model_id"] is None
        assert record["page_fingerprint"]
        assert record["outcome"] in ("hit", "miss")
