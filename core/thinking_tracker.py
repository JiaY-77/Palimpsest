"""
思维链捕获器
专门解析 preset 强制生成的 <thinking> 标签内容，并提取结构化记忆
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
        "diverse_expression": r"【多样表达】\s*(.*?)(?=【|$)",
    }

    def parse_thinking(self, thinking_text: str) -> dict[str, Any]:
        """解析 <thinking> 块，返回结构化数据"""
        result = {}
        for module_name, pattern in self.MODULE_PATTERNS.items():
            match = re.search(pattern, thinking_text, re.DOTALL)
            if match:
                raw_text = match.group(1).strip()
                result[module_name] = self._clean_thinking_block(raw_text)
        return result

    def _clean_thinking_block(self, raw_text: str) -> str:
        """清洗 thinking 块，去除残留标签和无关内容"""
        text = raw_text.strip()
        # 去除残留的XML标签
        text = re.sub(r"</?[A-Za-z\u4e00-\u9fff]+>", "", text)
        text = re.sub(r"</[^>]*>", "", text)
        # 压缩多余空白
        text = re.sub(r"[\r\n]{2,}", "\n", text)
        return text.strip()

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

        # 2. 角色分析 → 角色状态节点 (每个角色独立)
        if "characters" in parsed:
            char_nodes = self._extract_characters(parsed["characters"])
            nodes.extend(char_nodes)

        # 3. 剧情步骤 → 剧情计划节点 (每步独立)
        if "plot_steps" in parsed:
            step_nodes = self._extract_plot_steps(parsed["plot_steps"])
            nodes.extend(step_nodes)

        # 4. 用户意图 → 意图节点
        if "user_intent" in parsed:
            intent = self._extract_user_intent(parsed["user_intent"])
            if intent:
                nodes.append(intent)

        return nodes

    def _extract_characters(self, character_block: str) -> list[dict[str, Any]]:
        """从角色块中提取每个独立角色的信息，过滤掉分类标题行"""
        nodes = []
        # 先按 "每个在场角色..." 这种标题行分割
        parts = re.split(r"每个在场角色\w*[身份/性格/关系]是[：:]", character_block)

        for part in parts:
            lines = part.strip().split("\n")
            for i, line in enumerate(lines):
                line = line.lstrip("- ").strip()
                # 过滤掉分类标题行和过短的行
                if not line or len(line) < 5:
                    continue
                if (
                    "每个在场角色" in line
                    or "关系是：" in line
                    or "身份/性格是：" in line
                ):
                    continue
                # 如果下一行还是内容（不是新角色），合并进来
                if (
                    i + 1 < len(lines)
                    and not lines[i + 1].startswith("-")
                    and ":" in lines[i + 1]
                ):
                    continue
                nodes.append(
                    {
                        "type": "character_state",
                        "label": line[:50],
                        "content": line,
                        "importance": 0.9,
                    }
                )

        return nodes

    def _extract_plot_steps(self, plot_block: str) -> list[dict[str, Any]]:
        """从剧情模块中提取每一步剧情计划"""
        nodes = []

        # 移除 Meta 指令
        plot_block = re.sub(r"Nana将注意.*?$", "", plot_block, flags=re.MULTILINE)
        plot_block = re.sub(
            r"Nana以用户发送.*?$", "", plot_block, flags=re.MULTILINE
        ).strip()

        # 匹配序号开头的行：1. 2. 3. 或 1) 2) 3) 或 1、 2、 3、
        steps = re.findall(
            r"(?:\d+[\.\)、．]\s*)(.*?)(?=(?:\d+[\.\)、．])|$)", plot_block, re.DOTALL
        )

        for step in steps:
            content = step.strip()
            if content and len(content) > 5:
                nodes.append(
                    {
                        "type": "plot_plan",
                        "label": content[:50],
                        "content": content,
                        "importance": 0.7,
                    }
                )

        return nodes

    def _extract_user_intent(self, intent_block: str) -> dict[str, Any] | None:
        """提取用户意图的核心内容"""
        if not intent_block:
            return None
        # 去掉冗长的前缀描述
        core = re.sub(r'用户发送的最新内容是[：:]\s*"[^"]*"', "", intent_block)
        core = re.sub(r"用户需求字数[：:]\s*\S+", "", core)
        core = core.strip()

        if core:
            return {
                "type": "user_intent",
                "label": core[:50],
                "content": core,
                "importance": 0.85,
            }
        return None
