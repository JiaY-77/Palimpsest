"""Regression tests for the hermes-plugin tier/scope write-path & read-path fixes.

Covers three defects reported against plugin v1.0.0 (Palimpsest main @ 748606a):

  * ``sync_turn`` wrote raw user fragments into the facts tier as ``type=memory``
    and matched on a loose keyword set that fired on tool output ("启动" in a
    winget log). It also stored ``user_content[:300]`` while matching the whole
    text, so a match past char 300 left no evidence in the stored node.
  * ``on_session_end`` wrote distilled points as ``type=record`` (logs tier)
    while ``prefetch`` defaulted to ``tier="facts"`` — write-only, silently.
  * ``prefetch`` used ``scope="all"``, so ``kb_chunk`` (whose domain is always
    "kb", immune to the domain filter) crowded out the top_k injection slots.

The fixes: explicit-instruction regex for sync_turn, layered attribution
(auto-captured fragments -> logs, distilled summaries -> facts), configurable
prefetch scope defaulting to ``memory``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Fake `agent.memory_provider` module so the plugin can be imported standalone.
# ---------------------------------------------------------------------------

_fake_agent = types.ModuleType("agent")
_fake_agent.__path__ = []  # make it a package

_fake_mp = types.ModuleType("agent.memory_provider")


class _FakeMemoryProvider:
    pass


class _FakeRecallStatus:
    pass


def _fake_is_trivial_prompt(text: str) -> bool:
    return False


_fake_mp.MemoryProvider = _FakeMemoryProvider  # type: ignore[attr-defined]
_fake_mp.RecallStatus = _FakeRecallStatus  # type: ignore[attr-defined]
_fake_mp.is_trivial_prompt = _fake_is_trivial_prompt  # type: ignore[attr-defined]

sys.modules.setdefault("agent", _fake_agent)
sys.modules.setdefault("agent.memory_provider", _fake_mp)

_PLUGIN_PATH = (
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "hermes-plugin"
    / "__init__.py"
)

_spec = importlib.util.spec_from_file_location("palimpsest_plugin_tier", _PLUGIN_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

PalimpsestMemoryProvider = _mod.PalimpsestMemoryProvider
_EXPLICIT_INSTRUCTION_RE = _mod._EXPLICIT_INSTRUCTION_RE


def _make_provider(**env: str) -> PalimpsestMemoryProvider:
    """构造 provider，允许按测试注入环境变量（构造后清掉 env 影响）。"""
    with patch.dict("os.environ", env, clear=False):
        return PalimpsestMemoryProvider()


class _CapturingProvider(PalimpsestMemoryProvider):
    """Provider that records ingest/search payloads instead of doing HTTP."""

    def __init__(self, **env: str) -> None:
        self.sent: list[tuple[str, dict]] = []
        with patch.dict("os.environ", env, clear=False):
            super().__init__()

    def _fake_post(self, url: str, payload: dict, timeout: float = 5.0) -> dict:
        self.sent.append((url, payload))
        if url.endswith("/mem/search") and payload.get("query") == "__DUP__":  # pragma: no cover
            return {"results": []}
        return {"results": []}


# ---------------------------------------------------------------------------
# #48 — sync_turn: explicit-instruction regex, logs tier, auditable payload
# ---------------------------------------------------------------------------


class TestSyncTurnExplicitInstruction:
    def _run(self, user_content: str) -> list[tuple[str, dict]]:
        cap = _CapturingProvider()
        cap._enabled = True
        with patch.object(_mod, "_http_post", side_effect=cap._fake_post):
            cap.sync_turn(user_content, "ok")
        return cap.sent

    def test_operational_verbs_no_longer_trigger(self):
        # 从前这些会被宽正则命中并落库 —— 现在不应写入
        for text in (
            "已成功安装\n正在启动程序包安装...\n已成功完成",
            "接下来安排一下安装顺序",
            "这个计划怎么推进",
            "优先考虑开源方案",
        ):
            assert self._run(text) == [], f"should not ingest: {text!r}"

    def test_explicit_instruction_triggers(self):
        for text in (
            "以后定稿就是留下对应定稿的 md 文件",
            "记住我不喜欢这种术语",
            "不对，这个应该改成小写",
            "我的偏好是简洁一点",
        ):
            sent = self._run(text)
            assert len(sent) == 1, f"should ingest: {text!r}"

    def test_ingests_into_logs_tier_not_facts(self):
        sent = self._run("记住这个偏好：我喜欢深色模式")
        assert len(sent) == 1
        _, payload = sent[0]
        assert payload["type"] == "record"  # logs 层，不是 facts 层

    def test_payload_records_matched_keyword(self):
        sent = self._run("记住这个偏好：我喜欢深色模式")
        _, payload = sent[0]
        assert payload.get("matched_keyword") == "记住"
        assert payload.get("match_pos") == 0

    def test_truncation_marker_present_when_long(self):
        long_text = "x" * 400 + "记住这个规则"
        sent = self._run(long_text)
        assert len(sent) == 1
        content = sent[0][1]["content"]
        assert "…[截断]" in content

    def test_no_truncation_marker_when_short(self):
        sent = self._run("记住这个偏好")
        content = sent[0][1]["content"]
        assert "…[截断]" not in content

    def test_regex_does_not_match_pure_operational_sentence(self):
        assert _EXPLICIT_INSTRUCTION_RE.search("启动服务并安排计划") is None
        assert _EXPLICIT_INSTRUCTION_RE.search("记住这件事") is not None


# ---------------------------------------------------------------------------
# #47 — on_session_end writes distilled points to the facts tier
# ---------------------------------------------------------------------------


class TestSessionEndFactsTier:
    def test_session_end_writes_memory_type(self):
        cap = _CapturingProvider()
        cap._enabled = True
        msgs = [
            {"role": "user", "content": "记住这个偏好：我喜欢深色模式"},
            {"role": "user", "content": "不对，改成浅色"},
        ]
        with patch.object(_mod, "_http_post", side_effect=cap._fake_post):
            cap.on_session_end(msgs)
        ingest = [p for u, p in cap.sent if u.endswith("/mem/ingest")]
        assert len(ingest) == 1
        assert ingest[0]["type"] == "memory"  # facts 层 —— 与 prefetch 默认 tier 一致
        assert ingest[0]["source"] == "hermes-session_end"


# ---------------------------------------------------------------------------
# #46 — prefetch scope defaults to "memory" and is configurable
# ---------------------------------------------------------------------------


class TestPrefetchScope:
    def test_default_scope_is_memory(self):
        cap = _CapturingProvider()
        cap._enabled = True
        with patch.object(_mod, "_http_post", side_effect=cap._fake_post):
            cap.prefetch("记忆注入的是哪些节点")
        search_calls = [p for u, p in cap.sent if u.endswith("/mem/search")]
        assert len(search_calls) == 1
        assert search_calls[0]["scope"] == "memory"

    def test_scope_configurable_via_env(self):
        cap = _CapturingProvider(PALIMPSEST_PREFETCH_SCOPE="all")
        cap._enabled = True
        with patch.object(_mod, "_http_post", side_effect=cap._fake_post):
            cap.prefetch("记忆注入的是哪些节点")
        search_calls = [p for u, p in cap.sent if u.endswith("/mem/search")]
        assert search_calls[0]["scope"] == "all"

    def test_prefetch_still_sends_tier(self):
        cap = _CapturingProvider()
        cap._enabled = True
        with patch.object(_mod, "_http_post", side_effect=cap._fake_post):
            cap.prefetch("记忆注入的是哪些节点")
        search_calls = [p for u, p in cap.sent if u.endswith("/mem/search")]
        assert search_calls[0]["tier"] == "facts"


# ---------------------------------------------------------------------------
# config_schema.py exposes the new key
# ---------------------------------------------------------------------------


class TestConfigSchemaSurface:
    def test_prefetch_scope_field_declared(self):
        schema_path = _PLUGIN_PATH.parent / "config_schema.py"
        with patch.dict(
            sys.modules,
            {"plugins.memory.config_schema": _fake_config_schema_module()},
        ):
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
