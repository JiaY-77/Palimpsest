"""契约测试：实质轮筛选（「骨架即胶囊」第二刀，issue #170）。

会话缓冲只收「实质轮」——三条判据任一满足（判定在 core/strategy.py 纯函数
is_substantive_turn，端点只取值与执行，本文件钉行为）：

  1. 判据一（工具调用）  tool_calls_count > 0 → 实质（正/反例 + 负数不命中）
  2. 判据二（长思考+结论） 正文 ≥ 门槛（strip 后）且结论类字段任一非空 → 实质
     （含恰好等于门槛的边界；只有长复述没有结论字段 → 不实质）
  3. 判据三（指令动词）  用户消息含指令动词 → 实质；**问句但含指令动词也算**
     （指令 ≠ 问句）；纯疑问 / 寒暄 → 不实质
  4. 三判据全不满足 → 不进缓冲且 skip_reason=non_substantive（可观测，不静默
     丢弃）；no_session_id / no_sections 既有语义优先级不变
  5. 老宿主兼容：不传新字段（tool_calls_count 缺省 0 / 纯函数不传 user_message）
     → 只有判据二生效，缺字段绝不被判成实质轮
  6. 端到端（临时库，不碰生产）：全短轮 session → session-end 不产胶囊
     （capsule 字段缺席）；混合 session（2 短 + 2 实质）→ 胶囊只含那 2 条
     实质轮

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib

import pytest

from core.strategy import (
    SESSION_BUFFER_MIN_THOUGHT_CHARS,
    decide_session_buffer,
    is_substantive_turn,
    parse_thought_sections,
)


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本测试新建的节点。

    与 test_session_capsule 同款隔离模式：conftest 用 session 级临时库，
    所有测试文件共享；删除节点会级联删边 + 清 FTS，只需追踪 node_id 差集。
    """
    from mcp_tools import store

    def _ids() -> set:
        try:
            return {nid for nid, _ in store.iter_payloads()}
        except Exception:  # noqa: BLE001 —— 桩失败则不清理（不掩盖真实错误）
            return set()

    before = _ids()
    yield
    for nid in _ids() - before:
        with contextlib.suppress(Exception):
            store.delete_node(nid)


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------

# 短轮 <thought>：default 骨架四问都有编号答案（judgement 非空），但正文
# 远低于 400 字——三条判据全不中（配合无工具 + 无指令动词的 user_message）。
_SHORT_BLOCK = (
    "<thought>\n"
    "1. 要的是什么（一句话）：确认一下\n"
    "2. 已知 / 未知：无\n"
    "3. 值不值得记 / 该不该动手：不值得\n"
    "4. 这一轮的产出或下一步：无\n"
    "</thought>"
)


def _long_block_with_conclusion(body_len: int) -> str:
    """判据二正样本：thought 正文（strip 后）恰好 body_len 字，judgement 非空。

    前两行用填充字凑长度（构造失败立刻 raise，不静默出偏短样本）。
    """
    l1_head = "1. 要的是什么（一句话）："
    l2_head = "2. 已知 / 未知（还缺什么信息）："
    l3 = "3. 值不值得记 / 该不该动手：值得，拍板就按方案 A 执行"
    l4 = "4. 这一轮的产出或下一步：下一步跑回归"
    pad = body_len - len(l1_head) - len(l2_head) - len("补充材料") - len(l3) - len(l4) - 3  # 3 个换行
    assert pad > 0, (
        f"body_len={body_len} 装不下固定标签行（至少需 {len(l1_head) + len(l2_head) + len(l3) + len(l4) + 8}）"
    )
    block = f"<thought>\n{l1_head}{'长' * pad}\n{l2_head}补充材料\n{l3}\n{l4}\n</thought>"
    assert len(_thought_body(block).strip()) == body_len, "样本正文长度漂移（测试构造 bug）"
    return block


def _long_block_without_conclusion(body_len: int) -> str:
    """判据二反样本：正文（strip 后）恰好 body_len 字，但结论类字段全缺——
    只写 goal / known_unknown 两个编号行（不写 3/4 问，模型跳问的真实形态），
    长复述不等于想清楚了。"""
    l1_head = "1. 要的是什么（一句话）："
    l2_head = "2. 已知 / 未知（还缺什么信息）："
    pad = body_len - len(l1_head) - len(l2_head) - len("补充材料") - 1  # 1 个换行
    assert pad > 0, f"body_len={body_len} 装不下固定标签行"
    block = f"<thought>\n{l1_head}{'长' * pad}\n{l2_head}补充材料\n</thought>"
    assert len(_thought_body(block).strip()) == body_len, "样本正文长度漂移（测试构造 bug）"
    return block


