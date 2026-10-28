"""契约测试：实质轮筛选（「骨架即胶囊」第二刀，issue #170）。

会话缓冲只收「实质轮」——三条判据任一满足（判定在 core/strategy.py 纯函数
is_substantive_turn，端点只取值与执行，本文件钉行为）：

  （判据一「工具调用」已按 issue #194 删除：宿主从不填该字段，且空 sections 在更上游
   就被 no_sections 挡掉，该判据在任何真实链路里都到不了）
  2. 判据二（长思考+结论） 正文 ≥ 门槛（strip 后）且结论类字段任一非空 → 实质
     （含恰好等于门槛的边界；只有长复述没有结论字段 → 不实质）
  3. 判据三（指令动词）  用户消息含指令动词 → 实质；**问句但含指令动词也算**
     （指令 ≠ 问句）；纯疑问 / 寒暄 → 不实质
  4. 三判据全不满足 → 不进缓冲且 skip_reason=non_substantive（可观测，不静默
     丢弃）；no_session_id / no_sections 既有语义优先级不变
  5. 老宿主兼容：不传新字段（纯函数不传 user_message）
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

# 「填了格子」的轮：四问都有编号答案（正文很短）。统一骨架后判据 2 = 任一非空
# 格子即实质轮，所以这是**正样本**——「每轮顺手填一次」就是这个形态。
_SHORT_BLOCK = (
    "<thought>\n"
    "1. 这一轮的核心判断 / 思路：确认一下\n"
    "2. 做了什么（改了什么 / 查清了什么）：无\n"
    "3. 下一步 / 待办：无\n"
    "4. 踩的坑 / 风险：无\n"
    "</thought>"
)

# 「没填格子」的轮：只有编号行、四问全空 → 解析不出任何字段，缓冲走 no_sections。
# 闲聊轮不会拿到骨架（decide_thought_skeleton 对 trivial 返回空），模型也就不会
# 按编号写——所以这个样本同时也是「噪声轮」的真实形态。
# 注意：编号行后面不能留任何文字——parse_thought_sections 会把同行内容原样
# 当作答案（标签回显也算），那样就成了「填了格子」。真空格 = 只有编号和分隔符。
_EMPTY_BLOCK = "<thought>\n1.\n2.\n3.\n4.\n</thought>"


def _sections_of(block: str, kind: str = "default") -> dict[str, str]:
    return parse_thought_sections(block, kind)


# ---------------------------------------------------------------------------
# 1-3. is_substantive_turn：三条判据各一组正反例
# ---------------------------------------------------------------------------


class _RemovedCriterionToolCalls:  # issue #194：判据 1 已删除，用例随之移除
    pass


class TestCriterionFilledSections:
    """判据 2（2026-10-28 放宽，issue #188）：**填了胶囊格子就算实质轮**。

    旧规则是「正文 ≥400 字 且 落在结论字段上」，与「每轮顺手填一次」的设计意图
    冲突：实测全库缓冲 `turns` 恒为 1、自动胶囊只产 3 条，短填全被挡在门外。
    统一骨架后四问直接对应四节，任一非空格子就是一次有效填写。
    """

    def test_short_filled_thought_is_substantive(self):
        """四问都填、正文很短 → 实质（旧门槛下会被判 False，这是设计变化点）。"""
        assert is_substantive_turn(_thought_body(_SHORT_BLOCK), _sections_of(_SHORT_BLOCK)) is True

    def test_single_filled_section_suffices(self):
        """只填一格也算：顺手填一次不可能每题都写满。"""
        for field in ("thought", "outcome", "next", "pitfalls"):
            assert is_substantive_turn("短", {field: "x"}) is True, field

    def test_all_sections_empty_not_substantive(self):
        assert is_substantive_turn("短", {}) is False

    def test_blank_values_do_not_count(self):
        """空白字符串不算填写。"""
        assert is_substantive_turn("短", {"thought": "   ", "outcome": ""}) is False

    def test_non_string_section_values_ignored(self):
        """sections 值非字符串（异常输入）不算填写，绝不抛异常。"""
        bad = {"thought": 123, "outcome": None}  # type: ignore[dict-item]
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
# 4. decide_session_buffer：non_substantive + skip_reason（no_sections 收窄到
#    「无内容可缓冲」，原生思考兜底进 thought 节，issue #216）
# ---------------------------------------------------------------------------


class TestDecideSessionBufferFilter:
    def test_empty_sections_gives_no_sections(self):
        """四问全空 → 解析不出字段 → no_sections（缓冲只存结构化思考）。

        放宽判据 2 之后「有格子就进缓冲」，所以 `non_substantive` 在当前链路里
        不再触达——空思考在更早的 no_sections 分支就被挡住了（见下方老宿主用例）。
        """
        r = decide_session_buffer(
            _EMPTY_BLOCK,
            skeleton_kind="default",
            session_id="s1",
            turn_index=1,
            user_message="你好呀",
        )
        assert r["buffer"] is False
        assert r["reason"] == "no_sections"

    def test_criterion_3_buffers_via_user_message(self):
        r = decide_session_buffer(
            _SHORT_BLOCK,
            skeleton_kind="default",
            session_id="s1",
            turn_index=1,
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
            user_message="改一下",
        )
        assert r["buffer"] is False and r["reason"] == "no_session_id"

    def test_free_text_thought_no_longer_dropped(self):
        """issue #216：原生思考（无编号行）不再整轮丢弃——原文兜底进 thought 节。

        旧语义是「no_sections 优先于实质轮判定」，自由思考轮全被挡在缓冲外；
        现在 no_sections 收窄到「连 <thought> 原文都没有」或「含编号行但四问
        全空」（见 test_empty_sections_gives_no_sections 与老宿主用例）。
        """
        r = decide_session_buffer(
            "<thought>\n自由思考，没有编号。\n</thought>",
            skeleton_kind="code",
            session_id="s1",
            turn_index=1,
            user_message="改一下",
        )
        assert r["buffer"] is True and r["reason"] == ""
        assert r["sections"] == {"thought": "自由思考，没有编号。"}


# ---------------------------------------------------------------------------
# 5. 老宿主兼容：不传新字段 → 只有判据二生效，缺字段绝不当成实质轮
# ---------------------------------------------------------------------------


class TestLegacyHostCompat:
    """老宿主不传新字段：判据 2 只看「填没填格子」，与宿主能力无关，照常生效。"""

    def test_empty_thought_never_buffered_without_signals(self):
        """四问全空 + 无工具 + 无指令 → 不进缓冲（缺信号 ≠ 实质轮）。"""
        r = decide_session_buffer(_EMPTY_BLOCK, skeleton_kind="default", session_id="s1", turn_index=1)
        assert r["buffer"] is False and r["reason"] == "no_sections"

    def test_short_filled_thought_buffered_without_host_signals(self):
        """填了格子（正文很短）+ 老宿主不传任何新字段 → 照常进缓冲。"""
        r = decide_session_buffer(_SHORT_BLOCK, skeleton_kind="default", session_id="s1", turn_index=1)
        assert r["buffer"] is True and r["reason"] == ""

    def test_rest_without_tool_calls_empty_thought_skipped(self, db_path):
        """REST 层老宿主（不传任何新字段）：空思考（四问全空）→ 不进缓冲。"""
        body = (
            _client()
            .post(
                "/lifecycle/post-turn",
                json={
                    "session_id": "sub-legacy-1",
                    "turn_index": 1,
                    "user_message": "你好呀",
                    "assistant_message": f"正文\n{_EMPTY_BLOCK}\n正文结束",
                    "skeleton_kind": "default",
                },
            )
            .json()
        )
        assert body["session_buffer"]["buffered"] is False
        assert body["session_buffer"]["reason"] == "no_sections"

    def test_rest_without_tool_calls_filled_thought_buffered(self, db_path):
        """REST 层老宿主：不传新字段但四问填了格子 → 照常进缓冲。"""
        body = (
            _client()
            .post(
                "/lifecycle/post-turn",
                json={
                    "session_id": "sub-legacy-2",
                    "turn_index": 1,
                    "user_message": "你好呀",
                    "assistant_message": f"正文\n{_SHORT_BLOCK}\n正文结束",
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


def test_all_empty_session_produces_no_capsule(db_path):
    """全会话都是空思考（四问全空）→ 缓冲从不建立，session-end 不产胶囊。"""
    client = _client()
    for i in (1, 2):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "sub-e2e-short",
                "turn_index": i,
                "user_message": "你好呀",
                "assistant_message": f"正文\n{_EMPTY_BLOCK}\n正文结束",
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


def test_mixed_session_capsule_contains_only_filled_turns(db_path):
    """混合 session（2 空思考 + 2 填了格子）→ 胶囊 turns=2，缓冲只收填了格子的轮。"""
    client = _client()
    turns_spec = [
        # (turn_index, user_message, assistant_thought)
        (1, "你好呀", _EMPTY_BLOCK),  # 空思考：寒暄（无骨架可填）
        (2, "把缓冲逻辑改一下", _SHORT_BLOCK),  # 填了格子 → 实质
        (3, "收到啦", _EMPTY_BLOCK),  # 空思考：确认
        (4, "收到啦", _SHORT_BLOCK),  # 填了格子 → 实质
    ]
    for idx, message, block in turns_spec:
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "sub-e2e-mixed",
                "turn_index": idx,
                "user_message": message,
                "assistant_message": f"正文\n{block}\n正文结束",
                "skeleton_kind": "default",
            },
        )
        assert r.status_code == 200, r.text
        resp = r.json()["session_buffer"]
        expected = idx in (2, 4)
        assert resp["buffered"] is expected, f"turn {idx}: {resp}"
        # 空思考轮的 skip 原因是 no_sections（缓冲只存结构化思考），不是 non_substantive
        assert resp["reason"] == ("" if expected else "no_sections")

    buf = _buffer_by_session("sub-e2e-mixed")
    assert buf is not None
    payload = buf[1]
    assert [t["turn_index"] for t in payload["turns"]] == [2, 4]  # 短轮没进缓冲

    end = client.post("/lifecycle/session-end", json={"session_id": "sub-e2e-mixed", "messages": []})
    assert end.status_code == 200, end.text
    body = end.json()
    assert body["capsule"]["turns"] == 2
    # 思路节只由填了格子的实质轮贡献（两轮内容相同 → 去重后一条）
    assert "确认一下" in body["capsule"]["sections"]["thought"]
