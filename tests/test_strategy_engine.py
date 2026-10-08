"""Tests for the memory strategy engine (``core/strategy.py``).

These tests were migrated from ``test_hermes_plugin_extraction.py`` after the
decision logic moved from the Hermes adapter (``hermes-plugin/__init__.py``)
into the Palimpsest core. The behaviours pinned here (issue #46/#47/#48) are
unchanged — only their *location* moved: the strategy engine now owns
"what to remember / which tier / when to recall", the adapter only forwards.

No HTTP and no database are involved: the engine is pure decision logic, so it
is tested directly.
"""

from __future__ import annotations

from core import strategy
from core.strategy import (
    decide_post_turn,
    decide_pre_compress,
    decide_pre_turn,
    decide_session_end,
    extract_points,
    is_near_duplicate,
    is_trivial_prompt,
)


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


IMPORTANT_TEXT = "记住这个偏好：我喜欢深色模式"  # hits _IMPORTANT_RE
BORING_TEXT = "今天天气不错"  # does NOT hit _IMPORTANT_RE


# ---------------------------------------------------------------------------
# extract_points (was test_hermes_plugin_extraction.py::TestExtractPoints)
# ---------------------------------------------------------------------------


class TestExtractPoints:
    def test_tool_and_system_roles_ignored(self):
        msgs = [
            _msg("tool", '{"success": true, "name": "model-fleet-command"}'),
            _msg("system", "You are a helpful assistant."),
            _msg("tool", IMPORTANT_TEXT),
        ]
        assert extract_points(msgs, limit=10, per_message_chars=150) == []

    def test_user_and_assistant_kept(self):
        msgs = [_msg("user", IMPORTANT_TEXT), _msg("assistant", "好的，没问题。")]
        pts = extract_points(msgs, limit=10, per_message_chars=150)
        assert len(pts) == 1
        assert "[user]" in pts[0]
        assert IMPORTANT_TEXT[:150] in pts[0]

    def test_duplicate_text_only_once(self):
        msgs = [
            _msg("user", IMPORTANT_TEXT),
            _msg("assistant", IMPORTANT_TEXT),
            _msg("user", IMPORTANT_TEXT),
        ]
        assert len(extract_points(msgs, limit=10, per_message_chars=150)) == 1

    def test_boring_text_no_points(self):
        msgs = [_msg("user", BORING_TEXT), _msg("assistant", "是的。")]
        assert extract_points(msgs, limit=10, per_message_chars=150) == []

    def test_per_message_chars_truncation(self):
        long_text = "记住这个：" + "x" * 500
        pts = extract_points([_msg("user", long_text)], limit=10, per_message_chars=80)
        assert len(pts) == 1
        body = pts[0].split("] ", 1)[1]
        assert len(body) <= 80

    def test_limit_enforced(self):
        msgs = [_msg("user", f"记住第{i}条规则：编号{i}") for i in range(11)]
        assert len(extract_points(msgs, limit=5, per_message_chars=150)) == 5

    def test_empty_and_whitespace_text_skipped(self):
        msgs = [_msg("user", ""), _msg("user", "   "), _msg("user", IMPORTANT_TEXT)]
        assert len(extract_points(msgs, limit=10, per_message_chars=150)) == 1

    def test_missing_role_ignored(self):
        assert extract_points([{"content": IMPORTANT_TEXT}], limit=10, per_message_chars=150) == []

    def test_mixed_roles_preserves_order(self):
        msgs = [
            _msg("assistant", "记住：方案A优先"),
            _msg("user", "不对，改成方案B"),
        ]
        pts = extract_points(msgs, limit=10, per_message_chars=150)
        assert len(pts) == 2
        assert pts[0].startswith("[assistant]")
        assert pts[1].startswith("[user]")


# ---------------------------------------------------------------------------
# is_near_duplicate — now takes the /mem/search hits (query done by caller)
# ---------------------------------------------------------------------------


class TestIsNearDuplicate:
    def test_high_score_returns_true(self):
        assert is_near_duplicate([{"score": 0.99}]) is True

    def test_low_score_returns_false(self):
        assert is_near_duplicate([{"score": 0.5}]) is False

    def test_empty_results_returns_false(self):
        assert is_near_duplicate([]) is False
        assert is_near_duplicate(None) is False

    def test_missing_score_returns_false(self):
        assert is_near_duplicate([{}]) is False

    def test_score_exactly_at_threshold_returns_true(self):
        assert is_near_duplicate([{"score": strategy.NEAR_DUP_THRESHOLD}]) is True


# ---------------------------------------------------------------------------
# is_trivial_prompt — self-contained copy (was borrowed from the host)
# ---------------------------------------------------------------------------


class TestIsTrivialPrompt:
    def test_empty_and_slash_are_trivial(self):
        assert is_trivial_prompt("") is True
        assert is_trivial_prompt("   ") is True
        assert is_trivial_prompt("/help") is True

    def test_greetings_are_trivial(self):
        for t in ("hi", "ok", "thanks", "got it", "continue", "yes"):
            assert is_trivial_prompt(t) is True

    def test_real_question_is_not_trivial(self):
        assert is_trivial_prompt("帮我查一下上周的会议记录") is False


# ---------------------------------------------------------------------------
# decide_pre_turn (issue #46: scope/tier are honoured; skip logic in core)
# ---------------------------------------------------------------------------


