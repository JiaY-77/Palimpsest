"""Tests for the thin Hermes context-engine adapter (``hermes-plugin/context_engine.py``).

After the lifecycle refactor the graph-enhancement *decision* — which topics to
query, how many hits to take, how the injected block is formatted — lives in the
Palimpsest core (``core/strategy.py`` behind ``POST /lifecycle/context-enhance``).
This adapter only reads config, forwards the compression event, and merges the
returned ``enhancement_text`` into ``memory_context``.

Pinned here:

  * ``_graph_enhancement`` -> POST /lifecycle/context-enhance, returns enhancement_text
  * ``compress`` merges the enhancement into memory_context (and leaves it alone otherwise)
  * fail-open: a backend error yields "" so compression proceeds untouched
  * boundary guard: the topic-extraction / formatting decisions are gone from the adapter
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Fake `agent.context_compressor` so the plugin imports standalone.
# ---------------------------------------------------------------------------

_fake_agent = types.ModuleType("agent")
_fake_agent.__path__ = []  # make it a package


class _FakeContextCompressor:
    """Minimal stand-in: records the memory_context it was handed."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.protect_first_n = 3
        self.protect_last_n = 6
        self.last_memory_context: str | None = None

    def compress(
        self,
        messages: list[dict],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict]:
        self.last_memory_context = memory_context
        return [{"role": "system", "content": memory_context}]


_fake_cc = types.ModuleType("agent.context_compressor")
_fake_cc.ContextCompressor = _FakeContextCompressor  # type: ignore[attr-defined]

sys.modules.setdefault("agent", _fake_agent)
sys.modules.setdefault("agent.context_compressor", _fake_cc)

_ENGINE_PATH = Path(__file__).resolve().parent.parent / "hermes-plugin" / "context_engine.py"

_spec = importlib.util.spec_from_file_location("palimpsest_context_engine", _ENGINE_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

PalimpsestContextEngine = _mod.PalimpsestContextEngine


class _CapturingEngine(PalimpsestContextEngine):
    """Engine that records request payloads instead of doing HTTP."""

    def __init__(self, response: dict | None = None, **env: str) -> None:
        self.sent: list[tuple[str, dict]] = []
        self._response = response if response is not None else {}
        with patch.dict("os.environ", env, clear=False):
            super().__init__()

    def _http_post(self, url: str, payload: dict, timeout: float = 4.0) -> dict:  # type: ignore[override]
        self.sent.append((url, payload))
        return self._response


# ---------------------------------------------------------------------------
# Boundary guard: decisions live in the core, not the adapter.
# ---------------------------------------------------------------------------


class TestContextEngineHasNoDecisionLogic:
    def test_topic_extraction_moved_to_core(self):
        assert not hasattr(_mod, "_extract_topics"), "topic extraction must live in core/strategy.py"

    def test_injection_template_moved_to_core(self):
        src = _ENGINE_PATH.read_text(encoding="utf-8")
        assert "/lifecycle/context-enhance" in src, "adapter must forward to the core endpoint"
        # 注入文本的排版属于策略；这些模板词不得回流到适配器。
        assert "主题「" not in src
        assert "图谱关联" not in src


# ---------------------------------------------------------------------------
# _graph_enhancement -> /lifecycle/context-enhance
# ---------------------------------------------------------------------------


class TestGraphEnhancementForwarding:
    def test_posts_payload_and_returns_text(self):
        engine = _CapturingEngine(response={"enhancement_text": "[Palimpsest 图谱要点]\n- x"})
        msgs = [{"role": "user", "content": "项目代号是什么"}]
        out = engine._graph_enhancement(msgs, None)
        assert out == "[Palimpsest 图谱要点]\n- x"
        url, payload = engine.sent[0]
        assert url.endswith("/lifecycle/context-enhance")
        assert payload["messages"] == msgs
        assert payload["protect_first_n"] == 3
        assert payload["protect_last_n"] == 6

    def test_empty_messages_skip_request(self):
        engine = _CapturingEngine()
        assert engine._graph_enhancement([], None) == ""
        assert engine.sent == []

    def test_focus_topic_and_domain_forwarded(self):
        engine = _CapturingEngine(response={"enhancement_text": "x"}, PALIMPSEST_DOMAIN="novel")
        engine._graph_enhancement([{"role": "user", "content": "hi"}], "手动主题")
        _, payload = engine.sent[0]
        assert payload["focus_topic"] == "手动主题"
        assert payload["domain"] == "novel"

    def test_max_topics_from_env_forwarded(self):
        engine = _CapturingEngine(response={"enhancement_text": "x"}, PALIMPSEST_GRAPH_TOPICS="4")
        engine._graph_enhancement([{"role": "user", "content": "hi"}], None)
        assert engine.sent[0][1]["max_topics"] == 4

    def test_error_response_yields_empty(self):
        engine = _CapturingEngine(response={"error": "boom"})
        assert engine._graph_enhancement([{"role": "user", "content": "hi"}], None) == ""


# ---------------------------------------------------------------------------
# compress() merges the enhancement into memory_context
# ---------------------------------------------------------------------------


class TestCompressMergesEnhancement:
    def test_enhancement_appended_to_memory_context(self):
        engine = _CapturingEngine(response={"enhancement_text": "[Palimpsest 图谱要点（压缩前提炼）]\n- x"})
        out = engine.compress([{"role": "user", "content": "hi"}], memory_context="原有上下文")
        assert engine.last_memory_context is not None
        assert engine.last_memory_context.startswith("原有上下文")
        assert "Palimpsest 图谱要点" in engine.last_memory_context
        assert out[0]["content"] == engine.last_memory_context

    def test_no_enhancement_keeps_memory_context_untouched(self):
        engine = _CapturingEngine(response={"enhancement_text": ""})
        engine.compress([{"role": "user", "content": "hi"}], memory_context="原有上下文")
        assert engine.last_memory_context == "原有上下文"

    def test_backend_error_still_compresses(self):
        engine = _CapturingEngine(response={"error": "unreachable"})
        out = engine.compress([{"role": "user", "content": "hi"}], memory_context="原有上下文")
        assert engine.last_memory_context == "原有上下文"
        assert out == [{"role": "system", "content": "原有上下文"}]
