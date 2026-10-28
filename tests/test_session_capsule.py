"""契约测试：会话胶囊（「骨架即胶囊」——post-turn 累进缓冲，session-end 合并胶囊）。

验收闸门（策略判定在 core/strategy.py 纯函数，端点执行在 main.py，本文件钉行为）：

  1. parse_thought_sections    <thought> 编号答案 → 胶囊字段（三种骨架正常样本 /
     缺编号 / 空值 / 无 <thought> / 全角半角混排 / 同行带内容 / 异常输入一律 {}）
  2. decide_session_buffer     四种 reason：no_session_id / no_sections /
     non_substantive（issue #170 实质轮筛选）/ 正常
  3. compose_session_capsule   四节合成（空节进 pending / 思路超 300 纯截断 /
     handles 拼进 outcome 末尾 / 去重 / next 信号词过滤）
  4. 端到端（临时库，不碰生产）：3 轮 post-turn（同 session_id 不同 turn_index）
     → 缓冲节点只有 1 条、turns 3 项；同 turn_index 重复提交幂等；session-end
     → 1 条 type=task + kind=handoff 胶囊、缓冲节点 closed、CONSOLIDATED_INTO 边、
     可被 list_active_tasks(include_handoff=True) 取回
  5. 兼容：session_id="" 行为与改动前一致（不写 session 节点、不报错）

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib

import pytest

from core.strategy import (
    CAPSULE_THOUGHT_MAX_CHARS,
    SESSION_BUFFER_RAW_MAX_CHARS,
    SESSION_BUFFER_STATUS_CLOSED,
    SESSION_BUFFER_TYPE,
    compose_session_capsule,
    decide_session_buffer,
    detect_thought_origin,
    extract_handles,
    parse_thought_sections,
)


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本测试新建的节点。

    与 test_thought_lifecycle 同款隔离模式：conftest 用 session 级临时库，
    所有测试文件共享；残留节点会污染顺序敏感测试。删除节点会级联删边 +
    清 FTS（delete_node 语义），因此这里只需追踪 node_id 集合差集。
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
        with contextlib.suppress(Exception):  # 清理尽力而为
            store.delete_node(nid)


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# 样本：模型「照着骨架逐项续写」的 <thought> 段（引导行与模板一一对应）
# ---------------------------------------------------------------------------

_CODE_BLOCK = (
    "<thought>\n"
    "1. 本轮改什么 / 新增什么（一句话）：给骨架加胶囊字段\n"
    "2. 失败路径（哪里会坏、坏了什么表现、怎么兜住）：解析抛异常会拖垮落库\n"
    "3. 怎么验（跑什么、看到什么算数）：pytest 全绿\n"
    "4. 交付物（PR / 文件路径 / 节点号）：core/strategy.py\n"
    "</thought>"
)

_RESEARCH_BLOCK = (
    "<thought>\n"
    "1. 要查清的问题（一句话）：胶囊四节怎么合成\n"
    "2. 已有证据 / 还缺什么（标明 实测 / 引用 / 推测）：任务书即规格（引用）\n"
    "3. 反例找了吗（相反结论成立会是什么样）：若节空还硬编会编造内容\n"
    "4. 结论 / 下一步：空节写（待补）并记 pending\n"
    "</thought>"
)

_DEFAULT_BLOCK = (
    "<thought>\n"
    "1. 要的是什么（一句话）：确认胶囊可被交接通道取回\n"
    "2. 已知 / 未知（还缺什么信息）：还缺端到端证据\n"
    "3. 值不值得记 / 该不该动手：值得，动手写端到端\n"
    "4. 这一轮的产出或下一步：跑 pytest\n"
    "</thought>"
)

# 旧版骨架文案（模板改版前的引导行）——存量 thought 节点长这样。
_LEGACY_CODE_TEXT = (
    "1. 要改什么 / 新增什么（一句话）：旧模板的产物\n"
    "2. 现状核实了吗（哪些已核实、哪些还没看）：已核实\n"
    "3. 失败路径（哪里会坏、坏了什么表现、怎么兜住）：无\n"
    "4. 怎么验（跑什么、看到什么算数）：单测"
)


# ---------------------------------------------------------------------------
# 1. parse_thought_sections — 编号答案 → 胶囊字段
# ---------------------------------------------------------------------------


class TestParseThoughtSections:
    def test_code_skeleton_sample(self):
        sections = parse_thought_sections(f"正文前缀\n{_CODE_BLOCK}\n正文后缀", "code")
        assert sections == {
            "thought": "本轮改什么 / 新增什么（一句话）：给骨架加胶囊字段",
            "outcome": "失败路径（哪里会坏、坏了什么表现、怎么兜住）：解析抛异常会拖垮落库",
            "next": "怎么验（跑什么、看到什么算数）：pytest 全绿",
            "pitfalls": "交付物（PR / 文件路径 / 节点号）：core/strategy.py",
        }

    def test_research_skeleton_sample(self):
        sections = parse_thought_sections(_RESEARCH_BLOCK, "research")
        assert sections == {
            "thought": "要查清的问题（一句话）：胶囊四节怎么合成",
            "outcome": "已有证据 / 还缺什么（标明 实测 / 引用 / 推测）：任务书即规格（引用）",
            "next": "反例找了吗（相反结论成立会是什么样）：若节空还硬编会编造内容",
            "pitfalls": "结论 / 下一步：空节写（待补）并记 pending",
        }

    def test_default_skeleton_sample(self):
        sections = parse_thought_sections(_DEFAULT_BLOCK, "default")
        assert sections == {
            "thought": "要的是什么（一句话）：确认胶囊可被交接通道取回",
            "outcome": "已知 / 未知（还缺什么信息）：还缺端到端证据",
            "next": "值不值得记 / 该不该动手：值得，动手写端到端",
            "pitfalls": "这一轮的产出或下一步：跑 pytest",
        }

    def test_number_separator_variants_and_leading_whitespace(self):
        """编号分隔符认 . / 、 / ．，容忍前导空白；冒号只作行内标签分隔不作编号分隔。"""
        text = (
            "<thought>\n"
            "  1、要的是什么（一句话）：一碗白米饭\n"
            "  2．已知 / 未知（还缺什么信息）：还缺米饭的量\n"
            "3. 值不值得记 / 该不该动手：值得\n"
            "4. 这一轮的产出或下一步：开饭\n"
            "</thought>"
        )
        sections = parse_thought_sections(text, "default")
        assert sections["thought"] == "要的是什么（一句话）：一碗白米饭"
        assert sections["outcome"] == "已知 / 未知（还缺什么信息）：还缺米饭的量"
        assert sections["next"] == "值不值得记 / 该不该动手：值得"
        assert sections["pitfalls"] == "这一轮的产出或下一步：开饭"

    def test_fullwidth_halfwidth_colon_mix(self):
        """全/半角冒号混排：都是「编号行 标签：答案」的合法形态。"""
        text = "<thought>\n1. 要的是什么(一句话): 查空态\n2. 已知 / 未知（还缺什么信息）：缺截图\n</thought>"
        sections = parse_thought_sections(text, "default")
        assert sections["thought"] == "要的是什么(一句话): 查空态"
        assert sections["outcome"] == "已知 / 未知（还缺什么信息）：缺截图"

    def test_multiline_answer_and_missing_number(self):
        """多行答案归入同字段；缺编号（3）→ 对应字段不出现，其余照常。"""
        text = (
            "<thought>\n"
            "1. 要查清的问题（一句话）：查清锁行为\n"
            "2. 已有证据 / 还缺什么（标明 实测 / 引用 / 推测）：已有先例\n"
            "  还缺实测证据\n"
            "4. 结论 / 下一步：先实测\n"
            "</thought>"
        )
        sections = parse_thought_sections(text, "research")
        assert set(sections) == {"thought", "outcome", "pitfalls"}
        assert sections["outcome"] == "已有证据 / 还缺什么（标明 实测 / 引用 / 推测）：已有先例\n  还缺实测证据"

    def test_same_line_answer_is_captured(self):
        """编号之后同行若有内容要取到（任务书样例句式）。"""
        text = "<thought>\n1. 本轮改什么：加了 session_id 落库\n</thought>"
        sections = parse_thought_sections(text, "code")
        assert sections["thought"] == "本轮改什么：加了 session_id 落库"

    def test_template_verbatim_line_then_answer_below(self):
        """模型原样回吐模板行、答案写在下一行：标签回显原样保留（不猜、不改写），
        答案行跟在值里——值的边界仍到下一个编号行为止。"""
        text = "<thought>\n1. 本轮改什么 / 新增什么（一句话）：\n重构了 strategy 模块\n</thought>"
        sections = parse_thought_sections(text, "code")
        assert sections["thought"] == "本轮改什么 / 新增什么（一句话）：\n重构了 strategy 模块"

    def test_empty_numbered_line_field_absent(self):
        """空值不编造：编号行后无内容 → 该字段不出现。"""
        text = "<thought>\n1.\n2. 已知 / 未知（还缺什么信息）：已核实\n</thought>"
        sections = parse_thought_sections(text, "default")
        assert "thought" not in sections
        assert sections["outcome"] == "已知 / 未知（还缺什么信息）：已核实"

    def test_out_of_range_number_section_dropped(self):
        """编号超出映射范围（5+）：该段不归属任何字段，不污染其他字段。"""
        text = (
            "<thought>\n"
            "1. 要的是什么（一句话）：查库存\n"
            "5. 题外话：这一段没有字段可挂\n"
            "2. 已知 / 未知（还缺什么信息）：缺截图\n"
            "</thought>"
        )
        sections = parse_thought_sections(text, "default")
        assert sections["thought"] == "要的是什么（一句话）：查库存"
        assert sections["outcome"] == "已知 / 未知（还缺什么信息）：缺截图"
        assert "pitfalls" not in sections

    def test_free_text_without_numbers_returns_empty(self):
        assert parse_thought_sections("<thought>\n先看堆栈第二帧再改，别急着动手。\n</thought>", "code") == {}

    def test_without_thought_block_returns_empty(self):
        assert parse_thought_sections("普通回复，没有思考段", "code") == {}

    def test_non_string_and_empty_inputs_safe(self):
        assert parse_thought_sections(None, "code") == {}  # type: ignore[arg-type]
        assert parse_thought_sections(123, "code") == {}  # type: ignore[arg-type]
        assert parse_thought_sections("", "code") == {}
        assert parse_thought_sections("   ", "code") == {}

    def test_unknown_kind_falls_back_to_default_map(self):
        """skeleton_kind 空 / 未知 → default 映射（不猜成其他骨架的字段）。"""
        sections = parse_thought_sections(_DEFAULT_BLOCK, "bogus")
        assert set(sections) == {"thought", "outcome", "next", "pitfalls"}
        assert set(parse_thought_sections(_DEFAULT_BLOCK, "")) == set(sections)


# ---------------------------------------------------------------------------
# 2. decide_session_buffer — 三种 reason
# ---------------------------------------------------------------------------


class TestDecideSessionBuffer:
    def test_no_session_id_not_buffered(self):
        r = decide_session_buffer(_DEFAULT_BLOCK, skeleton_kind="default", session_id="", turn_index=1)
        assert r["buffer"] is False
        assert r["reason"] == "no_session_id"
        assert r["sections"] == {}

    def test_whitespace_session_id_counts_as_empty(self):
        r = decide_session_buffer(_DEFAULT_BLOCK, skeleton_kind="default", session_id="   ", turn_index=1)
        assert r["buffer"] is False and r["reason"] == "no_session_id"

    def test_no_thought_block_no_sections(self):
        r = decide_session_buffer("普通回复", skeleton_kind="default", session_id="s1", turn_index=1)
        assert r["buffer"] is False and r["reason"] == "no_sections"

    def test_free_text_thought_falls_back_to_thought_section(self):
        """有 <thought> 原文但没有编号答案 → 原文截断进 thought 节照收（宁粗不空，issue #216）。

        旧行为是整轮 no_sections 丢弃——原生思考轮全被挡在缓冲外；连
        <thought> 原文都没有的轮才维持 no_sections（见 test_no_thought_block_no_sections）。
        """
        r = decide_session_buffer(
            "<thought>\n自由思考，没有编号。\n</thought>", skeleton_kind="code", session_id="s1", turn_index=1
        )
        assert r["buffer"] is True and r["reason"] == ""
        assert r["sections"] == {"thought": "自由思考，没有编号。"}
        assert r["raw"] == "自由思考，没有编号。"

    def test_normal_turn_is_buffered(self):
        r = decide_session_buffer(_CODE_BLOCK, skeleton_kind="code", session_id="s1", turn_index=7)
        assert r["buffer"] is True
        assert r["reason"] == ""
        assert r["skeleton_kind"] == "code"
        assert set(r["sections"]) == {"thought", "outcome", "next", "pitfalls"}
        assert r["raw"].startswith("1. 本轮改什么")

    def test_skeleton_kind_normalized(self):
        """白名单外的 kind 归空串 → 按 default 映射解析（与落库事实字段同一套归一）。"""
        r = decide_session_buffer(_DEFAULT_BLOCK, skeleton_kind="bogus", session_id="s1", turn_index=1)
        assert r["skeleton_kind"] == ""
        assert set(r["sections"]) == {"thought", "outcome", "next", "pitfalls"}

    def test_raw_truncated_to_4000(self):
        long_block = "<thought>\n1. 要的是什么（一句话）：" + "长" * 5000 + "\n</thought>"
        r = decide_session_buffer(long_block, skeleton_kind="default", session_id="s1", turn_index=1)
        assert r["buffer"] is True
        assert len(r["raw"]) == SESSION_BUFFER_RAW_MAX_CHARS == 4000


# ---------------------------------------------------------------------------
# 3. compose_session_capsule — 四节合成
# ---------------------------------------------------------------------------


def _turn(sections: dict) -> dict:
    return {"turn_index": 0, "skeleton_kind": "default", "sections": sections, "raw": ""}


class TestComposeSessionCapsule:
    def test_all_four_sections_filled(self):
        turns = [
            _turn(parse_thought_sections(_CODE_BLOCK, "code")),
            _turn(parse_thought_sections(_RESEARCH_BLOCK, "research")),
            _turn(parse_thought_sections(_DEFAULT_BLOCK, "default")),
        ]
        out = compose_session_capsule(turns, session_id="sess-compose-1")
        assert out["turns"] == 3
        assert out["pending"] == []
        assert set(out["sections"]) == {"thought", "outcome", "next", "pitfalls"}
        assert all(body and body != "（待补）" for body in out["sections"].values())
        lines = out["content"].splitlines()
        assert lines[0] == "接续点 / 会话接续 sess-compose-1"
        assert lines[1].startswith("【思路】")
        # 四节标题逐字固定、各自起一行
        assert "\n【做了什么】" in out["content"]
        assert "\n【下一步】" in out["content"]
        assert "\n【踩的坑】" in out["content"]
        assert "解析抛异常会拖垮落库" in out["sections"]["outcome"]

    def test_thought_priority_per_turn(self):
        """每轮取 judgement / conclusion / goal 里第一个非空（优先级按此顺序）。"""
        turns = [
            _turn({"goal": "要的是什么：A", "judgement": "值不值得记：B"}),
            _turn({"conclusion": "结论：C"}),
            _turn({"goal": "要的是什么：D"}),
        ]
        out = compose_session_capsule(turns, session_id="s")
        assert out["sections"]["thought"] == "值不值得记：B\n结论：C\n要的是什么：D"

    def test_empty_section_goes_to_pending(self):
        turns = [_turn({"goal": "要的是什么（一句话）：一碗白米饭"})]
        out = compose_session_capsule(turns, session_id="s")
        assert out["sections"]["thought"] != "（待补）"
        assert out["sections"]["outcome"] == "（待补）"
        assert out["sections"]["next"] == "（待补）"
        assert out["sections"]["pitfalls"] == "（待补）"
        assert out["pending"] == ["outcome", "next", "pitfalls"]

    def test_thought_section_truncated_at_300(self):
        """思路节总长 ≤300 字：纯截断（不加省略号，保证 ≤300 的合同）。"""
        turns = [_turn({"judgement": "长" * 350})]
        out = compose_session_capsule(turns, session_id="s")
        body = out["sections"]["thought"]
        assert len(body) == CAPSULE_THOUGHT_MAX_CHARS == 300
        assert body == "长" * 300

    def test_handles_appended_to_outcome_end(self):
        turns = [_turn({"change": "改了 strategy", "deliverable": "core/strategy.py"})]
        out = compose_session_capsule(turns, session_id="s", handles=["PR #140", "节点 42"])
        lines = out["sections"]["outcome"].splitlines()
        assert lines[0] == "改了 strategy"
        assert lines[1] == "core/strategy.py"
        assert lines[-2] == "PR #140"  # handles 拼在末尾
        assert lines[-1] == "节点 42"

    def test_outcome_deduped_across_turns(self):
        turns = [
            _turn({"change": "改了 A", "deliverable": "x.py"}),
            _turn({"change": "改了 A"}),
        ]
        out = compose_session_capsule(turns, session_id="s")
        assert out["sections"]["outcome"] == "改了 A\nx.py"

    def test_next_section_filters_by_marker(self):
        """next 只收带「下一步 / 待 / 需 / 再」的行；无信号词的行不进。"""
        turns = [
            _turn(
                {
                    "verify": "怎么验：netstat 看监听 PID\n需要看到监听 PID 消失",
                    "output": "本轮产出：胶囊初稿",
                }
            ),
            _turn({"verify": "跑 pytest，待全绿后收工"}),
        ]
        out = compose_session_capsule(turns, session_id="s")
        assert "netstat 看监听 PID" not in out["sections"]["next"]
        assert "本轮产出：胶囊初稿" not in out["sections"]["next"]
        assert "需要看到监听 PID 消失" in out["sections"]["next"]
        assert "跑 pytest，待全绿后收工" in out["sections"]["next"]

    def test_empty_turns_all_pending(self):
        out = compose_session_capsule([], session_id="s")
        assert set(out["sections"]) == {"thought", "outcome", "next", "pitfalls"}
        assert all(body == "（待补）" for body in out["sections"].values())
        assert out["pending"] == ["thought", "outcome", "next", "pitfalls"]
        assert out["turns"] == 0


# ---------------------------------------------------------------------------
# 4. 端到端（临时库）：post-turn 累进缓冲 → session-end 合并胶囊
# ---------------------------------------------------------------------------


def _session_nodes() -> list[tuple[int, dict]]:
    """全库的 type=session 缓冲节点。"""
    from mcp_tools import store

    return [
        (nid, payload)
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict) and payload.get("type") == SESSION_BUFFER_TYPE
    ]


def _buffer_by_session(session_id: str) -> tuple[int, dict] | None:
    nodes = [(nid, pl) for nid, pl in _session_nodes() if pl.get("session_id") == session_id]
    assert len(nodes) <= 1, f"同一 session_id 只允许一条缓冲节点，实际 {len(nodes)} 条"
    return nodes[0] if nodes else None


def test_post_turn_three_turns_accumulate_into_one_buffer(db_path):
    """3 轮 post-turn（同 session_id、不同 turn_index）→ 缓冲节点只有 1 条、turns 3 项。"""
    client = _client()
    for i, (block, kind) in enumerate(
        [(_CODE_BLOCK, "code"), (_RESEARCH_BLOCK, "research"), (_DEFAULT_BLOCK, "default")], start=1
    ):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "cap-e2e-1",
                "turn_index": i,
                "user_message": f"第 {i} 轮：帮我看看胶囊相关的问题",
                "assistant_message": f"正文开始\n{block}\n正文结束",
                "skeleton_kind": kind,
                # issue #170 实质轮筛选后，进缓冲需要实质轮——本组测的是缓冲
                # 累进/幂等/合并机制（不是筛选判据，判据在 test_substantive_turn），
                # 样本统一按「实质轮宿主」构造（带工具调用信号），断言不变。
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # 第三条路径与 thought 落库路径并存：两条都成功
        assert body["thought"]["stored"] is True
        assert body["session_buffer"]["buffered"] is True
        assert body["session_buffer"]["reason"] == ""
        assert body["session_buffer"]["error"] is None

    from config import Config

    buf = _buffer_by_session("cap-e2e-1")
    assert buf is not None
    payload = buf[1]
    assert payload["status"] == "active"
    assert payload["session_id"] == "cap-e2e-1"
    assert isinstance(payload["started_at"], float)
    assert len(payload["turns"]) == 3
    assert payload["last_turn_index"] == 3
    assert [t["turn_index"] for t in payload["turns"]] == [1, 2, 3]
    assert [t["skeleton_kind"] for t in payload["turns"]] == ["code", "research", "default"]
    assert payload["turns"][0]["sections"]["thought"].startswith("本轮改什么")
    assert payload["turns"][0]["raw"].startswith("1. 本轮改什么")
    # 缓冲节点是过程簿记：type=session 已登记 TIER_LOGS（不进 facts 默认注入池）
    assert "session" in Config.TIER_LOGS


def test_post_turn_same_turn_index_is_idempotent(db_path):
    """同一 turn_index 重复提交：覆盖不追加（turns 仍 3 项，内容换成新答案）。"""
    client = _client()
    base = {"session_id": "cap-e2e-2", "user_message": "看看幂等", "skeleton_kind": "default"}
    for turn_index in (1, 2, 3):
        r = client.post(
            "/lifecycle/post-turn",
            json={**base, "turn_index": turn_index, "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n"},
        )
        assert r.json()["session_buffer"]["buffered"] is True

    replaced = _DEFAULT_BLOCK.replace("确认胶囊可被交接通道取回", "换成新的答案")
    r = client.post(
        "/lifecycle/post-turn",
        json={**base, "turn_index": 2, "assistant_message": f"正文\n{replaced}\n"},
    )
    assert r.json()["session_buffer"]["buffered"] is True

    buf = _buffer_by_session("cap-e2e-2")
    assert buf is not None
    payload = buf[1]
    assert len(payload["turns"]) == 3  # 仍是 3 项，不是 4
    assert [t["turn_index"] for t in payload["turns"]] == [1, 2, 3]
    assert payload["turns"][1]["sections"]["thought"] == "要的是什么（一句话）：换成新的答案"


def test_session_end_writes_capsule_closes_buffer_and_links(db_path):
    """session-end：1 条 type=task + kind=handoff 胶囊；缓冲 closed；CONSOLIDATED_INTO 边。"""
    client = _client()
    for i in (1, 2):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "cap-e2e-3",
                "turn_index": i,
                "user_message": f"第 {i} 轮",
                "assistant_message": f"正文\n{_CODE_BLOCK}\n",
                "skeleton_kind": "code",
            },
        )
        assert r.json()["session_buffer"]["buffered"] is True

    from mcp_tools import store

    buf = _buffer_by_session("cap-e2e-3")
    assert buf is not None
    buf_nid, _ = buf

    end = client.post("/lifecycle/session-end", json={"session_id": "cap-e2e-3", "messages": []})
    assert end.status_code == 200, end.text
    body = end.json()
    capsule = body["capsule"]
    assert capsule["node_id"] is not None
    assert capsule["turns"] == 2
    assert set(capsule["sections"]) == {"thought", "outcome", "next", "pitfalls"}
    assert isinstance(capsule["pending"], list)

    # 胶囊节点回读：type/kind/importance/domain/session_id/sections/pending/turns/content
    got = client.get(f"/memory/{capsule['node_id']}").json()
    payload = got["payload"]
    assert payload["type"] == "task"
    assert payload["kind"] == "handoff"
    assert payload["importance"] == 0.85
    assert payload["domain"] == "hermes"
    assert payload["session_id"] == "cap-e2e-3"
    assert payload["turns"] == 2
    assert set(payload["sections"]) == {"thought", "outcome", "next", "pitfalls"}
    assert payload["pending"] == capsule["pending"]
    assert payload["content"].splitlines()[0] == "接续点 / 会话接续 cap-e2e-3"
    assert "【思路】" in payload["content"] and "【踩的坑】" in payload["content"]

    # 缓冲节点 closed + CONSOLIDATED_INTO 边指向胶囊
    buf_after = store.get_node(buf_nid).get("payload", {})
    assert buf_after.get("status") == SESSION_BUFFER_STATUS_CLOSED
    edges = store.get_edges(buf_nid)
    assert any(
        getattr(e, "target_id", None) == capsule["node_id"] and getattr(e, "label", "") == "CONSOLIDATED_INTO"
        for e in edges
    )


def test_capsule_reachable_via_tasks_active_channel(db_path):
    """胶囊能被 tasks_active(include_handoff=true) 取到（同进程直调 list_active_tasks）。"""
    from core.task_state import list_active_tasks
    from mcp_tools import store

    client = _client()
    for i in (1, 2):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "cap-e2e-4",
                "turn_index": i,
                "user_message": f"第 {i} 轮",
                "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
                "skeleton_kind": "default",
            },
        )
        assert r.json()["session_buffer"]["buffered"] is True
    end = client.post("/lifecycle/session-end", json={"session_id": "cap-e2e-4", "messages": []})
    capsule = end.json()["capsule"]
    assert capsule["node_id"] is not None

    listed = list_active_tasks(store, include_handoff=True, fresh_days=0)
    ids = [item["id"] for item in listed["results"]]
    assert capsule["node_id"] in ids
    # 默认通道（include_handoff=False）把接续点排除——互斥验证打标生效
    hidden = list_active_tasks(store, include_handoff=False, fresh_days=0)
    assert capsule["node_id"] not in [item["id"] for item in hidden["results"]]


def test_session_end_without_buffer_unchanged_response(db_path):
    """无缓冲的 session-end：返回体不加 capsule 字段，既有字段不变（回归保护）。"""
    client = _client()
    body = client.post(
        "/lifecycle/session-end",
        json={
            "session_id": "cap-no-buffer",
            "messages": [{"role": "user", "content": "记住：周报用极简风模板"}],
        },
    ).json()
    assert body["store"] is True
    assert body["stored"] is True
    assert body["tier_distribution"] == {"facts": 1}
    assert "capsule" not in body


def test_session_memory_switch_off_skips_legacy_path_only(db_path):
    """Config.SESSION_MEMORY_INGEST=False：旧要点路径停用（可观测），胶囊路径照常。"""
    from config import Config

    client = _client()
    for i in (1, 2):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "cap-e2e-5",
                "turn_index": i,
                "user_message": f"第 {i} 轮",
                "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
                "skeleton_kind": "default",
            },
        )
        assert r.json()["session_buffer"]["buffered"] is True

    original = Config.SESSION_MEMORY_INGEST
    Config.SESSION_MEMORY_INGEST = False
    try:
        end = client.post(
            "/lifecycle/session-end",
            json={
                "session_id": "cap-e2e-5",
                "messages": [{"role": "user", "content": "记住：这次要写要点"}],
            },
        )
        body = end.json()
        assert body["store"] is False
        assert body["skip_reason"] == "session_memory_disabled"
        # 胶囊路径不受开关影响
        assert body["capsule"]["node_id"] is not None
    finally:
        Config.SESSION_MEMORY_INGEST = original


def test_session_buffer_failure_is_observable_not_blocking(db_path):
    """缓冲累计失败：不阻断既有两条落库路径，session_buffer.error 透传。"""
    import main as main_mod

    client = _client()
    original = main_mod._append_session_buffer_turn

    def _boom(session_id, turn_index, buf):
        raise RuntimeError("mock: 缓冲节点写入失败")

    main_mod._append_session_buffer_turn = _boom
    try:
        before = _session_nodes()
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "cap-e2e-6",
                "turn_index": 1,
                "user_message": "看看失败路径",
                "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
                "skeleton_kind": "default",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        sb = body["session_buffer"]
        assert sb["buffered"] is False
        assert sb["node_id"] is None
        assert "mock" in sb["error"]
        # 前两条路径不受影响：thought 照常落库
        assert body["thought"]["stored"] is True
        assert len(_session_nodes()) == len(before)
    finally:
        main_mod._append_session_buffer_turn = original


# ---------------------------------------------------------------------------
# 5. 兼容：session_id="" 行为与改动前一致
# ---------------------------------------------------------------------------


def test_empty_session_id_post_turn_writes_no_session_node(db_path):
    """老宿主（无 session_id）：不写缓冲节点、不报错，thought 路径照旧。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "",
            "turn_index": 5,
            "user_message": "老宿主的一轮",
            "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
            "skeleton_kind": "default",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thought"]["stored"] is True
    assert body["session_buffer"] == {"buffered": False, "node_id": None, "reason": "no_session_id", "error": None}
    assert _session_nodes() == []  # 全库没有任何 type=session 节点

    # session-end 同样：无 capsule 字段、无缓冲节点残留
    end = client.post("/lifecycle/session-end", json={"session_id": "", "messages": []}).json()
    assert "capsule" not in end
    assert _session_nodes() == []


