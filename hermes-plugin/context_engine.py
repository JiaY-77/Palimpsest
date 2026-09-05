"""Palimpsest 图谱压缩引擎 — 继承内置 ContextCompressor，压缩前用 Palimpsest 图谱提炼关键链。

不重写压缩逻辑（内置 ContextCompressor 成熟稳定：should_compress 阈值 /
protect_first_n / protect_last_n / LLM 总结），只做图谱增强——

compress() 时：
  1. 从将被压缩的消息提取主题（focus_topic 优先 + 中间段 user 消息）
  2. 调 Palimpsest /mem/search（include_neighbors=True）拿关键记忆 + 图谱关联
  3. 组装「Palimpsest 图谱要点」合并进 memory_context（内置压缩器会把
     memory_context 作为 <memory-provider-context> 注入总结 prompt）
  4. 调 super().compress(..., memory_context=enhanced)

fail-open：Palimpsest 不可达/超时/报错 → 原样压缩（图谱增强是增量，不阻塞主线）。
prompt caching 红线：压缩本身是内置例外路径，我们只增强 memory_context 文本，
不改变消息结构。

配置（环境变量，可选）：
  PALIMPSEST_BASE_URL        默认 http://127.0.0.1:8090
  PALIMPSEST_DOMAIN          默认 hermes
  PALIMPSEST_GRAPH_TOPICS    图谱主题数（默认 3，最多 5）
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional

from agent.context_compressor import ContextCompressor

logger = logging.getLogger(__name__)


class PalimpsestContextEngine(ContextCompressor):
    """Palimpsest 图谱压缩引擎：内置压缩 + 压缩前图谱关键链提炼。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._base_url = os.environ.get(
            "PALIMPSEST_BASE_URL", "http://127.0.0.1:8090"
        ).rstrip("/")
        self._domain = os.environ.get("PALIMPSEST_DOMAIN", "hermes")
        self._max_topics = max(1, min(int(os.environ.get("PALIMPSEST_GRAPH_TOPICS", "3")), 5))
        # 图谱增强总耗时预算（秒）：后端不可达时整体 fail-open，不让压缩链路白等
        self._graph_timeout_budget = float(
            os.environ.get("PALIMPSEST_GRAPH_TIMEOUT", "8.0"))
        self._graph_enhance_errors = 0

    @property
    def name(self) -> str:
        return "palimpsest-graph"

    # -- 图谱增强 -----------------------------------------------------

    def _http_post(self, url: str, payload: dict, timeout: float = 4.0) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("Palimpsest graph REST %s failed: %s", url, exc)
            return {"error": str(exc)}

    def _extract_topics(self, messages: List[Dict[str, Any]], focus_topic: Optional[str]) -> List[str]:
        """从将被压缩的消息中提取图谱查询主题。

        优先 focus_topic（手动 /compress <focus>）；否则取中间段 user 消息
        （跳过开头 protect_first_n 与结尾 protect_last_n 保护段，那些不压缩）。
        内容 < 8 字符的寒暄跳过。
        """
        topics: List[str] = []
        if focus_topic and str(focus_topic).strip():
            topics.append(str(focus_topic).strip())

        head = getattr(self, "protect_first_n", 3) or 3
        tail = getattr(self, "protect_last_n", 6) or 6
        # 消息太短（没有真正的中间段可压缩）→ 无图谱提炼
        if len(messages) - tail <= head:
            return []
        candidates = []
        for idx, m in enumerate(messages):
            if m.get("role") != "user":
                continue
            if idx < head or idx >= max(0, len(messages) - tail):
                continue  # 保护段不参与图谱提炼
            content = str(m.get("content") or "").strip()
            if len(content) >= 8:
                candidates.append(content)
        # 取最近的意图（靠后的优先），补足主题数
        for content in reversed(candidates):
            if len(topics) >= self._max_topics:
                break
            topics.append(content[:120])
        return topics[: self._max_topics]

    def _graph_enhancement(self, messages: List[Dict[str, Any]], focus_topic: Optional[str]) -> str:
        """压缩前调 Palimpsest 图谱提炼关键链，返回注入文本；失败/无主题返回空串。

        总耗时预算：整体链路的多次串行 POST 受 _graph_timeout_budget 约束，
        超预算即停止后续 POST（fail-open，绝不让 Palimpsest 拖垮 Hermes 压缩）。
        """
        topics = self._extract_topics(messages, focus_topic)
        if not topics:
            return ""
        lines = ["[Palimpsest 图谱要点（压缩前提炼）]"]
        added = 0
        deadline = time.monotonic() + max(0.1, self._graph_timeout_budget)
        for topic in topics:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            resp = self._http_post(f"{self._base_url}/mem/search", {
                "query": topic, "scope": "all", "domain": self._domain,
                "top_k": 2, "include_neighbors": True,
            }, timeout=min(4.0, remaining))
            if "error" in resp or not resp.get("results"):
                continue
            top = resp["results"][0]
            lines.append(
                f"- 主题「{topic[:50]}」→ 关键记忆({top.get('score', 0):.2f}): "
                f"{str(top.get('summary', ''))[:120]}"
            )
            neighbors = resp.get("neighbors") or []
            for nb in neighbors[:3]:
                lines.append(
                    f"  · 图谱关联: {nb.get('relation', 'LINKED')} → {str(nb.get('title', ''))[:80]}"
                )
            added += 1
            if added >= self._max_topics:
                break
        return "\n".join(lines) if added else ""

    # -- 主入口 -------------------------------------------------------

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        """内置压缩 + 图谱增强：把 Palimpsest 提炼的关键链合并进 memory_context。"""
        try:
            enhancement = self._graph_enhancement(messages, focus_topic)
            if enhancement:
                memory_context = (
                    (memory_context + "\n\n") if memory_context else ""
                ) + enhancement
        except Exception as exc:  # noqa: BLE001 — fail-open，不阻塞压缩
            self._graph_enhance_errors += 1
            logger.warning("Palimpsest graph enhancement failed (fail-open): %s", exc)
        return super().compress(
            messages,
            current_tokens=current_tokens,
            focus_topic=focus_topic,
            force=force,
            memory_context=memory_context,
        )
