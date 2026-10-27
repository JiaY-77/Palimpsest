"""契约测试：会话胶囊（session-end 一次性提取 + 合并，「骨架即胶囊」收口）。

验收闸门（策略判定在 core/strategy.py，端点执行在 main.py，本文件钉行为）：

  1. parse_thought_sections    <thought> 段按编号 → 胶囊字段（三种骨架 /
     编号变体 / 缺字段 / 自由文本 / 异常输入一律 {} 不抛）
  2. extract_session_thoughts  整场 messages 按序提取每轮 thought（多轮 /
     一条多段 / 缺 role 也扫 / raw 截断 4000 / 空与非列表 → []）
  3. compose_session_capsule   四节合成（空节进 pending / 思路截断 300 /
     handles 拼进 outcome / 空 turns → content 为空）
  4. POST /lifecycle/session-end 端到端（临时库）：写 1 条 type=task +
     kind=handoff 胶囊；同 session 重复 POST 幂等更新不新增；可被
     list_active_tasks(include_handoff=True) 取回；要点路径字段原样
  5. 兼容：session_id="" 照常生成；messages=[] 不写胶囊

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib

import pytest

from core.strategy import (
    THOUGHT_MAX_CHARS,
    compose_session_capsule,
    extract_session_thoughts,
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
    "2. 失败路径（哪里会坏、坏了什么表现、怎么兜住）：解析抛异常会拖垮 session-end\n"
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


# ---------------------------------------------------------------------------
# 1. parse_thought_sections
# ---------------------------------------------------------------------------


class TestParseThoughtSections:
    def test_code_skeleton_sample(self):
        sections = parse_thought_sections(f"正文前缀\n{_CODE_BLOCK}\n正文后缀", "code")
        assert sections == {
            "change": "本轮改什么 / 新增什么（一句话）：给骨架加胶囊字段",
            "failure_path": "失败路径（哪里会坏、坏了什么表现、怎么兜住）：解析抛异常会拖垮 session-end",
            "verify": "怎么验（跑什么、看到什么算数）：pytest 全绿",
            "deliverable": "交付物（PR / 文件路径 / 节点号）：core/strategy.py",
        }

    def test_research_skeleton_sample(self):
        sections = parse_thought_sections(_RESEARCH_BLOCK, "research")
        assert sections == {
            "question": "要查清的问题（一句话）：胶囊四节怎么合成",
            "evidence": "已有证据 / 还缺什么（标明 实测 / 引用 / 推测）：任务书即规格（引用）",
            "counterexample": "反例找了吗（相反结论成立会是什么样）：若节空还硬编会编造内容",
            "conclusion": "结论 / 下一步：空节写（待补）并记 pending",
        }

    def test_default_skeleton_sample(self):
        sections = parse_thought_sections(_DEFAULT_BLOCK, "default")
        assert sections == {
            "goal": "要的是什么（一句话）：确认胶囊可被交接通道取回",
            "known_unknown": "已知 / 未知（还缺什么信息）：还缺端到端证据",
            "judgement": "值不值得记 / 该不该动手：值得，动手写端到端",
            "output": "这一轮的产出或下一步：跑 pytest",
        }

    def test_number_separator_variants(self):
        """编号变体全兼容：1、/ 2．/ 3: / 4：、全半角冒号、前导空白。"""
        text = (
            "<thought>\n"
            "  1、要的是什么（一句话）：一碗白米饭\n"
            "  2．已知 / 未知（还缺什么信息）：还缺米饭的量\n"
            "3: 值不值得记 / 该不该动手：值得\n"
            "4：这一轮的产出或下一步：开饭\n"
            "</thought>"
        )
        sections = parse_thought_sections(text, "default")
        assert sections["goal"] == "要的是什么（一句话）：一碗白米饭"
        assert sections["known_unknown"] == "已知 / 未知（还缺什么信息）：还缺米饭的量"
        assert sections["judgement"] == "值不值得记 / 该不该动手：值得"
        assert sections["output"] == "这一轮的产出或下一步：开饭"

    def test_multiline_answer_and_missing_number(self):
        """多行答案归入同字段；缺编号（3）→ 对应字段不出现。"""
        text = (
            "<thought>\n"
            "1. 要查清的问题（一句话）：查清锁行为\n"
            "2. 已有证据 / 还缺什么（标明 实测 / 引用 / 推测）：已有先例\n"
            "  还缺实测证据\n"
            "4. 结论 / 下一步：先实测\n"
            "</thought>"
        )
        sections = parse_thought_sections(text, "research")
        assert set(sections) == {"question", "evidence", "conclusion"}
        assert sections["evidence"] == "已有证据 / 还缺什么（标明 实测 / 引用 / 推测）：已有先例\n还缺实测证据"

    def test_empty_numbered_line_field_absent(self):
        """空值不编造：编号行后无内容 → 该字段不出现。"""
        text = "<thought>\n1.\n2. 已知 / 未知（还缺什么信息）：已核实\n</thought>"
        sections = parse_thought_sections(text, "default")
        assert "goal" not in sections
        assert sections["known_unknown"] == "已知 / 未知（还缺什么信息）：已核实"

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
        assert set(sections) == {"goal", "known_unknown", "judgement", "output"}
        assert set(parse_thought_sections(_DEFAULT_BLOCK, "")) == set(sections)


# ---------------------------------------------------------------------------
# 2. extract_session_thoughts
# ---------------------------------------------------------------------------


class TestExtractSessionThoughts:
    def test_multi_turn_extracted_in_order(self):
        messages = [
            {"role": "user", "content": "帮我修复这个端点的报错"},
            {"role": "assistant", "content": f"正文\n{_CODE_BLOCK}", "skeleton_kind": "code"},
            {"role": "user", "content": "再安排一下会议纪要"},
            {"role": "assistant", "content": _DEFAULT_BLOCK, "skeleton_kind": "default"},
        ]
        turns = extract_session_thoughts(messages)
        assert [t["turn_index"] for t in turns] == [1, 3]
        assert [t["skeleton_kind"] for t in turns] == ["code", "default"]
        assert turns[0]["sections"]["change"].startswith("本轮改什么")
        assert turns[1]["sections"]["goal"].startswith("要的是什么")

    def test_two_blocks_in_one_message_both_extracted(self):
        messages = [
            {"role": "assistant", "content": f"{_CODE_BLOCK}\n中间正文\n{_RESEARCH_BLOCK}"},
        ]
        turns = extract_session_thoughts(messages)
        assert len(turns) == 2
        assert all(t["turn_index"] == 0 for t in turns)
        assert set(turns[0]["sections"]) == {"change", "failure_path", "verify", "deliverable"}
        assert set(turns[1]["sections"]) == {"question", "evidence", "counterexample", "conclusion"}

    def test_missing_role_scanned_and_user_role_skipped(self):
        messages = [
            {"content": _CODE_BLOCK},  # 缺 role：按内容有无 <thought> 判断 → 提取
            {"role": "user", "content": _DEFAULT_BLOCK},  # 明确 user → 跳过
        ]
        turns = extract_session_thoughts(messages)
        assert len(turns) == 1
        assert turns[0]["turn_index"] == 0

    def test_raw_is_block_original_truncated_to_4000(self):
        long_inner = "长" * 5000
        messages = [{"role": "assistant", "content": f"<thought>\n{long_inner}\n</thought>"}]
        turns = extract_session_thoughts(messages)
        assert len(turns) == 1
        assert len(turns[0]["raw"]) == THOUGHT_MAX_CHARS == 4000
        assert turns[0]["raw"].startswith("<thought>")

    def test_skeleton_kind_defaults_to_empty(self):
        turns = extract_session_thoughts([{"role": "assistant", "content": _DEFAULT_BLOCK}])
        assert turns[0]["skeleton_kind"] == ""

    def test_empty_and_non_list_inputs_return_empty(self):
        assert extract_session_thoughts([]) == []
        assert extract_session_thoughts(None) == []  # type: ignore[arg-type]
        assert extract_session_thoughts("不是列表") == []  # type: ignore[arg-type]
        assert extract_session_thoughts([{"role": "assistant", "content": "没有思考段"}]) == []


# ---------------------------------------------------------------------------
# 3. compose_session_capsule
# ---------------------------------------------------------------------------


class TestComposeSessionCapsule:
    def test_all_four_sections_filled(self):
        turns = extract_session_thoughts(
            [
                {"role": "assistant", "content": _CODE_BLOCK},
                {"role": "assistant", "content": _RESEARCH_BLOCK},
                {"role": "assistant", "content": _DEFAULT_BLOCK},
            ]
        )
        out = compose_session_capsule(turns, session_id="sess-compose-1")
        assert out["turns"] == 3
        assert out["pending"] == []
        assert set(out["sections"]) == {"thought", "outcome", "next", "pitfalls"}
        assert all(body and body != "（待补）" for body in out["sections"].values())
        lines = out["content"].splitlines()
        assert lines[0] == "接续点 / 会话接续 sess-compose-1"
        assert lines[1].startswith("【思路】")
        # 节体可多行，四节标题必须各自起一行（逐字固定，供解析）
        assert "\n【做了什么】" in out["content"]
        assert "\n【下一步】" in out["content"]
        assert "\n【踩的坑】" in out["content"]
        assert "解析抛异常会拖垮 session-end" in out["sections"]["pitfalls"]

    def test_empty_section_goes_to_pending(self):
        turns = [{"turn_index": 0, "sections": {"goal": "要的是什么（一句话）：一碗白米饭"}}]
        out = compose_session_capsule(turns, session_id="s")
        assert out["sections"]["thought"] != "（待补）"
        assert out["sections"]["outcome"] == "（待补）"
        assert out["sections"]["next"] == "（待补）"
        assert out["sections"]["pitfalls"] == "（待补）"
        assert out["pending"] == ["outcome", "next", "pitfalls"]

    def test_thought_section_truncated_at_300(self):
        turns = [{"turn_index": 0, "sections": {"judgement": "长" * 350}}]
        out = compose_session_capsule(turns, session_id="s")
        body = out["sections"]["thought"]
        assert len(body) == 301  # 300 字 + 「…」
        assert body.startswith("长" * 300)
        assert body.endswith("…")

    def test_handles_appended_to_outcome(self):
        turns = [{"turn_index": 0, "sections": {"change": "改了 strategy"}}]
        out = compose_session_capsule(turns, session_id="s", handles=["core/strategy.py", "PR #140"])
        assert out["sections"]["outcome"].endswith("交付物：core/strategy.py、PR #140")
        # handles 为空 / 缺省 → 不拼交付物行
        assert "交付物：" not in compose_session_capsule(turns, session_id="s")["sections"]["outcome"]

    def test_empty_turns(self):
        out = compose_session_capsule([], session_id="s")
        assert out == {
            "content": "",
            "sections": {},
            "pending": ["thought", "outcome", "next", "pitfalls"],
            "turns": 0,
        }

    def test_dedup_and_outcome_split_by_line_semicolon(self):
        turns = [
            {"turn_index": 0, "sections": {"change": "改了 A；改了 B\n改了 A", "deliverable": "x.py"}},
            {"turn_index": 1, "sections": {"change": "改了 A"}},
        ]
        out = compose_session_capsule(turns, session_id="s")
        assert out["sections"]["outcome"] == "改了 A\n改了 B\nx.py"

    def test_next_section_filters_by_marker(self):
        turns = [
            {
                "turn_index": 0,
                "sections": {
                    "verify": "怎么验（跑什么、看到什么算数）：netstat 看监听 PID",  # 无信号词 → 不进 next
                    "conclusion": "结论 / 下一步：后续再实测锁行为",
                },
            },
        ]
        out = compose_session_capsule(turns, session_id="s")
        assert "netstat 看监听 PID" not in out["sections"]["next"]
        assert "后续再实测锁行为" in out["sections"]["next"]


# ---------------------------------------------------------------------------
# 4. 端到端：POST /lifecycle/session-end（临时库）
# ---------------------------------------------------------------------------

_E2E_MESSAGES = [
    {"role": "user", "content": "帮我修复这个端点的报错"},
    {
        "role": "assistant",
        "content": f"正文从这里开始\n{_CODE_BLOCK}",
        "skeleton_kind": "code",
    },
    {"role": "user", "content": "再调研一下胶囊的合成规则"},
    {
        "role": "assistant",
        "content": _RESEARCH_BLOCK,
        "skeleton_kind": "research",
    },
    {
        "role": "assistant",
        "content": _DEFAULT_BLOCK,  # 老宿主：不带 skeleton_kind → 空串
    },
]


def _capsule_nodes_by_session(session_id: str) -> list[tuple[int, dict]]:
    from mcp_tools import store

    return [
        (nid, payload)
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict)
        and payload.get("type") == "task"
        and payload.get("kind") == "handoff"
        and payload.get("session_id") == session_id
    ]


def test_session_end_writes_one_handoff_capsule(db_path):
    client = _client()
    r = client.post("/lifecycle/session-end", json={"session_id": "sess-capsule-e2e", "messages": _E2E_MESSAGES})
    assert r.status_code == 200, r.text
    body = r.json()

    # 老路径字段原样在返回体里
    assert "store" in body and "points" in body and "stored" in body and "tier_distribution" in body

    caps = body["capsule"]
    assert caps["created"] is True
    assert caps["error"] is None
    assert caps["turns"] == 3
    assert caps["pending"] == []
    assert set(caps["sections"]) == {"thought", "outcome", "next", "pitfalls"}
    assert caps["node_id"] is not None

    got = client.get(f"/memory/{caps['node_id']}").json()
    payload = got["payload"]
    assert payload["type"] == "task"
    assert payload["kind"] == "handoff"
    assert payload["session_id"] == "sess-capsule-e2e"
    assert payload["turns"] == 3
    assert payload["importance"] == 0.85
    assert payload["domain"] == "hermes"
    assert set(payload["sections"]) == {"thought", "outcome", "next", "pitfalls"}
    assert set(payload["pending"]) == set()
    assert payload["content"].splitlines()[0] == "接续点 / 会话接续 sess-capsule-e2e"
    assert "【思路】" in payload["content"]


def test_session_end_same_session_is_idempotent_update(db_path):
    client = _client()
    first = client.post(
        "/lifecycle/session-end", json={"session_id": "sess-capsule-idem", "messages": _E2E_MESSAGES}
    ).json()["capsule"]
    assert first["created"] is True and first["node_id"] is not None

    second = client.post(
        "/lifecycle/session-end", json={"session_id": "sess-capsule-idem", "messages": _E2E_MESSAGES}
    ).json()["capsule"]
    assert second["created"] is True
    assert second["node_id"] == first["node_id"]  # 原地更新，不换节点

    nodes = _capsule_nodes_by_session("sess-capsule-idem")
    assert len(nodes) == 1  # 仍然只有一条胶囊
    assert nodes[0][1]["turns"] == 3


def test_session_end_capsule_reachable_via_task_state_channel(db_path):
    from core.task_state import list_active_tasks
    from mcp_tools import store

    client = _client()
    caps = client.post(
        "/lifecycle/session-end", json={"session_id": "sess-capsule-channel", "messages": _E2E_MESSAGES}
    ).json()["capsule"]
    assert caps["created"] is True

    listed = list_active_tasks(store, include_handoff=True)
    assert caps["node_id"] in [item["id"] for item in listed["results"]]
    # 默认通道（include_handoff=False）把接续点排除——互斥验证打标生效
    hidden = list_active_tasks(store, include_handoff=False)
    assert caps["node_id"] not in [item["id"] for item in hidden["results"]]


def test_session_end_empty_messages_writes_no_capsule(db_path):
    client = _client()
    body = client.post("/lifecycle/session-end", json={"session_id": "sess-capsule-empty", "messages": []}).json()
    assert body["capsule"] == {
        "created": False,
        "node_id": None,
        "sections": {},
        "pending": [],
        "turns": 0,
        "error": None,
    }
    assert _capsule_nodes_by_session("sess-capsule-empty") == []


def test_session_end_empty_session_id_still_writes_capsule(db_path):
    client = _client()
    caps = client.post("/lifecycle/session-end", json={"session_id": "", "messages": _E2E_MESSAGES}).json()["capsule"]
    assert caps["created"] is True and caps["node_id"] is not None
    got = client.get(f"/memory/{caps['node_id']}").json()
    assert got["payload"]["session_id"] == ""
    assert got["payload"]["content"].startswith("接续点 / 会话接续 ")


def test_session_end_legacy_memory_path_unaffected(db_path):
    """要点路径（type=memory）行为不变：store/stored/tier_distribution 原样。"""
    client = _client()
    msgs = [
        {"role": "user", "content": "记住这个偏好：报告用极简风"},
        {"role": "user", "content": "不对，标题要左对齐"},
    ]
    body = client.post("/lifecycle/session-end", json={"session_id": "sess-capsule-legacy", "messages": msgs}).json()
    assert body["store"] is True
    assert body["stored"] is True
    assert body["tier_distribution"] == {"facts": 1}
    assert body["capsule"]["created"] is False  # 本场无 thought → 不写胶囊，但不影响要点路径
    got = client.get(f"/memory/{body['node_id']}").json()
    assert got["payload"]["type"] == "memory"