def test_detect_thought_origin_tolerates_legacy_skeleton_text():
    """模板改版后，旧骨架文案的存量节点：起手式不再匹配 → 不再判 skeleton（可接受
    降级），但绝不抛异常——回填与交叉核对通道对此容忍（任务书兼容要求）。"""
    result = detect_thought_origin(_LEGACY_CODE_TEXT)
    assert result in ("native", "unknown")  # 降级到任一非 skeleton 值均可
    assert detect_thought_origin(f"[思维底片] {_LEGACY_CODE_TEXT}") == result


# ---------------------------------------------------------------------------
# 胶囊每节不得残留「问句回显」（2026-10-28）
#
# 实测产出过这样的胶囊：【做了什么】做了什么：新增 dashboard_api.py…
# ——编号行冒号后的内容原先原样保留（含标签回显），每节都顶着重复的问句。
# 问句是固定四句，所以按 `_CAPSULE_QUESTIONS` 精确匹配剥掉，剥不到就不动。
# ---------------------------------------------------------------------------


def test_parse_strips_question_echo_including_paren_clause():
    """回显带括号补充说明（``做了什么（改了什么 / 查清了什么）：``）也要剥干净。"""
    text = (
        "<thought>\n"
        "1. 这一轮的核心判断 / 思路（一句话）：把端点抽成注入式 router。\n"
        "2. 做了什么（改了什么 / 查清了什么）：补 13 条测试。\n"
        "3. 下一步 / 待办：确认 8010 下线。\n"
        "4. 踩的坑 / 风险：本地测试假绿。\n"
        "</thought>"
    )
    sections = parse_thought_sections(text, "code")
    assert sections["thought"] == "把端点抽成注入式 router。"
    assert sections["outcome"] == "补 13 条测试。"
    assert sections["next"] == "确认 8010 下线。"
    assert sections["pitfalls"] == "本地测试假绿。"


