"""元素定位领域服务 — 管理 UI 元素的定位策略和选择器规则。

数据工程核心：数据标准化与字段映射转换 — 跨模块数据产物

数据流:
    element_selector (管理定位策略) → channel("rpa.locator_strategies")
    → runtime_session 消费执行自动化操作
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from app.services.base import BaseModuleService
from app.services.locator_cache import (
    locator_cache,
    parse_selector_boundaries,
    summarize_page_model,
    summarize_strategy,
)
from app.store import store


class LocatorType(Enum):
    CSS = "css"
    XPATH = "xpath"
    ID = "id"
    TEXT = "text"
    ATTRIBUTE = "attribute"


class ElementSelectorService(BaseModuleService):
    MODULE = "element_selector"
    ENTITY = "定位规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "选择器类型", "定位表达式", "匹配策略", "等待条件", "回退方案", "所属流程"]
    REQUIRED_FIELDS = ["规则名称", "选择器类型", "定位表达式"]
    STATUS_ORDER = ["草稿", "已测试", "已启用", "定位失效"]
    ACTION_RULES = {"测试定位": "草稿", "启用规则": "已测试"}
    NEGATIVE_ACTIONS = []

    # ---- 跨模块业务方法 (element_locator 角色) ----

    def publish_locator_strategy(self, rule_id: int) -> dict[str, Any] | None:
        """发布定位规则作为结构化策略，供 runtime_session 消费。

        将定位规则（选择器类型、表达式、匹配策略、回退方案）封装为
        结构化策略写入 channel。策略同时携带页面结构指纹与可复用选择器边界：
        运行时据此判断结构是否变化、哪些候选路径仍有效，
        结构未变时不再重新扫描整组选择器。

        Returns:
            发布结果概要或 None
        """
        entry = store.find(self.MODULE, rule_id)
        if entry is None:
            return None

        current = entry.get("status")
        if current not in ("已测试", "草稿"):
            return None

        entry["status"] = "已启用"
        entry["pending"] = True

        strategy = self.build_locator_strategy(entry)

        msg = store.channel.publish(
            topic="rpa.locator_strategies",
            source=self.MODULE,
            payload=strategy,
        )
        return {
            "rule_id": rule_id,
            "status": "已启用",
            "msg_id": msg.msg_id,
            "selector_type": strategy["selector_type"],
            "page_model_id": strategy["page_model_id"],
            "page_fingerprint": strategy["page_fingerprint"],
            "boundary_count": len(strategy["boundaries"]),
        }

    def build_locator_strategy(self, rule: dict[str, Any]) -> dict[str, Any]:
        """构造定位策略载荷：规则字段 + 页面结构摘要 + 选择器边界。

        关联页面模型时以模型的结构指纹为准；未关联时退化为合成摘要，
        保证旧版规则（没有页面模型引用）仍然可以发布和执行。
        """
        model_id = self.resolve_page_model_id(rule)
        model = store.find("page_model", model_id) if model_id is not None else None
        if model is not None:
            summary = summarize_page_model(model)
            # 有关联页面模型时，可复用边界只取自页面结构，
            # 不能把规则自己的表达式混进来，否则任何选择器都会“命中”自己。
            boundaries = parse_selector_boundaries(
                summary["structure"], summary["dynamic_elements"])
        else:
            summary = summarize_strategy({
                "expression": rule.get("定位表达式", ""),
                "fallback": rule.get("回退方案", ""),
                "selector_type": rule.get("选择器类型", "css"),
                "bot_ref": rule.get("所属流程", ""),
            })
            # 旧版规则没有页面模型上下文，只能以策略自身作为兼容边界。
            boundaries = parse_selector_boundaries(
                rule.get("定位表达式", ""), rule.get("回退方案", ""))
        return {
            "rule_id": rule.get("id"),
            "rule_name": rule.get("规则名称"),
            "selector_type": rule.get("选择器类型", "css"),
            "expression": rule.get("定位表达式", ""),
            "match_strategy": rule.get("匹配策略", "first_match"),
            "wait_condition": rule.get("等待条件", "visible"),
            "fallback": rule.get("回退方案", ""),
            "bot_ref": rule.get("所属流程", ""),
            "page_model_id": summary["model_id"],
            "page_model_name": summary["model_name"],
            "page_fingerprint": summary["fingerprint"],
            "page_summary": summary["summary"],
            "boundaries": boundaries,
            "boundary_count": len(boundaries),
            "structure_changed": bool(
                locator_cache.state_for(summary["model_id"]).get("structure_changed")
            ),
        }

    def resolve_page_model_id(self, rule: dict[str, Any]) -> int | None:
        """确定规则所属页面模型：优先显式引用，其次按“所属流程”匹配模型名。"""
        explicit = rule.get("page_model_id")
        if explicit not in (None, ""):
            try:
                return int(explicit)
            except (TypeError, ValueError):
                pass
        bot_ref = str(rule.get("所属流程", "")).strip()
        if not bot_ref:
            return None
        for model in store.rows("page_model"):
            name = str(model.get("模型名称", ""))
            if name and (bot_ref in name or name in bot_ref):
                return int(model.get("id"))
        return None

    def resolve_element(self, element_spec: str,
                         page_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """解析指定元素的定位策略。

        根据元素名称或表达式，从已启用的定位规则中查找推荐策略。

        Args:
            element_spec: 元素标识（如 "提交按钮", "结果表格" 等）
            page_context: 页面上下文信息

        Returns:
            {"found": bool, "strategies": [...], "recommended": dict | None}
        """
        rows = store.rows(self.MODULE)
        active = [
            r for r in rows if r.get("status") == "已启用"
        ]

        matches = []
        for rule in active:
            rule_name = rule.get("规则名称", "")
            expression = rule.get("定位表达式", "")
            if (element_spec and element_spec in rule_name) or \
               (expression and element_spec in expression):
                matches.append({
                    "rule_id": rule["id"],
                    "name": rule_name,
                    "selector_type": rule.get("选择器类型"),
                    "expression": expression,
                    "match_strategy": rule.get("匹配策略"),
                    "wait_condition": rule.get("等待条件"),
                    "fallback": rule.get("回退方案"),
                })

        return {
            "found": len(matches) > 0,
            "strategies": matches,
            "recommended": matches[0] if matches else None,
        }

    def batch_resolve_elements(self, elements: list[str],
                                bot_id: int | None = None) -> dict[str, Any]:
        """批量解析多个元素的定位策略。

        供 runtime_session 在启动前批量获取所需元素的定位信息。

        Args:
            elements: 元素名称列表
            bot_id: 关联的流程 ID（用于过滤相关策略）

        Returns:
            {"total": int, "resolved": int, "results": [...]}
        """
        resolved = []
        for elem in elements:
            result = self.resolve_element(elem)
            resolved.append({
                "element": elem,
                "found": result["found"],
                "strategy": result["recommended"],
            })

        # 发布批量解析结果到 channel
        if resolved:
            store.channel.publish(
                topic="rpa.elements_resolved",
                source=self.MODULE,
                payload={
                    "bot_id": bot_id,
                    "total": len(elements),
                    "resolved_count": sum(1 for r in resolved if r["found"]),
                    "results": resolved,
                },
            )

        return {
            "total": len(elements),
            "resolved": sum(1 for r in resolved if r["found"]),
            "results": resolved,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        active_rules = sum(
            1 for r in store.rows(self.MODULE) if r.get("status") == "已启用")
        return {
            **base,
            "active_locator_rules": active_rules,
            "strategies_published": len(
                store.channel.snapshot(topic="rpa.locator_strategies")),
        }