def _sections_of(block: str, kind: str = "default") -> dict[str, str]:
    return parse_thought_sections(block, kind)


# ---------------------------------------------------------------------------
# 1-3. is_substantive_turn：三条判据各一组正反例
# ---------------------------------------------------------------------------


class TestCriterionToolCalls:
    def test_tool_call_turn_is_substantive_even_with_short_thought(self):
        assert is_substantive_turn("短", _sections_of(_SHORT_BLOCK), tool_calls_count=1) is True

    def test_multiple_tool_calls(self):
        assert is_substantive_turn("短", _sections_of(_SHORT_BLOCK), tool_calls_count=3) is True

    def test_zero_tool_calls_does_not_hit_criterion_1(self):
        assert is_substantive_turn("短", _sections_of(_SHORT_BLOCK), tool_calls_count=0) is False

    def test_negative_count_treated_as_no_calls(self):
        """异常输入（负数）按「无工具调用」处理，不当成实质轮。"""
        assert is_substantive_turn("短", _sections_of(_SHORT_BLOCK), tool_calls_count=-1) is False


class TestCriterionLongThoughtWithConclusion:
    def test_long_thought_with_conclusion_field(self):
        block = _long_block_with_conclusion(450)
        assert is_substantive_turn(_thought_body(block), _sections_of(block)) is True

    def test_exactly_at_threshold_counts(self):
        """边界：正文恰好等于门槛（400）→ 算实质（≥ 语义）。"""
        block = _long_block_with_conclusion(SESSION_BUFFER_MIN_THOUGHT_CHARS)
        assert is_substantive_turn(_thought_body(block), _sections_of(block)) is True

    def test_below_threshold_not_substantive(self):
        block = _long_block_with_conclusion(SESSION_BUFFER_MIN_THOUGHT_CHARS - 1)
        assert is_substantive_turn(_thought_body(block), _sections_of(block)) is False

    def test_long_but_no_conclusion_field_not_substantive(self):
        """长复述：正文超门槛但结论类字段全缺 → 不实质。"""
        block = _long_block_without_conclusion(450)
        sections = _sections_of(block)
        assert "judgement" not in sections
        assert "conclusion" not in sections and "deliverable" not in sections and "outcome" not in sections
        assert is_substantive_turn(_thought_body(block), sections) is False

    def test_short_thought_with_conclusion_not_substantive(self):
        """短轮每问必答（judgement 非空）但正文不足 → 不实质。"""
        assert is_substantive_turn(_thought_body(_SHORT_BLOCK), _sections_of(_SHORT_BLOCK)) is False

    def test_threshold_injectable(self):
        """门槛可调（Config 注入）：调低后同一正文命中判据二，调高后不命中。"""
        block = _long_block_with_conclusion(100)  # 正文恰 100 字（标签行已自证长度）
        assert is_substantive_turn(_thought_body(block), _sections_of(block), min_thought_chars=10) is True
        assert is_substantive_turn(_thought_body(block), _sections_of(block), min_thought_chars=101) is False

    def test_non_string_section_values_ignored(self):
        """sections 值非字符串（异常输入）不算结论字段，绝不抛异常。"""
        bad = {"judgement": 123, "conclusion": None}  # type: ignore[dict-item]
        assert is_substantive_turn("短", bad) is False  # type: ignore[arg-type]


class TestCriterionImperativeVerb:
    def test_chinese_imperative(self):
        assert is_substantive_turn("短", {}, user_message="把缓冲逻辑改一下") is True

    def test_english_imperative(self):
        assert is_substantive_turn("短", {}, user_message="please fix the bug") is True

    def test_question_form_with_verb_is_still_substantive(self):
        """任务书钉的边界：问句但含指令动词 → 实质轮（指令 ≠ 问句）。"""
        assert is_substantive_turn("短", {}, user_message="要怎么改这个配置？") is True
        assert is_substantive_turn("短", {}, user_message="can you check the logs?") is True

    def test_pure_question_without_verb_not_substantive(self):
        assert is_substantive_turn("短", {}, user_message="这是什么意思？") is False

    def test_smalltalk_not_substantive(self):
        assert is_substantive_turn("短", {}, user_message="你好呀，辛苦啦") is False

    def test_english_without_verb_not_substantive(self):
        assert is_substantive_turn("短", {}, user_message="thank you so much") is False

    def test_word_boundary_no_substring_false_positive(self):
        """英文词边界：docker 不命中 do、doing 不命中 do。"""
        assert is_substantive_turn("短", {}, user_message="open the docker dashboard") is False
        assert is_substantive_turn("短", {}, user_message="keep doing that") is False

    def test_empty_message_not_substantive(self):
        assert is_substantive_turn("短", {}, user_message="") is False
        assert is_substantive_turn("短", {}, user_message="   ") is False