def test_parse_strips_plain_echo_without_parens():
    text = "<thought>\n1. 这一轮的核心判断 / 思路：答案直接写。\n2. 做了什么：两节都正常。\n</thought>"
    sections = parse_thought_sections(text, "default")
    assert sections["thought"] == "答案直接写。"
    assert sections["outcome"] == "两节都正常。"


def test_parse_only_strips_leading_echo_never_body():
    """只剥行首回显一次：答案正文里再出现同样的词不碰（少剥只是难看，多剥会吃内容）。"""
    # 第 2 问才是「做了什么」（第 1 问是核心判断）；回显只剥行首那一次
    text = "<thought>\n2. 做了什么：我做了什么：其实只修了两处。\n</thought>"
    sections = parse_thought_sections(text, "default")
    assert sections["outcome"] == "我做了什么：其实只修了两处。"


def test_parse_keeps_multi_line_answer_after_echo():
    """回显后换行续写的多行答案，续行逐字保留。"""
    text = "<thought>\n2. 做了什么：\n第一行\n第二行\n</thought>"
    sections = parse_thought_sections(text, "default")
    assert sections["outcome"] == "第一行\n第二行"


# ---------------------------------------------------------------------------
# 6. M1：一 session 一胶囊（issue #187 先查后建 / 全量轮次）+ 单次写（#198）
# ---------------------------------------------------------------------------


