"""
思维链捕获器
专门解析 preset 强制生成的 <thinking> 标签内容
"""

import re
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ThinkingTracker:
    """从 AI 的 thinking 输出中提取结构化记忆"""

    # 匹配你预设中的模块标签
    MODULE_PATTERNS = {
        "plot_summary": r"【上段剧情】\s*前情提要：(.*?)(?=【|$)",
        "self_check": r"【自我检查】\s*(.*?)(?=【|$)",
        "characters": r"【角色】\s*(.*?)(?=【|$)",
        "plot_steps": r"【剧情模块】\s*(.*?)(?=【|$)",
        "user_intent": r"【用户需求】\s*(.*?)(?=【|$)",
        "status_bar": r"【状态栏】\s*(.*?)(?=【|$)",
    }

    def parse_thinking(self, thinking_text: str) -> dict[str, Any]:
        """解析 <thinking> 块，返回结构化数据"""
        result = {}
        for module_name, pattern in self.MODULE_PATTERNS.items():
            match = re.search(pattern, thinking_text, re.DOTALL)
            if match:
                result[module_name] = match.group(1).strip()
        return result

    def to_memory_nodes(self, parsed: dict[str, Any]) -> list[dict[str, Any]]:
        """将解析结果转换为记忆节点候选"""
        nodes = []

        # 1. 剧情总结 → 事件节点
        if "plot_summary" in parsed:
            nodes.append(
                {
                    "type": "event",
                    "label": parsed["plot_summary"][:50],
                    "content": parsed["plot_summary"],
                    "importance": 0.8,
                }
            )

        # 2. 角色分析 → 角色状态节点
        if "characters" in parsed:
            nodes.append(
                {
                    "type": "character_state",
                    "label": parsed["characters"][:50],
                    "content": parsed["characters"],
                    "importance": 0.9,
                }
            )

        # 3. 剧情步骤 → 剧情计划节点
        if "plot_steps" in parsed:
            nodes.append(
                {
                    "type": "plot_plan",
                    "label": parsed["plot_steps"][:50],
                    "content": parsed["plot_steps"],
                    "importance": 0.7,
                }
            )

        # 4. 用户意图 → 意图节点
        if "user_intent" in parsed:
            nodes.append(
                {
                    "type": "user_intent",
                    "label": parsed["user_intent"][:50],
                    "content": parsed["user_intent"],
                    "importance": 0.85,
                }
            )

        # 5. 状态栏 → 结构化状态节点
        if "status_bar" in parsed:
            nodes.append(
                {
                    "type": "status_update",
                    "label": parsed["status_bar"][:50],
                    "content": parsed["status_bar"],
                    "importance": 0.75,
                }
            )

        return nodes