def _thought_body(block: str) -> str:
    """测试辅助：取 <thought> 段正文（与 decide_session_buffer 传给判定的一致）。"""
    from core.strategy import extract_thought

    parsed = extract_thought(block)
    assert parsed["found"] is True
    return parsed["text"]


# ---------------------------------------------------------------------------
# 4. decide_session_buffer：non_substantive + 既有 skip_reason 优先级不变
# ---------------------------------------------------------------------------


class TestDecideSessionBufferFilter:
    def test_all_three_missed_gives_non_substantive(self):
        """三判据全不满足 → 不进缓冲且 reason=non_substantive（不是静默丢）。"""
        r = decide_session_buffer(
            _SHORT_BLOCK,
            skeleton_kind="default",
            session_id="s1",
            turn_index=1,
            tool_calls_count=0,
            user_message="你好呀",
        )
        assert r["buffer"] is False
        assert r["reason"] == "non_substantive"
        # 可观测：解析出的 sections 随诊断返回（不静默丢弃）
        assert r["sections"].get("goal") == "要的是什么（一句话）：确认一下"

    def test_criterion_3_buffers_via_user_message(self):
        r = decide_session_buffer(
            _SHORT_BLOCK,
            skeleton_kind="default",
            session_id="s1",
            turn_index=1,
            tool_calls_count=0,
            user_message="把这个逻辑改一下",
        )
        assert r["buffer"] is True and r["reason"] == ""

    def test_no_session_id_still_takes_priority(self):
        """既有语义不变：no_session_id 优先于实质轮判定。"""
        r = decide_session_buffer(
            _SHORT_BLOCK,
            skeleton_kind="default",
            session_id="",
            turn_index=1,
            tool_calls_count=5,
            user_message="改一下",
        )
        assert r["buffer"] is False and r["reason"] == "no_session_id"

    def test_no_sections_still_takes_priority(self):
        """既有语义不变：no_sections 优先于实质轮判定（缓冲只存编号思考）。"""
        r = decide_session_buffer(
            "<thought>\n自由思考，没有编号。\n</thought>",
            skeleton_kind="code",
            session_id="s1",
            turn_index=1,
            tool_calls_count=5,
            user_message="改一下",
        )
        assert r["buffer"] is False and r["reason"] == "no_sections"


# ---------------------------------------------------------------------------
# 5. 老宿主兼容：不传新字段 → 只有判据二生效，缺字段绝不当成实质轮
# ---------------------------------------------------------------------------


class TestLegacyHostCompat:
    def test_missing_fields_never_substantive_by_themselves(self):
        """纯函数缺省全部新参数：短轮不被判成实质（缺信号 ≠ 实质轮）。"""
        r = decide_session_buffer(_SHORT_BLOCK, skeleton_kind="default", session_id="s1", turn_index=1)
        assert r["buffer"] is False and r["reason"] == "non_substantive"

    def test_legacy_long_thought_with_conclusion_still_buffered(self):
        """老宿主唯一可判的实质性证据（判据二）照常生效：长思考 + 结论字段进缓冲。"""
        block = _long_block_with_conclusion(450)
        r = decide_session_buffer(block, skeleton_kind="default", session_id="s1", turn_index=1)
        assert r["buffer"] is True and r["reason"] == ""

    def test_rest_without_tool_calls_short_turn_skipped(self, db_path):
        """REST 层老宿主（不传 tool_calls_count）：短轮 + 寒暄 → 不进缓冲。"""
        body = (
            _client()
            .post(
                "/lifecycle/post-turn",
                json={
                    "session_id": "sub-legacy-1",
                    "turn_index": 1,
                    "user_message": "你好呀",
                    "assistant_message": f"正文\n{_SHORT_BLOCK}\n正文结束",
                    "skeleton_kind": "default",
                },
            )
            .json()
        )
        assert body["session_buffer"]["buffered"] is False
        assert body["session_buffer"]["reason"] == "non_substantive"

    def test_rest_without_tool_calls_long_thought_buffered(self, db_path):
        """REST 层老宿主：不传 tool_calls_count 但思考够长含结论 → 判据二进缓冲。"""
        body = (
            _client()
            .post(
                "/lifecycle/post-turn",
                json={
                    "session_id": "sub-legacy-2",
                    "turn_index": 1,
                    "user_message": "你好呀",
                    "assistant_message": f"正文\n{_long_block_with_conclusion(450)}\n正文结束",
                    "skeleton_kind": "default",
                },
            )
            .json()
        )
        assert body["session_buffer"]["buffered"] is True
        assert body["session_buffer"]["reason"] == ""


