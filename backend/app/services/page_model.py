"""页面建模领域服务 — 源端结构定义与发布。

数据工程核心：数据标准化与字段映射转换 — 跨模块数据产物

数据流:
    page_model (定义源端结构) → channel("rpa.source_configured")
    → data_extract 消费 → channel("rpa.data_collected")
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.locator_cache import (
    locator_cache,
    parse_selector_boundaries,
    summarize_page_model,
)
from app.store import store


class PageModelService(BaseModuleService):
    MODULE = "page_model"
    ENTITY = "页面模型"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["模型名称", "目标站点", "页面结构", "动态元素", "分页模式", "加载策略", "版本号"]
    REQUIRED_FIELDS = ["模型名称", "目标站点", "页面结构"]
    STATUS_ORDER = ["草稿", "已验证", "已使用", "结构失效"]
    ACTION_RULES = {"验证模型": "草稿", "标记失效": "已验证"}
    NEGATIVE_ACTIONS = []

    # ---- 跨模块业务方法 (source_config 角色) ----

    def publish_source_config(self, model_id: int) -> dict[str, Any] | None:
        """发布页面模型作为源端配置，供 data_extract 消费。

        将页面模型（目标站点、结构、分页模式、动态元素）封装为
        源端配置写入 channel，触发下游数据提取服务。

        Args:
            model_id: 页面模型 ID

        Returns:
            channel 消息概要或 None
        """
        entry = store.find(self.MODULE, model_id)
        if entry is None:
            return None

        current = entry.get("status")
        if current not in ("已验证", "草稿"):
            return None

        entry["status"] = "已使用"
        entry["pending"] = False

        # 构造源端配置
        source_config = {
            "model_id": model_id,
            "model_name": entry.get("模型名称"),
            "target_site": entry.get("目标站点"),
            "page_structure": entry.get("页面结构"),
            "dynamic_elements": entry.get("动态元素", ""),
            "pagination_mode": entry.get("分页模式", "none"),
            "load_strategy": entry.get("加载策略", "lazy"),
            "version": entry.get("版本号", "1.0"),
            "fields_to_extract": self._infer_extractable_fields(entry),
        }

        msg = store.channel.publish(
            topic="rpa.source_configured",
            source=self.MODULE,
            payload=source_config,
        )
        return {
            "model_id": model_id,
            "status": "已使用",
            "msg_id": msg.msg_id,
            "target_site": source_config["target_site"],
            "inferred_fields": len(source_config["fields_to_extract"]),
        }

    def _infer_extractable_fields(self, entry: dict[str, Any]) -> list[str]:
        """从页面结构推断可提取字段（基于结构描述关键词）。"""
        structure = entry.get("页面结构", "") + entry.get("动态元素", "")
        field_keywords = {
            "表格": ["序号", "名称", "状态", "金额", "日期"],
            "列表": ["标题", "链接", "摘要", "时间"],
            "表单": ["字段名", "字段值", "类型", "必填"],
            "详情": ["标题", "内容", "标签", "发布日期"],
        }
        for keyword, fields in field_keywords.items():
            if keyword in structure:
                return fields
        return ["数据项1", "数据项2", "数据项3"]  # 默认字段

    def page_structure_summary(self, model_id: int) -> dict[str, Any] | None:
        """读取页面结构摘要并同步缓存指纹状态。

        结构指纹变化时，该模型下所有失效选择器缓存立即作废，
        返回结果里的 ``structure_changed`` 供运行时决定是否批量重解析。
        """
        entry = store.find(self.MODULE, model_id)
        if entry is None:
            return None
        observed = locator_cache.observe(entry)
        return {
            **observed,
            "boundaries": parse_selector_boundaries(
                entry.get("页面结构", ""), entry.get("动态元素", "")),
        }

    def fingerprint_of(self, model_id: int) -> str | None:
        entry = store.find(self.MODULE, model_id)
        if entry is None:
            return None
        return summarize_page_model(entry)["fingerprint"]

    def list_structure_fingerprints(self) -> list[dict[str, Any]]:
        """列出全部页面模型的结构指纹，供定位工作台核对。"""
        return [
            {
                "model_id": model.get("id"),
                "model_name": model.get("模型名称"),
                "target_site": model.get("目标站点"),
                "fingerprint": summarize_page_model(model)["fingerprint"],
                "status": model.get("status"),
                "cache_state": locator_cache.state_for(model.get("id")),
            }
            for model in store.rows(self.MODULE)
        ]

    def list_active_models(self) -> list[dict[str, Any]]:
        """查询所有已使用状态的页面模型。"""
        rows = store.rows(self.MODULE)
        return [
            {
                "model_id": r.get("id"), "name": r.get("模型名称"),
                "site": r.get("目标站点"), "structure": r.get("页面结构"),
                "pagination": r.get("分页模式"), "version": r.get("版本号"),
            }
            for r in rows if r.get("status") == "已使用"
        ]

    def validate_model_structure(self, model_id: int) -> dict[str, Any]:
        """验证页面模型结构完整性，标记潜在问题。

        Returns:
            {"model_id": int, "valid": bool, "issues": [...]}
        """
        entry = store.find(self.MODULE, model_id)
        if entry is None:
            return {"model_id": model_id, "valid": False, "issues": ["模型不存在"]}

        issues = []
        if not entry.get("目标站点", "").strip():
            issues.append("目标站点为空")
        if not entry.get("页面结构", "").strip():
            issues.append("页面结构未定义")
        if entry.get("分页模式", "none") == "none":
            issues.append("分页模式未配置")

        return {
            "model_id": model_id,
            "model_name": entry.get("模型名称"),
            "valid": len(issues) == 0,
            "issues": issues,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        active = self.list_active_models()
        channel_events = store.channel.snapshot(topic="rpa.source_configured")
        return {
            **base,
            "active_models": len(active),
            "source_config_events": len(channel_events),
        }