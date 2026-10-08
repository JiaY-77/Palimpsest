"""Tests for the thin Hermes adapter (``hermes-plugin/__init__.py``).

After the lifecycle refactor, the adapter holds **no** decision logic: it only
forwards lifecycle events to the Palimpsest本体 lifecycle protocol and returns
whatever the本体 decided. These tests pin the *transport* half:

  * ``prefetch``            -> POST /lifecycle/pre-turn      (returns inject_text)
  * ``sync_turn``           -> POST /lifecycle/post-turn
  * ``on_session_end``      -> POST /lifecycle/session-end
  * ``on_pre_compress``     -> POST /lifecycle/pre-compress  (returns points_text)
  * cron/flush sessions are skipped
  * the backend must fail open (errors never raise)

A boundary guard asserts the decision symbols are gone from the adapter — if
they ever come back, the "strategy lives in the core" invariant has regressed.

The decision-behaviour tests that used to live here moved to
``tests/test_strategy_engine.py`` (they now exercise ``core.strategy``).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Fake `agent.memory_provider` module so the plugin imports standalone.
# ---------------------------------------------------------------------------

_fake_agent = types.ModuleType("agent")
_fake_agent.__path__ = []  # make it a package

_fake_mp = types.ModuleType("agent.memory_provider")


class _FakeMemoryProvider:
    pass


class _FakeRecallStatus:
    def __init__(self, provider_label: str = "", count: int = 0) -> None:
        self.provider_label = provider_label
        self.count = count


_fake_mp.MemoryProvider = _FakeMemoryProvider  # type: ignore[attr-defined]
_fake_mp.RecallStatus = _FakeRecallStatus  # type: ignore[attr-defined]

sys.modules.setdefault("agent", _fake_agent)
sys.modules.setdefault("agent.memory_provider", _fake_mp)

_PLUGIN_PATH = __import__("pathlib").Path(__file__).resolve().parent.parent / "hermes-plugin" / "__init__.py"

_spec = importlib.util.spec_from_file_location("palimpsest_plugin_adapter", _PLUGIN_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

PalimpsestMemoryProvider = _mod.PalimpsestMemoryProvider
SEARCH_SCHEMA = _mod.SEARCH_SCHEMA
INGEST_SCHEMA = _mod.INGEST_SCHEMA


class _CapturingProvider(PalimpsestMemoryProvider):
    """Provider that records request payloads instead of doing HTTP."""

    def __init__(self, response: dict | None = None, **env: str) -> None:
        self.sent: list[tuple[str, dict]] = []
        self._response = response if response is not None else {}
        with patch.dict("os.environ", env, clear=False):
            super().__init__()

    def _fake_post(self, url: str, payload: dict, timeout: float = 5.0) -> dict:
        self.sent.append((url, payload))
        return self._response

    def _run(self, method: str, *args, **kwargs):
        with patch.object(_mod, "_http_post", side_effect=self._fake_post):
            return getattr(self, method)(*args, **kwargs)


# ---------------------------------------------------------------------------
# Boundary guard: decisions live in the core, not the adapter.
# ---------------------------------------------------------------------------


class TestAdapterHasNoDecisionLogic:
    def test_decision_symbols_are_gone(self):
        for name in (
            "_extract_points",
            "_is_near_duplicate",
            "_IMPORTANT_RE",
            "_EXPLICIT_INSTRUCTION_RE",
            "_NEAR_DUP_THRESHOLD",
            "is_trivial_prompt",
        ):
            assert not hasattr(_mod, name), f"{name} must live in core/strategy.py, not the adapter"

    def test_lifecycle_endpoints_are_used(self):
        src = _PLUGIN_PATH.read_text(encoding="utf-8")
        for endpoint in (
            "/lifecycle/pre-turn",
            "/lifecycle/post-turn",
            "/lifecycle/session-end",
            "/lifecycle/pre-compress",
        ):
            assert endpoint in src, f"adapter must forward to {endpoint}"


# ---------------------------------------------------------------------------
# prefetch -> /lifecycle/pre-turn
# ---------------------------------------------------------------------------


class TestPrefetchForwarding:
    def test_posts_to_pre_turn_and_returns_inject_text(self):
        cap = _CapturingProvider(response={"inject_text": "[Palimpsest 记忆注入]\n- (0.8) x"})
        cap._enabled = True
        out = cap._run("prefetch", "帮我查一下上周的会议记录")
        assert out == "[Palimpsest 记忆注入]\n- (0.8) x"
        url, payload = cap.sent[0]
        assert url.endswith("/lifecycle/pre-turn")
        assert payload["user_message"] == "帮我查一下上周的会议记录"

    def test_forwards_scope_tier_topk_minscore(self):
        cap = _CapturingProvider(
            response={"inject_text": "x"},
            PALIMPSEST_PREFETCH_SCOPE="memory",
            PALIMPSEST_PREFETCH_TIER="facts",
            PALIMPSEST_PREFETCH_TOP_K="3",
            PALIMPSEST_PREFETCH_MIN_SCORE="0.3",
        )
        cap._enabled = True
        cap._run("prefetch", "帮我查一下上周的会议记录")
        _, payload = cap.sent[0]
        assert payload["scope"] == "memory"
        assert payload["tier"] == "facts"
        assert payload["top_k"] == 3
        assert payload["min_score"] == 0.3

    def test_error_response_yields_empty(self):
        cap = _CapturingProvider(response={"error": "boom"})
        cap._enabled = True
        assert cap._run("prefetch", "帮我查一下上周的会议记录") == ""

    def test_disabled_returns_empty_without_request(self):
        cap = _CapturingProvider()
        cap._enabled = False
        assert cap._run("prefetch", "帮我查一下上周的会议记录") == ""
        assert cap.sent == []


# ---------------------------------------------------------------------------
# sync_turn -> /lifecycle/post-turn
# ---------------------------------------------------------------------------


class TestSyncTurnForwarding:
    def test_posts_to_post_turn(self):
        cap = _CapturingProvider()
        cap._enabled = True
        cap._run("sync_turn", "记住这个偏好", "好的")
        url, payload = cap.sent[0]
        assert url.endswith("/lifecycle/post-turn")
        assert payload["user_message"] == "记住这个偏好"
        assert payload["assistant_message"] == "好的"

    def test_auto_ingest_off_skips_request(self):
        cap = _CapturingProvider(PALIMPSEST_AUTO_INGEST="false")
        cap._enabled = True
        cap._run("sync_turn", "记住这个偏好", "好的")
        assert cap.sent == []


# ---------------------------------------------------------------------------
# on_session_end -> /lifecycle/session-end
# ---------------------------------------------------------------------------


class TestSessionEndForwarding:
    def test_posts_to_session_end(self):
        cap = _CapturingProvider()
        cap._enabled = True
        msgs = [{"role": "user", "content": "记住这个偏好：我喜欢深色模式"}]
        cap._run("on_session_end", msgs)
        url, payload = cap.sent[0]
        assert url.endswith("/lifecycle/session-end")
        assert payload["messages"] == msgs


# ---------------------------------------------------------------------------
# on_pre_compress -> /lifecycle/pre-compress
# ---------------------------------------------------------------------------


class TestPreCompressForwarding:
    def test_returns_points_text(self):
        cap = _CapturingProvider(response={"points_text": "[user] 记住这个偏好"})
        cap._enabled = True
        out = cap._run("on_pre_compress", [{"role": "user", "content": "记住这个偏好"}])
        assert out == "[user] 记住这个偏好"
        assert cap.sent[0][0].endswith("/lifecycle/pre-compress")


# ---------------------------------------------------------------------------
# initialize: cron/flush sessions are skipped
# ---------------------------------------------------------------------------


class TestInitializeContexts:
    def test_cron_context_disables_provider(self):
        cap = _CapturingProvider()
        cap.initialize("s1", agent_context="cron")
        assert cap._enabled is False

    def test_normal_context_enables_provider(self):
        cap = _CapturingProvider()
        cap.initialize("s1", agent_context="cli", platform="desktop")
        assert cap._enabled is True


# ---------------------------------------------------------------------------
# config_schema.py still exposes prefetch_scope
# ---------------------------------------------------------------------------


class TestConfigSchemaSurface:
    def test_prefetch_scope_field_declared(self):
        schema_path = _PLUGIN_PATH.parent / "config_schema.py"
        fake = _fake_config_schema_module()
        # config_schema.py imports `plugins.memory.config_schema` at module load;
        # register the pure-data stand-in under that exact name first.
        with patch.dict(sys.modules, {"plugins.memory.config_schema": fake}):
            spec = importlib.util.spec_from_file_location("_pscope_schema", schema_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        keys = [f.key for f in module.CONFIG_SCHEMA.fields]
        assert "prefetch_scope" in keys


def _fake_config_schema_module() -> types.ModuleType:
    """Minimal stand-in for plugins.memory.config_schema (pure data classes)."""
    mod = types.ModuleType("plugins.memory.config_schema")

    class ProviderField:
        def __init__(self, *, key, label, default, description, kind="text", inline=False):
            self.key = key
            self.label = label
            self.default = default
            self.description = description
            self.kind = kind
            self.inline = inline

    class ProviderConfigSchema:
        def __init__(self, *, name, label, storage, docs_url, fields):
            self.name = name
            self.label = label
            self.storage = storage
            self.docs_url = docs_url
            self.fields = fields

    mod.ProviderField = ProviderField  # type: ignore[attr-defined]
    mod.ProviderConfigSchema = ProviderConfigSchema  # type: ignore[attr-defined]
    mod.STORAGE_FLAT_JSON = "flat_json"  # type: ignore[attr-defined]
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    mem = types.ModuleType("plugins.memory")
    mem.__path__ = []
    sys.modules.setdefault("plugins", pkg)
    sys.modules.setdefault("plugins.memory", mem)
    return mod