_PRE_TURN_HITS = [{"id": 1, "score": 0.8, "summary": "一条很有用的记忆"}]


class TestDecidePreTurn:
    def test_trivial_skipped(self):
        r = decide_pre_turn("hi", hits=_PRE_TURN_HITS)
        assert r["skip"] is True and r["skip_reason"] == "trivial"

    def test_too_short_skipped(self):
        r = decide_pre_turn("a", hits=_PRE_TURN_HITS)
        assert r["skip"] is True and r["skip_reason"] == "too_short"

    def test_no_hits_skipped(self):
        r = decide_pre_turn("帮我查一下上周的会议记录", hits=[])
        assert r["skip"] is True and r["skip_reason"] == "no_hit"

    def test_hits_produce_inject_text_and_blocks(self):
        r = decide_pre_turn("帮我查一下上周的会议记录", hits=_PRE_TURN_HITS)
        assert r["skip"] is False
        assert "Palimpsest 记忆注入" in r["inject_text"]
        assert r["inject_blocks"][0]["content"] == "一条很有用的记忆"
        assert r["inject_blocks"][0]["source"] == "mem#1"

    def test_min_score_filters(self):
        hits = [{"id": 1, "score": 0.2, "summary": "低分"}, {"id": 2, "score": 0.9, "summary": "高分"}]
        r = decide_pre_turn("帮我查一下上周的会议记录", hits=hits, min_score=0.5)
        assert [b["content"] for b in r["inject_blocks"]] == ["高分"]

    def test_top_k_truncates(self):
        hits = [{"id": i, "score": 0.9, "summary": f"m{i}"} for i in range(10)]
        r = decide_pre_turn("帮我查一下上周的会议记录", hits=hits, top_k=3)
        assert len(r["inject_blocks"]) == 3


# ---------------------------------------------------------------------------
# decide_post_turn (issue #48: explicit-instruction, logs tier, auditable)
# ---------------------------------------------------------------------------


class TestDecidePostTurn:
    def test_operational_verbs_do_not_trigger(self):
        for text in (
            "已成功安装\n正在启动程序包安装...\n已成功完成",
            "接下来安排一下安装顺序",
            "这个计划怎么推进",
            "优先考虑开源方案",
        ):
            assert decide_post_turn(text)["store"] is False, f"should not ingest: {text!r}"

    def test_explicit_instruction_triggers(self):
        for text in (
            "以后定稿就是留下对应定稿的 md 文件",
            "记住我不喜欢这种术语",
            "不对，这个应该改成小写",
            "我的偏好是简洁一点",
        ):
            assert decide_post_turn(text)["store"] is True, f"should ingest: {text!r}"

    def test_stores_into_logs_tier_not_facts(self):
        d = decide_post_turn("记住这个偏好：我喜欢深色模式")
        assert d["store"] is True
        assert d["type"] == "record"  # logs 层
        assert d["tier"] == "logs"

    def test_records_matched_keyword_for_audit(self):
        d = decide_post_turn("记住这个偏好：我喜欢深色模式")
        assert d["decision_log"]["matched_keyword"] == "记住"
        assert d["decision_log"]["match_pos"] == 0

    def test_truncation_marker_present_when_long(self):
        d = decide_post_turn("x" * 400 + "记住这个规则")
        assert d["store"] is True
        assert "…[截断]" in d["content"]

    def test_no_truncation_marker_when_short(self):
        assert "…[截断]" not in decide_post_turn("记住这个偏好")["content"]

    def test_correction_raises_importance(self):
        assert decide_post_turn("不对，改成这样")["importance"] == 0.7
        assert decide_post_turn("记住这个偏好")["importance"] == 0.6

    def test_auto_ingest_off_stores_nothing(self):
        assert decide_post_turn("记住这个偏好", auto_ingest=False)["store"] is False


# ---------------------------------------------------------------------------
# decide_session_end (issue #47: distilled points -> facts tier)
# ---------------------------------------------------------------------------


class TestDecideSessionEnd:
    def test_writes_memory_type_facts_tier(self):
        msgs = [
            {"role": "user", "content": "记住这个偏好：我喜欢深色模式"},
            {"role": "user", "content": "不对，改成浅色"},
        ]
        d = decide_session_end(msgs)
        assert d["store"] is True
        assert d["type"] == "memory"  # facts 层 —— 与 prefetch 默认 tier 一致
        assert d["tier"] == "facts"
        assert d["importance"] == 0.55

    def test_no_points_stores_nothing(self):
        d = decide_session_end([{"role": "user", "content": BORING_TEXT}])
        assert d["store"] is False

    def test_auto_ingest_off_stores_nothing(self):
        msgs = [{"role": "user", "content": "记住这个偏好"}]
        assert decide_session_end(msgs, auto_ingest=False)["store"] is False


# ---------------------------------------------------------------------------
# decide_pre_compress — extract only, never writes
# ---------------------------------------------------------------------------


class TestDecidePreCompress:
    def test_returns_points_text_and_no_store_key(self):
        msgs = [{"role": "user", "content": "记住这个偏好：深色模式"}]
        r = decide_pre_compress(msgs)
        assert "记住" in r["points_text"]
        assert "store" not in r  # 明确不写库