def _capsules_by_session(session_id: str) -> list[tuple[int, dict]]:
    """全库中该 session 的会话胶囊（type=task + kind=handoff + session_id 相等）。"""
    from mcp_tools import store

    return [
        (nid, payload)
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict)
        and payload.get("type") == "task"
        and payload.get("kind") == "handoff"
        and payload.get("session_id") == session_id
    ]


def test_capsule_scenario_a_one_session_one_capsule(db_path):
    """场景 A：post-turn ×2 → session-end → 1 条胶囊、turns==2、四节齐全。"""
    client = _client()
    for i, (block, kind) in enumerate([(_CODE_BLOCK, "code"), (_RESEARCH_BLOCK, "research")], start=1):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "cap-m1-a",
                "turn_index": i,
                "user_message": f"第 {i} 轮：改改胶囊",
                "assistant_message": f"正文\n{block}\n",
                "skeleton_kind": kind,
            },
        )
        assert r.json()["session_buffer"]["buffered"] is True
    end = client.post("/lifecycle/session-end", json={"session_id": "cap-m1-a", "messages": []})
    capsules = _capsules_by_session("cap-m1-a")
    assert len(capsules) == 1, f"应只 1 条胶囊，实际 {[nid for nid, _ in capsules]}"
    nid, payload = capsules[0]
    assert end.json()["capsule"]["node_id"] == nid
    assert payload["turns"] == 2
    assert set(payload["sections"]) == {"thought", "outcome", "next", "pitfalls"}
    assert all(body != "（待补）" for body in payload["sections"].values())
    assert payload["pending"] == []
    # #208 配套：新建路径不写 closed_at（此刻还没有会话接手过它）
    assert "closed_at" not in payload


