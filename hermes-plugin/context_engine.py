"""Palimpsest 图谱压缩引擎 — 继承内置 ContextCompressor，压缩前用 Palimpsest 图谱提炼关键链。

不重写压缩逻辑（内置 ContextCompressor 成熟稳定：should_compress 阈值 /
protect_first_n / protect_last_n / LLM 总结），只做图谱增强——

compress() 时调本体 ``POST /lifecycle/context-enhance``，把「将要压缩的消息 +
保护段参数 + 耗时预算」交给 Palimpsest；本体挑主题、查图谱、组装文本，返回
``enhancement_text``。本适配器把它合并进 memory_context（内置压缩器会把
memory_context 作为 <memory-provider-context> 注入总结 prompt），再调
super().compress(..., memory_context=enhanced)。

**决策全在本体**：挑哪些主题、每主题取几条、注入文本怎么排版，均由
``core/strategy.py`` 判定；本适配器只读配置 + 转发 + 合并（无阈值 / 无正则）。
检索也在本体内进程完成，省掉此前「每个主题一次 HTTP」的往返。

fail-open：Palimpsest 不可达/超时/报错 → 原样压缩（图谱增强是增量，不阻塞主线）。
prompt caching 红线：压缩本身是内置例外路径，我们只增强 memory_context 文本，
不改变消息结构。

配置（环境变量，可选）：
  PALIMPSEST_BASE_URL        默认 http://127.0.0.1:8090
  PALIMPSEST_DOMAIN          默认 hermes
  PALIMPSEST_GRAPH_TOPICS    图谱主题数（默认 3；最终上限由本体收敛到 5）
  PALIMPSEST_GRAPH_TIMEOUT   整条增强链路的耗时预算（秒，默认 8）
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from agent.context_compressor import ContextCompressor

logger = logging.getLogger(__name__)


class PalimpsestContextEngine(ContextCompressor):
    """Palimpsest 图谱压缩引擎：内置压缩 + 压缩前图谱关键链提炼。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._base_url = os.environ.get("PALIMPSEST_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
        self._domain = os.environ.get("PALIMPSEST_DOMAIN", "hermes")
        # 主题数上限由本体收敛（core.strategy.clamp_max_topics），这里只读原始配置。
        raw_topics = os.environ.get("PALIMPSEST_GRAPH_TOPICS", "3")
        try:
            self._max_topics = int(raw_topics)
        except (TypeError, ValueError):
            self._max_topics = 3
        # 图谱增强总耗时预算（秒）：后端不可达时整体 fail-open，不让压缩链路白等
        self._graph_timeout_budget = float(os.environ.get("PALIMPSEST_GRAPH_TIMEOUT", "8.0"))
        self._graph_enhance_errors = 0

    @property
    def name(self) -> str:
        return "palimpsest-graph"

    # -- 图谱增强 -----------------------------------------------------

    def _http_post(self, url: str, payload: dict, timeout: float = 4.0) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("Palimpsest graph REST %s failed: %s", url, exc)
            return {"error": str(exc)}

    def _graph_enhancement(self, messages: list[dict[str, Any]], focus_topic: str | None) -> str:
        """把「挑主题 + 查图谱 + 组装」整条决策委托给本体，返回注入文本。

        本体 ``POST /lifecycle/context-enhance`` 负责主题提取、图谱检索与排版；
        本适配器只转发输入、取回 ``enhancement_text``。
        fail-open：本体不可达/超时/报错 → 返回空串（compress() 遂原样压缩）。
        """
        if not messages:
            return ""
        resp = self._http_post(
            f"{self._base_url}/lifecycle/context-enhance",
            {
                "messages": messages,
                "focus_topic": focus_topic or "",
                "domain": self._domain,
                "protect_first_n": int(getattr(self, "protect_first_n", 3) or 3),
                "protect_last_n": int(getattr(self, "protect_last_n", 6) or 6),
                "max_topics": self._max_topics,
                "timeout_budget": self._graph_timeout_budget,
            },
            timeout=min(self._graph_timeout_budget + 5.0, 60.0),
        )
        return str(resp.get("enhancement_text") or "")

    # -- 主入口 -------------------------------------------------------

    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict[str, Any]]:
        """内置压缩 + 图谱增强：把 Palimpsest 提炼的关键链合并进 memory_context。"""
        try:
            enhancement = self._graph_enhancement(messages, focus_topic)
            if enhancement:
                memory_context = ((memory_context + "\n\n") if memory_context else "") + enhancement
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
