"""Palimpsest - 记忆提取器 (思维链版本)"""

import json
import logging
from typing import Any

from openai import OpenAI
from config import Config
from core.thinking_tracker import ThinkingTracker

logger = logging.getLogger(__name__)


class Extractor:
    """调用 LLM 从对话中提取记忆候选 (专注思维链捕获)"""

    def __init__(self) -> None:
        llm_cfg = Config.get_llm_config()
        self.client = OpenAI(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
        )
        self.model = llm_cfg["model"]
        self.tracker = ThinkingTracker()

    def extract_from_thinking(self, thinking_text: str) -> dict[str, Any]:
        """
        从 thinking 文本直接提取记忆
        这是目前 Palimpsest 最高质量的记忆来源
        """
        # 解析 thinking 中的模块
        parsed = self.tracker.parse_thinking(thinking_text)

        # 转换为节点候选
        nodes = self.tracker.to_memory_nodes(parsed)

        # 构建节点之间的边
        edges = []
        for i in range(len(nodes) - 1):
            edges.append(
                {
                    "source_label": nodes[i]["label"],
                    "target_label": nodes[i + 1]["label"],
                    "relation_type": "LEADS_TO",
                    "content": "思考链顺序",
                }
            )

        return {"nodes": nodes, "edges": edges}

    def extract_facts(
        self,
        character_name: str,
        user_name: str,
        new_message: str,
        thinking_text: str | None = None,
        recent_messages: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        提取记忆的主入口
        优先使用 thinking_text，如果不可用则调用 LLM
        """
        # 主通道：直接从 thinking 文本提取
        if thinking_text and "<thinking>" in thinking_text:
            logger.info("使用 thinking 文本直接提取记忆")
            return self.extract_from_thinking(thinking_text)

        # 备用通道：调用 LLM 分析消息文本
        logger.info("thinking 文本不可用，使用 LLM 提取记忆")
        return self._extract_via_llm(
            character_name, user_name, new_message, recent_messages
        )

    def _extract_via_llm(
        self,
        character_name: str,
        user_name: str,
        new_message: str,
        recent_messages: list[str] | None = None,
    ) -> dict[str, Any]:
        """备用方案：通过 LLM 分析消息文本提取记忆"""
        context = ""
        if recent_messages:
            context = "最近对话片段：\n" + "\n".join(recent_messages[-10:])

        prompt = f"""你是记忆提取器，分析以下内容并提取关键记忆。

当前角色：{character_name}
当前用户：{user_name}

{context}

新消息：{new_message}

请分析上述对话，提取关键事件、角色状态变化、重要物品或地点。

返回 JSON 格式：
{{
  "nodes": [
    {{
      "type": "event",
      "label": "简短标签",
      "content": "详细描述",
      "importance": 0.8
    }}
  ],
  "edges": []
}}
如果没有值得提取的内容，返回空 JSON：{{"nodes": [], "edges": []}}
"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是精确的记忆提取器，只输出JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.error(f"LLM 提取失败: {e}")
            return {"nodes": [], "edges": []}

        raw = response.choices[0].message.content
        if raw is None:
            return {"nodes": [], "edges": []}

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"JSON 解析失败: {raw}")
            return {"nodes": [], "edges": []}