def test_session_end_with_partial_sections_still_writes_capsule(db_path):
    """回归（2026-10-28 生产）：只要有一节为空，compose 就会把「（待补）」写进
    sections 并把该节列进 pending；胶囊护栏的旧判定把它当「非空却列待补」判矛盾，
    于是**整条胶囊被拒写入**（生产日志：会话胶囊生成失败 … pending 与 sections 矛盾）。
    自动胶囊天然常有空节（原料是每轮 <thought>，多数轮只填一两问），这条必须绿。
    """
    client = _client()
    partial = "<thought>\n1. 这一轮的核心判断 / 思路（一句话）：只填第一格\n</thought>"
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "cap-partial",
            "turn_index": 1,
            "user_message": "改一下胶囊空节处理",
            "assistant_message": f"正文\n{partial}\n",
            "skeleton_kind": "default",
        },
    )
    assert r.json()["session_buffer"]["buffered"] is True
    end = client.post("/lifecycle/session-end", json={"session_id": "cap-partial", "messages": []})
    body = end.json()
    assert body.get("capsule"), f"部分节为空的会话必须仍能产出胶囊，实际返回：{body}"
    cap = body["capsule"]
    assert cap["sections"]["thought"], "有内容的那节要保留"
    assert cap["sections"]["outcome"] == "（待补）"
    assert set(cap["pending"]) == {"outcome", "next", "pitfalls"}


