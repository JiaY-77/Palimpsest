"""Tests for hermes-plugin _extract_points and _is_near_duplicate."""

from __future__ import annotations

import importlib.util
import sys
import types
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Fake `agent.memory_provider` module so the plugin can be imported without
# the real Hermes package.
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

# ---------------------------------------------------------------------------
# Load the plugin module by file path (avoids package-name conflicts).
# ---------------------------------------------------------------------------

_PLUGIN_PATH = (
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "hermes-plugin"
    / "__init__.py"
)

_spec = importlib.util.spec_from_file_location("palimpsest_plugin", _PLUGIN_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_extract_points = _mod._extract_points
_is_near_duplicate = _mod._is_near_duplicate
_http_post = _mod._http_post

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


IMPORTANT_TEXT = "记住这个偏好：我喜欢深色模式"  # hits _IMPORTANT_RE
BORING_TEXT = "今天天气不错"  # does NOT hit _IMPORTANT_RE


# ---------------------------------------------------------------------------
# _extract_points tests
# ---------------------------------------------------------------------------


class TestExtractPoints:
    def test_tool_and_system_roles_ignored(self):
        msgs = [
            _msg("tool", '{"success": true, "name": "model-fleet-command"}'),
            _msg("system", "You are a helpful assistant."),
            _msg("tool", IMPORTANT_TEXT),
        ]
        assert _extract_points(msgs, limit=10, per_message_chars=150) == []

    def test_user_and_assistant_kept(self):
        msgs = [_msg("user", IMPORTANT_TEXT), _msg("assistant", "好的，没问题。")]
        pts = _extract_points(msgs, limit=10, per_message_chars=150)
        assert len(pts) == 1
        assert "[user]" in pts[0]
        assert IMPORTANT_TEXT[:150] in pts[0]

    def test_duplicate_text_only_once(self):
        msgs = [
            _msg("user", IMPORTANT_TEXT),
            _msg("assistant", IMPORTANT_TEXT),
            _msg("user", IMPORTANT_TEXT),
        ]
        pts = _extract_points(msgs, limit=10, per_message_chars=150)
        assert len(pts) == 1

    def test_boring_text_no_points(self):
        msgs = [_msg("user", BORING_TEXT), _msg("assistant", "是的。")]
        assert _extract_points(msgs, limit=10, per_message_chars=150) == []

    def test_per_message_chars_truncation(self):
        long_text = "记住这个：" + "x" * 500
        msgs = [_msg("user", long_text)]
        pts = _extract_points(msgs, limit=10, per_message_chars=80)
        assert len(pts) == 1
        # "[user] " is 7 chars, then up to 80 chars of text
        body = pts[0].split("] ", 1)[1]
        assert len(body) <= 80

    def test_limit_enforced(self):
        msgs = [_msg("user", f"记住第{i}条规则：编号{i}") for i in range(11)]
        pts = _extract_points(msgs, limit=5, per_message_chars=150)
        assert len(pts) == 5

    def test_empty_and_whitespace_text_skipped(self):
        msgs = [_msg("user", ""), _msg("user", "   "), _msg("user", IMPORTANT_TEXT)]
        pts = _extract_points(msgs, limit=10, per_message_chars=150)
        assert len(pts) == 1

    def test_missing_role_ignored(self):
        msgs = [{"content": IMPORTANT_TEXT}]  # no "role" key
        pts = _extract_points(msgs, limit=10, per_message_chars=150)
        assert pts == []

    def test_mixed_roles_preserves_order(self):
        msgs = [
            _msg("assistant", "记住：方案A优先"),
            _msg("user", "不对，改成方案B"),
        ]
        pts = _extract_points(msgs, limit=10, per_message_chars=150)
        assert len(pts) == 2
        assert pts[0].startswith("[assistant]")
        assert pts[1].startswith("[user]")


# ---------------------------------------------------------------------------
# _is_near_duplicate tests
# ---------------------------------------------------------------------------


class TestIsNearDuplicate:
    def _call(self, resp_payload):
        with patch.object(_mod, "_http_post", return_value=resp_payload):
            return _is_near_duplicate("test content", "http://x:8090", "hermes")

    def test_high_score_returns_true(self):
        assert self._call({"results": [{"score": 0.99}]}) is True

    def test_low_score_returns_false(self):
        assert self._call({"results": [{"score": 0.5}]}) is False

    def test_empty_results_returns_false(self):
        assert self._call({"results": []}) is False

    def test_error_response_returns_false(self):
        assert self._call({"error": "boom"}) is False

    def test_http_post_raises_returns_false(self):
        with patch.object(_mod, "_http_post", side_effect=RuntimeError("net")):
            assert _is_near_duplicate("x", "http://x:8090", "hermes") is False

    def test_missing_score_returns_false(self):
        assert self._call({"results": [{}]}) is False
