"""Regression tests for issue #54 — ``palimpsest_search`` schema exposed no
``domain`` parameter, so the model could never pass one and searches silently
fell back to the plugin's default domain (``hermes``). Retrieving ``domain=task``
nodes (the built-in task workflow) was impossible through the tool even though
the REST endpoint supports it.

The fix is a one-line schema addition: ``SEARCH_SCHEMA`` now declares ``domain``
exactly like ``INGEST_SCHEMA`` already did — the handler in ``_tool_search``
already read ``args.get("domain", self._domain)``, so no logic changed.

These tests pin both halves:
  * the schema *declares* ``domain`` (so a model can send it);
  * a call *carrying* ``domain`` reaches the request payload (no silent drop).
"""

from __future__ import annotations

import importlib.util
import json
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

_spec = importlib.util.spec_from_file_location("palimpsest_plugin_search_schema", _PLUGIN_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

PalimpsestMemoryProvider = _mod.PalimpsestMemoryProvider
SEARCH_SCHEMA = _mod.SEARCH_SCHEMA
INGEST_SCHEMA = _mod.INGEST_SCHEMA


class _CapturingProvider(PalimpsestMemoryProvider):
    """Provider that records the request payload instead of doing HTTP."""

    def __init__(self, **env: str) -> None:
        self.sent: list[tuple[str, dict]] = []
        with patch.dict("os.environ", env, clear=False):
            super().__init__()

    def _fake_post(self, url: str, payload: dict, timeout: float = 5.0) -> dict:
        self.sent.append((url, payload))
        return {"results": []}


# ---------------------------------------------------------------------------
# Schema declaration: a model must be *able* to pass `domain`.
# ---------------------------------------------------------------------------


class TestSearchSchemaDeclaresDomain:
    def test_search_schema_has_domain_property(self):
        props = SEARCH_SCHEMA["parameters"]["properties"]
        assert "domain" in props, "palimpsest_search schema must expose `domain`"
        assert props["domain"]["type"] == "string"

    def test_search_domain_matches_ingest_domain_shape(self):
        # The two schemas describe the same concept; keep them consistent so
        # they can't drift apart again (that drift is exactly issue #54).
        search_domain = SEARCH_SCHEMA["parameters"]["properties"]["domain"]
        ingest_domain = INGEST_SCHEMA["parameters"]["properties"]["domain"]
        assert search_domain == ingest_domain

    def test_domain_not_required(self):
        # Absent domain must stay valid — the handler falls back to its own.
        assert "domain" not in SEARCH_SCHEMA["parameters"].get("required", ["query"])


# ---------------------------------------------------------------------------
# Handler behaviour: a passed `domain` reaches the request (no silent drop).
# ---------------------------------------------------------------------------


class TestSearchDomainReachesRequest:
    def _run(self, args: dict) -> dict:
        cap = _CapturingProvider()
        cap._enabled = True
        with patch.object(_mod, "_http_post", side_effect=cap._fake_post):
            cap._tool_search(args)
        assert cap.sent, "expected one request"
        url, payload = cap.sent[0]
        assert url.endswith("/mem/search")
        return payload

    def test_explicit_domain_is_forwarded(self):
        payload = self._run({"query": "T001", "domain": "task"})
        assert payload["domain"] == "task"

    def test_missing_domain_falls_back_to_provider_default(self):
        cap = _CapturingProvider(PALIMPSEST_DOMAIN="work")
        cap._enabled = True
        with patch.object(_mod, "_http_post", side_effect=cap._fake_post):
            cap._tool_search({"query": "x"})
        url, payload = cap.sent[0]
        assert url.endswith("/mem/search")
        assert payload["domain"] == "work"

    def test_empty_domain_string_is_forwarded_as_is(self):
        # REST treats "" as "no domain filter"; the tool must not rewrite it
        # to the default, or the documented empty-string behaviour is lost.
        payload = self._run({"query": "x", "domain": ""})
        assert payload["domain"] == ""


# ---------------------------------------------------------------------------
# get_tool_schemas wiring: the advertised schema is the fixed one.
# ---------------------------------------------------------------------------


class TestExposedSchemaIsFixed:
    def test_exposed_search_tool_carries_domain(self):
        cap = _CapturingProvider()
        tools = {t["name"]: t for t in cap.get_tool_schemas()}
        assert "palimpsest_search" in tools
        assert "domain" in tools["palimpsest_search"]["parameters"]["properties"]