def test_capsule_scenario_b_mid_session_end_still_one_capsule(db_path):
    """场景 B（#187 回归）：post-turn → session-end → post-turn → session-end
    → 仍只 1 条胶囊、turns 累积为 2、node_id 未变。改动前第二次 session-end 会
    新建第二条胶囊（同一 session_id 两条，生产 3564/3578 实测），本测试在修复
    前必须红。"""
    client = _client()
    r1 = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "cap-m1-b",
            "turn_index": 1,
            "user_message": "第一段活：改改胶囊",
            "assistant_message": f"正文\n{_CODE_BLOCK}\n",
            "skeleton_kind": "code",
        },
    )
    assert r1.json()["session_buffer"]["buffered"] is True
    end1 = client.post("/lifecycle/session-end", json={"session_id": "cap-m1-b", "messages": []})
    first_id = end1.json()["capsule"]["node_id"]

    r2 = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "cap-m1-b",
            "turn_index": 2,
            "user_message": "第二段活：改改胶囊",
            "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
            "skeleton_kind": "default",
        },
    )
    assert r2.json()["session_buffer"]["buffered"] is True
    end2 = client.post("/lifecycle/session-end", json={"session_id": "cap-m1-b", "messages": []})
    capsules = _capsules_by_session("cap-m1-b")
    assert len(capsules) == 1, f"#187 回归：应仍只 1 条胶囊，实际 {[nid for nid, _ in capsules]}"
    nid, payload = capsules[0]
    assert nid == first_id, "node_id 必须不变（原节点更新，不是新建）"
    assert end2.json()["capsule"]["node_id"] == nid
    assert payload["turns"] == 2, "轮次要全量累积（中途 session-end 之前的轮不能丢）"
    # #208 配套：更新路径要打 closed_at（这场会话的接手到此为止）
    assert payload.get("closed_at"), "更新已有胶囊时必须写 closed_at"
    # 重复 session-end：幂等空操作——不产第二条、返回体不加 capsule 字段
    end3 = client.post("/lifecycle/session-end", json={"session_id": "cap-m1-b", "messages": []})
    assert len(_capsules_by_session("cap-m1-b")) == 1
    assert "capsule" not in end3.json()


def test_capsule_scenario_c_updated_content_reflects_new_turns(db_path):
    """场景 C：更新路径下胶囊 content 四节反映**合并后**的轮次内容（正文重合成，
    不是只留最新轮）。"""
    client = _client()
    r1 = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "cap-m1-c",
            "turn_index": 1,
            "user_message": "先查一个问题",
            "assistant_message": f"正文\n{_RESEARCH_BLOCK}\n",
            "skeleton_kind": "research",
        },
    )
    assert r1.json()["session_buffer"]["buffered"] is True
    client.post("/lifecycle/session-end", json={"session_id": "cap-m1-c", "messages": []})
    r2 = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "cap-m1-c",
            "turn_index": 2,
            "user_message": "再补一轮收尾",
            "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
            "skeleton_kind": "default",
        },
    )
    assert r2.json()["session_buffer"]["buffered"] is True
    end = client.post("/lifecycle/session-end", json={"session_id": "cap-m1-c", "messages": []})
    capsule = end.json()["capsule"]
    payload = client.get(f"/memory/{capsule['node_id']}").json()["payload"]
    # 新轮（default 骨架）与旧轮（research 骨架）的思路 / 坑都在四节里
    assert "确认胶囊可被交接通道取回" in payload["sections"]["thought"]  # 新轮
    assert "胶囊四节怎么合成" in payload["sections"]["thought"]  # 旧轮不丢
    assert "跑 pytest" in payload["sections"]["pitfalls"]  # 新轮
    assert "空节写（待补）并记 pending" in payload["sections"]["pitfalls"]  # 旧轮
    assert payload["content"].splitlines()[0] == "接续点 / 会话接续 cap-m1-c"