# ---------------------------------------------------------------------------
# 6. 端到端（临时库）：全短轮不产胶囊；混合会话胶囊只含实质轮
# ---------------------------------------------------------------------------


def _session_nodes() -> list[tuple[int, dict]]:
    from mcp_tools import store

    return [
        (nid, payload)
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict) and payload.get("type") == "session"
    ]


def _buffer_by_session(session_id: str) -> tuple[int, dict] | None:
    nodes = [(nid, pl) for nid, pl in _session_nodes() if pl.get("session_id") == session_id]
    assert len(nodes) <= 1, f"同一 session_id 只允许一条缓冲节点，实际 {len(nodes)} 条"
    return nodes[0] if nodes else None


def test_all_short_session_produces_no_capsule(db_path):
    """全会话都是短轮 → 缓冲从不建立，session-end 不产胶囊（capsule 字段缺席）。"""
    client = _client()
    for i in (1, 2):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "sub-e2e-short",
                "turn_index": i,
                "user_message": "你好呀",
                "assistant_message": f"正文\n{_SHORT_BLOCK}\n正文结束",
                "skeleton_kind": "default",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["session_buffer"]["buffered"] is False

    assert _buffer_by_session("sub-e2e-short") is None

    end = client.post("/lifecycle/session-end", json={"session_id": "sub-e2e-short", "messages": []})
    assert end.status_code == 200, end.text
    body = end.json()
    assert "capsule" not in body  # 不产胶囊：字段缺席（且全库无 type=session 节点残留）
    assert _session_nodes() == []


def test_mixed_session_capsule_contains_only_substantive_turns(db_path):
    """混合 session（2 短 + 2 实质）→ 胶囊 turns=2，缓冲里只有那 2 条实质轮。"""
    client = _client()
    turns_spec = [
        # (turn_index, user_message, assistant_thought, tool_calls_count)
        (1, "你好呀", _SHORT_BLOCK, 0),  # 短：寒暄
        (2, "把缓冲逻辑改一下", _SHORT_BLOCK, 0),  # 实质：判据三（指令动词）
        (3, "收到啦", _SHORT_BLOCK, 0),  # 短：确认
        (4, "收到啦", _long_block_with_conclusion(450), 0),  # 实质：判据二（长思考+结论）
    ]
    for idx, message, block, calls in turns_spec:
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "sub-e2e-mixed",
                "turn_index": idx,
                "user_message": message,
                "assistant_message": f"正文\n{block}\n正文结束",
                "skeleton_kind": "default",
                "tool_calls_count": calls,
            },
        )
        assert r.status_code == 200, r.text
        resp = r.json()["session_buffer"]
        expected = idx in (2, 4)
        assert resp["buffered"] is expected, f"turn {idx}: {resp}"
        assert resp["reason"] == ("" if expected else "non_substantive")

    buf = _buffer_by_session("sub-e2e-mixed")
    assert buf is not None
    payload = buf[1]
    assert [t["turn_index"] for t in payload["turns"]] == [2, 4]  # 短轮没进缓冲

    end = client.post("/lifecycle/session-end", json={"session_id": "sub-e2e-mixed", "messages": []})
    assert end.status_code == 200, end.text
    body = end.json()
    assert body["capsule"]["turns"] == 2
    # 胶囊思路节来自实质轮的 judgement（turn 4），不含短轮内容
    assert "拍板就按方案 A 执行" in body["capsule"]["sections"]["thought"]
    assert "确认一下" not in body["capsule"]["sections"]["thought"]


def test_tool_call_signal_buffers_short_thought_end_to_end(db_path):
    """判据一端到端：宿主上报 tool_calls_count=1，短思考也进缓冲。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "sub-e2e-tool",
            "turn_index": 1,
            "user_message": "你好呀",
            "assistant_message": f"正文\n{_SHORT_BLOCK}\n正文结束",
            "skeleton_kind": "default",
            "tool_calls_count": 1,
        },
    ).json()
    assert r["session_buffer"]["buffered"] is True
    buf = _buffer_by_session("sub-e2e-tool")
    assert buf is not None
    assert [t["turn_index"] for t in buf[1]["turns"]] == [1]