def test_capsule_create_single_write_sections_survive_supplement_failure(db_path, monkeypatch):
    """#198：sections / pending 由 mem_ingest 单次写落库——建节点之后的簿记补写
    全部失败时四节仍在（旧两步写会留下无结构化字段的半状态胶囊）。"""
    from core.trivium_store import TriviumStore

    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "cap-m1-single",
            "turn_index": 1,
            "user_message": "单次写验证",
            "assistant_message": f"正文\n{_CODE_BLOCK}\n",
            "skeleton_kind": "code",
        },
    )
    assert r.json()["session_buffer"]["buffered"] is True

    original = TriviumStore.update_payload
    targeted: list[int] = []

    def _flaky(self, node_id, new_payload):
        # 只打断「胶囊簿记补写」（payload 同时带 session_id + turns 是本链路
        # 结构化补写的特征）；mem_ingest 内部的 last_touched_at 补写、缓冲关闭
        # 照常放行——精确模拟「补写失败」而非「全库写坏」。mock 的是协作者
        # store，不是被测函数本身。
        if isinstance(new_payload, dict) and "session_id" in new_payload and "turns" in new_payload:
            targeted.append(node_id)
            raise RuntimeError("mock: 胶囊簿记补写失败")
        return original(self, node_id, new_payload)

    monkeypatch.setattr(TriviumStore, "update_payload", _flaky)
    end = client.post("/lifecycle/session-end", json={"session_id": "cap-m1-single", "messages": []})
    capsule = end.json()["capsule"]
    assert targeted, "注入的补写失败必须真的命中（否则测试没咬住单次写契约）"
    payload = client.get(f"/memory/{capsule['node_id']}").json()["payload"]
    # 四节结构来自 mem_ingest 单次写：簿记补写失败也不缺
    assert set(payload["sections"]) == {"thought", "outcome", "next", "pitfalls"}
    assert isinstance(payload["pending"], list)
    assert payload["content"].splitlines()[0] == "接续点 / 会话接续 cap-m1-single"
    # 补写失败的字段如实缺席：turns 不在（宁可缺，不假装写成功）
    assert "turns" not in payload


# ---------------------------------------------------------------------------
# 7. M2：可核验句柄（extract_handles 纯函数 + 骨架第 2 问改文案 + 胶囊带句柄）
# ---------------------------------------------------------------------------


class TestExtractHandles:
    def test_pr_and_issue_numbers(self):
        assert extract_handles(["提了 PR #140，另见 #187"]) == ["#140", "#187"]

    def test_sha_full_and_backticked_short(self):
        full = "a" * 40
        assert extract_handles([f"commit {full} 与 `247e876`"]) == [full, "247e876"]
        # 裸短串（无反引号）不算句柄；反引号里的 40 位走完整 sha 形态，不产短串
        assert extract_handles(["裸 247e876 无反引号"]) == []
        assert extract_handles([f"`{full}`"]) == [full]

    def test_windows_and_unix_paths(self):
        got = extract_handles(["改了 D:/HeJiaQi/x.py，然后跑了 /var/log/palimpsest.log"])
        assert got == ["D:/HeJiaQi/x.py", "/var/log/palimpsest.log"]
        # 相对路径不得咬出碎片句柄（/strategy.py 这种路径不存在——宁可没有不产出错的）
        assert extract_handles(["改了 core/strategy.py 和 tests/x.py"]) == []
        # 中文目录名是合法路径成分；尾部全角标点是行文的，不进句柄
        assert extract_handles(["改了 D:/HeJiaQi/Documents/Novel/乾元风云录/角色.md，已归档"]) == [
            "D:/HeJiaQi/Documents/Novel/乾元风云录/角色.md"
        ]

    def test_service_addr_pid_and_node(self):
        got = extract_handles(["服务在 127.0.0.1:8090 与 localhost:8000，PID 12345、pid=99，看节点 3564 与 node 12"])
        assert got == ["127.0.0.1:8090", "localhost:8000", "PID 12345", "pid=99", "节点 3564", "node 12"]

    def test_dedupe_and_first_occurrence_order(self):
        got = extract_handles(["#140 先出现，#187 后出现，再来一次 #140 和 `247e876`、#187"])
        assert got == ["#140", "#187", "247e876"]

    def test_limit_truncates_keeping_first_occurrences(self):
        blobs = [" ".join(f"#{i}" for i in range(100, 120))]
        assert len(extract_handles(blobs)) == 12
        handles = extract_handles(blobs)
        assert handles[0] == "#100" and handles[-1] == "#111"
        assert extract_handles(blobs, limit=3) == ["#100", "#101", "#102"]

    def test_no_match_returns_empty_not_noise(self):
        assert extract_handles(["今天吃了两碗白米饭，什么句柄都没有"]) == []
        assert extract_handles([]) == []
        assert extract_handles(["", None]) == []  # type: ignore[list-item]

    def test_overlap_claims_by_priority(self):
        """URL 里的片段不得重复产出：服务地址整体认领，``p://`` / ``/dashboard``
        碎片全部让路。"""
        got = extract_handles(["面板在 http://127.0.0.1:8090/dashboard 可用"])
        assert got == ["127.0.0.1:8090"]


def test_skeleton_outcome_question_asks_for_handles_and_echo_still_stripped():
    """M2 骨架文案：第 2 问要求带可核验句柄；问句主干不变（做了什么）→ 新旧回显
    都照剥（_strip_question_echo 按「主干 + 可选括号」匹配，不钉括号原文）。"""
    from core.strategy import decide_thought_skeleton

    skeleton = decide_thought_skeleton("帮我把这个函数改成幂等的", enabled=True)["skeleton"]
    assert "带可核验句柄：PR 号 / commit / 路径" in skeleton
    # 新问句全文回显（模型照抄引导行再写答案）→ 剥到只剩答案
    new_echo = (
        "<thought>\n2. 做了什么（改了什么 / 查清了什么，带可核验句柄：PR 号 / commit / 路径）：修好了 #187\n</thought>"
    )
    assert parse_thought_sections(new_echo, "default")["outcome"] == "修好了 #187"
    # 旧文案回显（存量 thought 节点 / 老缓冲轮次）→ 照剥
    old_echo = "<thought>\n2. 做了什么（改了什么 / 查清了什么）：补 4 条测试\n</thought>"
    assert parse_thought_sections(old_echo, "default")["outcome"] == "补 4 条测试"


def test_capsule_outcome_carries_extracted_handles(db_path):
    """M2 端到端：缓冲轮里写了 PR 号 / 路径 → 自动胶囊 outcome 节带句柄
    （此前调用方不传 handles，自动胶囊一条可核验句柄都没有）。"""
    thought = (
        "<thought>\n"
        "1. 这一轮的核心判断 / 思路（一句话）：两步写会留半状态，收敛为单次写。\n"
        "2. 做了什么（改了什么 / 查清了什么，带可核验句柄：PR 号 / commit / 路径）：改了 D:/repo/main.py，见 PR #187\n"
        "3. 下一步 / 待办：待主人验收。\n"
        "4. 踩的坑 / 风险：无。\n"
        "</thought>"
    )
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "cap-m2-handle",
            "turn_index": 1,
            "user_message": "修一下句柄问题",
            "assistant_message": f"正文\n{thought}\n",
            "skeleton_kind": "default",
        },
    )
    assert r.json()["session_buffer"]["buffered"] is True
    end = client.post("/lifecycle/session-end", json={"session_id": "cap-m2-handle", "messages": []})
    payload = client.get(f"/memory/{end.json()['capsule']['node_id']}").json()["payload"]
    assert "#187" in payload["sections"]["outcome"]
    assert "D:/repo/main.py" in payload["sections"]["outcome"]


# ---------------------------------------------------------------------------
# 胶囊更新路径：手写补写内容不得被机器合成覆盖（2026-10-28 实测事故）
#
# 背景：`_finalize_session_capsule` 的更新路径原先是 update_content 硬覆盖，而胶囊
# 正文里可能有人按接续点口径补写的四节（不在缓冲 turns 里，机器合成不出来）。一次
# 1 轮缓冲的 session-end 或断线自愈补产就会把 1761 字手写接续点静默盖掉。现改为
# core.strategy.merge_capsule_with_existing 按行追加去重。
# ---------------------------------------------------------------------------

from core.strategy import CAPSULE_SECTION_TBD, merge_capsule_with_existing  # noqa: E402

_SECTION_NAMES = ("thought", "outcome", "next", "pitfalls")


def _tbd_composed():
    return {
        "sections": dict.fromkeys(_SECTION_NAMES, CAPSULE_SECTION_TBD),
        "pending": list(_SECTION_NAMES),
        "turns": 1,
    }


def test_merge_keeps_existing_when_incoming_is_tbd():
    """新合成全是「（待补）」→ 四节原样保留旧内容，pending 清空。"""
    existing = {
        "thought": "手写的思路",
        "outcome": "手写的产出：PR #214（0f9d52c）",
        "next": "手写的下一步：重启服务验证",
        "pitfalls": "手写的坑：只测了 happy path",
    }
    merged = merge_capsule_with_existing(_tbd_composed(), existing, session_id="s-merge-1")

    assert merged["sections"] == existing
    assert merged["pending"] == []
    assert merged["content"].startswith("接续点 / 会话接续 s-merge-1\n")
    assert "手写的产出：PR #214（0f9d52c）" in merged["content"]


def test_merge_appends_and_dedupes_lines():
    """两者都有内容 → 按行追加去重（旧在前、保序），重复行只留一条。"""
    existing = {"thought": "旧思路", "outcome": "旧产出\n共同产出", "next": "旧下一步", "pitfalls": "旧坑"}
    composed = {
        "sections": {
            "thought": "新思路",
            "outcome": "共同产出\n新产出",
            "next": CAPSULE_SECTION_TBD,
            "pitfalls": "新坑",
        },
        "pending": ["next"],
        "turns": 2,
    }
    merged = merge_capsule_with_existing(composed, existing, session_id="s-merge-2")

    assert merged["sections"]["thought"] == "旧思路\n新思路"
    assert merged["sections"]["outcome"] == "旧产出\n共同产出\n新产出"  # 共同产出 只一次
    assert merged["sections"]["next"] == "旧下一步"  # 新的空 → 保留旧
    assert merged["sections"]["pitfalls"] == "旧坑\n新坑"
    assert merged["pending"] == []
    assert merged["turns"] == 2  # turns 是缓冲事实，不从旧值并集来


def test_merge_without_existing_uses_incoming():
    """没有已有胶囊（新建路径的语义）→ 行为与纯合成一致。"""
    composed = {
        "sections": {
            "thought": "新思路",
            "outcome": "新产出",
            "next": CAPSULE_SECTION_TBD,
            "pitfalls": CAPSULE_SECTION_TBD,
        },
        "pending": ["next", "pitfalls"],
        "turns": 1,
    }
    merged = merge_capsule_with_existing(composed, None, session_id="s-merge-3")

    assert merged["sections"]["thought"] == "新思路"
    assert merged["sections"]["next"] == CAPSULE_SECTION_TBD
    assert merged["pending"] == ["next", "pitfalls"]


def test_session_end_does_not_overwrite_handwritten_content(db_path):
    """端到端：胶囊已存在且含手写四节 → 再来一轮 + session-end → 手写内容仍在、新轮也进来。"""
    from mcp_tools import store

    client = _client()
    sid = "cap-e2e-merge"
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": sid,
            "turn_index": 1,
            "user_message": "第一轮：先看缓冲",
            "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
            "skeleton_kind": "default",
        },
    )
    assert r.json()["session_buffer"]["buffered"] is True
    first = client.post("/lifecycle/session-end", json={"session_id": sid, "messages": [], "auto_ingest": True})
    capsule_id = first.json()["capsule"]["node_id"]

    handwritten = {
        "thought": "手写思路：这一轮真正要解决的是断线自愈",
        "outcome": "手写产出：capsule 更新改为按行合并",
        "next": "手写下一步：重启服务验证孤儿补产",
        "pitfalls": "手写坑：验收只测 happy path 会漏空节分支",
    }
    store.update_payload(
        capsule_id,
        {"sections": handwritten, "content": f"接续点 / 会话接续 {sid}\n【思路】{handwritten['thought']}"},
    )

    # 第二轮：缓冲在上次 session-end 已 closed，post-turn 另建一条 active 缓冲
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": sid,
            "turn_index": 2,
            "user_message": "第二轮：再补一轮",
            "assistant_message": f"正文\n{_DEFAULT_BLOCK}\n",
            "skeleton_kind": "default",
        },
    )
    assert r.json()["session_buffer"]["buffered"] is True

    second = client.post("/lifecycle/session-end", json={"session_id": sid, "messages": [], "auto_ingest": True})
    body = second.json()
    assert "capsule" in body, body
    assert body["capsule"]["node_id"] == capsule_id  # 同一节点，不新建

    payload = store.get_node(capsule_id)["payload"]
    sections = payload["sections"]
    for name, text in handwritten.items():
        assert text in sections[name], f"{name} 的手写内容被机器合成覆盖了：{sections[name]!r}"
    assert payload["pending"] == []
    # 新轮内容也确实进了正文（不是只有手写部分）
    assert "要的是什么" in sections["thought"]


def test_session_end_new_capsule_still_creates(db_path):
    """对照：新建路径不受合并影响（无既有胶囊 → 正常产出四节 + pending）。"""
    client = _client()
    sid = "cap-e2e-merge-new"
    client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": sid,
            "turn_index": 1,
            "user_message": "只填一格",
            "assistant_message": "正文\n<thought>\n1. 本轮改什么：只填了第一格\n</thought>\n",
            "skeleton_kind": "default",
        },
    )
    r = client.post("/lifecycle/session-end", json={"session_id": sid, "messages": [], "auto_ingest": True})
    capsule = r.json()["capsule"]
    assert capsule["turns"] == 1
    assert "只填了第一格" in capsule["sections"]["thought"]
    assert set(capsule["pending"]) == {"outcome", "next", "pitfalls"}
