"""契约测试：思维预设层（thought skeleton / 思维底片）的 lifecycle 落地。

这是「思考入籍」的验收闸门（策略判定在 core/strategy.py，本文件只钉
REST 契约与落库行为）：

  1. POST /lifecycle/pre-turn   按情境返回 thought_skeleton（code/research/
     default 三档互不相同；trivial/过短/宿主关闸 → 空串）
  2. POST /lifecycle/post-turn  从 assistant 消息抽 <thought> 段，写
     type="thought"、tier="logs"、source 带 lifecycle 来源标识；
     与用户原话强信号落库互不阻断
  3. 失败路径全部可观测（skip_reason），不静默吞：不命中 / 未闭合 /
     空段 / 过短 / auto_ingest 关闸 / 入库失败
  4. type="thought" 过 write-guard（无 type 白名单）且不进
     POLICY_PROTECTED_TYPES（保持可修可删）

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本测试新建的节点。

    与 test_lifecycle_endpoints 同款隔离模式：conftest 用 session 级临时库，
    所有测试文件共享；残留节点会污染顺序敏感测试（mem_recent /
    communities / smoke 的 FTS 一致性）。删除节点会级联删边 + 清 FTS
    （delete_node 语义），因此这里只需追踪 node_id 集合差集。
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


def _node_count(client) -> int:
    """库节点数（/mem/stats 是 POST；用下标取键——键缺失即报错而非静默 None）。"""
    return client.post("/mem/stats").json()["totals"]["total_nodes"]


# ---------------------------------------------------------------------------
# 1. pre-turn：按情境动态给思考骨架
# ---------------------------------------------------------------------------


def test_pre_turn_code_scenario_gets_failure_path_skeleton(db_path):
    client = _client()
    r = client.post("/lifecycle/pre-turn", json={"user_message": "帮我修复这个端点的报错"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thought_skeleton_kind"] == "code"
    assert body["thought_skeleton"].startswith("<thought>")
    assert "失败路径" in body["thought_skeleton"]


def test_pre_turn_research_scenario_gets_evidence_skeleton(db_path):
    client = _client()
    r = client.post("/lifecycle/pre-turn", json={"user_message": "调研对比一下这两个方案的可行性"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thought_skeleton_kind"] == "research"
    assert "反例" in body["thought_skeleton"]


def test_pre_turn_default_scenario_gets_generic_skeleton(db_path):
    client = _client()
    r = client.post("/lifecycle/pre-turn", json={"user_message": "帮我安排下周的会议纪要和行程"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thought_skeleton_kind"] == "default"
    assert "值不值得记" in body["thought_skeleton"]


def test_pre_turn_skeletons_differ_across_scenarios(db_path):
    """「动态骨架」的直接证据：三情境三份互不相同的骨架文案。"""
    client = _client()
    scenarios = [
        "帮我修复这个端点的报错",
        "调研对比一下这两个方案的可行性",
        "帮我安排下周的会议纪要和行程",
    ]
    skeletons = set()
    for msg in scenarios:
        body = client.post("/lifecycle/pre-turn", json={"user_message": msg}).json()
        assert body["thought_skeleton"], f"skeleton must not be empty: {msg}"
        skeletons.add(body["thought_skeleton"])
    assert len(skeletons) == 3


def test_pre_turn_trivial_message_gets_no_skeleton(db_path):
    client = _client()
    r = client.post("/lifecycle/pre-turn", json={"user_message": "hi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skip_reason"] == "trivial"  # 既有召回语义保持
    assert body["thought_skeleton"] == ""
    assert body["thought_skeleton_kind"] == ""


def test_pre_turn_skeleton_can_be_disabled_by_host(db_path):
    """宿主自有预设时可关闸：thought_skeleton=false → 空串（判定仍在本体）。"""
    client = _client()
    r = client.post(
        "/lifecycle/pre-turn",
        json={"user_message": "帮我修复这个端点的报错", "thought_skeleton": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thought_skeleton"] == ""


# ---------------------------------------------------------------------------
# 2. post-turn：抽取 <thought> 段 → type="thought" / tier="logs"
# ---------------------------------------------------------------------------

_THOUGHT_BODY = (
    "1. 要的是什么：核实端口占用\n"
    "2. 已核实：服务在 8090 正常\n"
    "3. 失败路径：占用进程没杀干净会双进程争库\n"
    "4. 怎么验：netstat 看监听 PID"
)


def test_post_turn_thought_written_as_thought_type(db_path):
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>\n{_THOUGHT_BODY}\n</thought>\n正文从这里开始",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    thought = body["thought"]
    assert thought["stored"] is True
    assert thought["skip_reason"] == ""
    assert thought["type"] == "thought"
    assert thought["tier"] == "logs"
    nid = thought["node_id"]
    assert nid is not None

    # 回读：真写进了库，type=thought，source 带生命周期来源标识
    got = client.get(f"/memory/{nid}").json()
    assert got["payload"]["type"] == "thought"
    assert "[思维底片]" in got["payload"]["content"]
    assert str(got["payload"].get("source") or "").startswith("lifecycle:post-turn")


def test_post_turn_thought_source_carries_session_id(db_path):
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "s-thought-1",
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>\n{_THOUGHT_BODY}\n</thought>",
        },
    )
    nid = r.json()["thought"]["node_id"]
    got = client.get(f"/memory/{nid}").json()
    assert got["payload"]["source"] == "lifecycle:post-turn:s-thought-1"


def test_post_turn_thought_in_body_middle_is_extracted(db_path):
    """<thought> 出现在正文中间（非开头）照样抽取。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"正文开头先寒暄。\n<thought>\n{_THOUGHT_BODY}\n</thought>\n正文收尾。",
        },
    )
    thought = r.json()["thought"]
    assert thought["stored"] is True
    assert thought["decision_log"]["position"] > 0


def test_post_turn_multiple_blocks_first_segment_wins(db_path):
    """多段：取第一段完整闭合段（一轮一条底片），segments 记总数。"""
    client = _client()
    before = _node_count(client)
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": (
                f"<thought>\n{_THOUGHT_BODY}\n</thought> 中间正文 <thought>\n{_THOUGHT_BODY}（第二段）\n</thought>"
            ),
        },
    )
    body = r.json()
    assert body["thought"]["stored"] is True
    assert body["thought"]["decision_log"]["segments"] == 2
    assert _node_count(client) == before + 1


def test_post_turn_nested_block_takes_first_close(db_path):
    """嵌套不递归解析：取第一个闭合段原样（内层标记保留），nested=True 可观测。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>外层开始 <thought>内层内容 {_THOUGHT_BODY}</thought> 外层尾巴</thought>",
        },
    )
    thought = r.json()["thought"]
    assert thought["stored"] is True
    assert thought["decision_log"]["nested"] is True
    got = client.get(f"/memory/{thought['node_id']}").json()
    assert got["payload"]["content"].startswith("[思维底片] 外层开始 <thought>内层内容")


def test_post_turn_unclosed_block_is_not_stored(db_path):
    """未闭合（有开标记无闭标记）：内容不完整，保守不落库，unclosed 可观测。"""
    client = _client()
    before = _node_count(client)
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"正文开头。\n<thought>\n{_THOUGHT_BODY}\n（思考被截断，无闭合标记）",
        },
    )
    thought = r.json()["thought"]
    assert thought["found"] is False
    assert thought["stored"] is False
    assert thought["skip_reason"] == "unclosed"
    assert _node_count(client) == before


def test_post_turn_no_thought_is_observable_not_silent(db_path):
    """不命中 <thought>：不落库且响应可区分（found/skip_reason），不静默吞。"""
    client = _client()
    before = _node_count(client)
    r = client.post(
        "/lifecycle/post-turn",
        json={"user_message": "今天天气不错", "assistant_message": "普通回复，没有任何思考段。"},
    )
    assert r.status_code == 200
    thought = r.json()["thought"]
    assert thought["found"] is False
    assert thought["stored"] is False
    assert thought["skip_reason"] == "not_found"
    assert _node_count(client) == before


def test_post_turn_empty_and_thin_blocks_not_stored(db_path):
    """空段 / 过短段（模型只回吐骨架没续写）：不落库，skip_reason 可区分。"""
    client = _client()
    cases = {
        "<thought>   </thought>": "empty",
        "<thought>嗯</thought>": "too_thin",
    }
    for msg, expected in cases.items():
        r = client.post(
            "/lifecycle/post-turn",
            json={"user_message": "今天天气不错", "assistant_message": msg},
        )
        thought = r.json()["thought"]
        assert thought["stored"] is False, msg
        assert thought["skip_reason"] == expected, (msg, thought["skip_reason"])


def test_post_turn_overlong_thought_is_truncated(db_path):
    from core import strategy

    body_text = "1. 超长思考：" + "记" * 6000
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={"user_message": "今天天气不错", "assistant_message": f"<thought>\n{body_text}\n</thought>"},
    )
    thought = r.json()["thought"]
    assert thought["stored"] is True
    got = client.get(f"/memory/{thought['node_id']}").json()
    content = got["payload"]["content"]
    assert content.endswith("…[截断]")
    assert len(content) <= len("[思维底片] ") + strategy.THOUGHT_MAX_CHARS + len("…[截断]")


def test_post_turn_auto_ingest_off_skips_thought(db_path):
    client = _client()
    before = _node_count(client)
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "记住这个偏好：报告用极简风",
            "assistant_message": f"<thought>\n{_THOUGHT_BODY}\n</thought>",
            "auto_ingest": False,
        },
    )
    body = r.json()
    assert body["store"] is False  # 用户原话路同闸（既有行为）
    assert body["thought"]["skip_reason"] == "auto_ingest_off"
    assert body["thought"]["stored"] is False
    assert _node_count(client) == before


def test_post_turn_thought_ingest_failure_is_observable(db_path, monkeypatch):
    """入库失败路径：注入 stored=False —— 端点不抛、不写库，
    skip_reason=ingest_failed 且 error 透传（不静默吞）。"""
    import json as _json

    import main as main_mod

    def _fake_ingest(content, **kwargs):
        return _json.dumps({"stored": False, "node_id": None, "error": "mock: 写入事务失败，已回滚"})

    monkeypatch.setattr(main_mod, "_mcp_mem_ingest", _fake_ingest)
    client = _client()
    before = _node_count(client)
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>\n{_THOUGHT_BODY}\n</thought>",
        },
    )
    assert r.status_code == 200, r.text
    thought = r.json()["thought"]
    assert thought["stored"] is False
    assert thought["skip_reason"] == "ingest_failed"
    assert "mock" in thought["error"]
    assert _node_count(client) == before


def test_post_turn_thought_importance_tiers(db_path):
    """importance 分档可解释：基础 0.5；思考里写下决断信号 → 0.7。"""
    client = _client()
    plain = client.post(
        "/lifecycle/post-turn",
        json={"user_message": "今天天气不错", "assistant_message": f"<thought>\n{_THOUGHT_BODY}\n</thought>"},
    ).json()["thought"]
    assert plain["importance"] == 0.5

    decisive = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": "<thought>\n1. 结论是根因在锁竞争，拍板先加超时重试\n</thought>",
        },
    ).json()["thought"]
    assert decisive["importance"] == 0.7


def test_post_turn_thought_and_record_written_independently(db_path):
    """混合轮：用户强信号 + assistant 思考段 → 两条独立落库，互不阻断，
    tier_distribution 汇总本轮全部写入（logs: 2）。"""
    client = _client()
    before = _node_count(client)
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "记住这个偏好：报告用极简风",
            "assistant_message": f"<thought>\n{_THOUGHT_BODY}\n</thought>",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store"] is True and body["stored"] is True  # 用户原话（type=record）
    assert body["thought"]["stored"] is True  # 思维底片（type=thought）
    assert body["thought"]["node_id"] != body["node_id"]
    assert body["tier_distribution"] == {"logs": 2}
    assert _node_count(client) == before + 2


def test_post_turn_thought_only_turn_counts_tier(db_path):
    """thought-only 轮：用户无强信号 → 顶层 store=False（响应结构保持），
    但 thought 已落库，tier_distribution 反映本轮真实写入。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>\n{_THOUGHT_BODY}\n</thought>",
        },
    )
    body = r.json()
    assert body["store"] is False
    assert body["thought"]["stored"] is True
    assert body["tier_distribution"] == {"logs": 1}


def test_post_turn_record_only_regression(db_path):
    """回归保护：assistant 无思考段时，强信号轮的既有行为不变（logs: 1）。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={"user_message": "记住这个偏好：报告用极简风", "assistant_message": "好的"},
    )
    body = r.json()
    assert body["store"] is True
    assert body["stored"] is True
    assert body["tier_distribution"] == {"logs": 1}
    assert body["thought"]["found"] is False
    assert body["thought"]["skip_reason"] == "not_found"


# ---------------------------------------------------------------------------
# 3. write-guard：type="thought" 入籍与保护类型边界
# ---------------------------------------------------------------------------


def test_type_thought_accepted_by_ingest(db_path):
    """type 无白名单：type="thought" 直接入籍不被 check_ingest 拒。"""
    client = _client()
    r = client.post(
        "/mem/ingest",
        json={"content": "契约测试：thought 类型直接入籍", "type": "thought", "importance": 0.5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stored"] is True
    nid = body["node_id"]
    got = client.get(f"/memory/{nid}").json()
    assert got["payload"]["type"] == "thought"


def test_thought_not_a_protected_type():
    """thought 不进 POLICY_PROTECTED_TYPES：默认保护仍只有 rule，
    思维底片保持可修可删（升格 facts/rule 是将来的显式治理动作）。"""
    from config import Config
    from core.policy import is_protected

    assert set(Config.POLICY_PROTECTED_TYPES) == {"rule"}
    assert "thought" not in Config.POLICY_PROTECTED_TYPES
    assert is_protected({"type": "thought"}) is False
    assert is_protected({"type": "rule"}) is True


# ---------------------------------------------------------------------------
# 6. 来源标记：skeleton_kind 事实字段透传 + thought_origin 形态启发
# ---------------------------------------------------------------------------

# 骨架照写样本（引导行与 core/strategy._THOUGHT_SKELETONS["default"] 对应）。
_THOUGHT_SKELETON_BODY = (
    "1. 要的是什么（一句话）：确认列表页空态有没有兜住\n"
    "2. 已知 / 未知：已核实入口存在，还没看空态分支\n"
    "3. 值不值得记：值得\n"
    "4. 该不该动手：先对齐"
)

# 原生思考样本（模型自带推理文本，与骨架无关；真实观测样本）。
_THOUGHT_NATIVE_BODY = "All done. Now report to the user."


def test_post_turn_skeleton_kind_written_to_payload(db_path):
    """宿主上报本轮注入的骨架类型 → 事实字段进 payload；骨架照写 → 启发式判 skeleton。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>\n{_THOUGHT_SKELETON_BODY}\n</thought>",
            "skeleton_kind": "default",
        },
    )
    assert r.status_code == 200, r.text
    thought = r.json()["thought"]
    assert thought["stored"] is True
    assert thought["skeleton_kind"] == "default"
    assert thought["thought_origin"] == "skeleton"

    got = client.get(f"/memory/{thought['node_id']}").json()
    assert got["payload"]["skeleton_kind"] == "default"
    assert got["payload"]["thought_origin"] == "skeleton"


def test_post_turn_without_skeleton_kind_defaults_to_empty(db_path):
    """老宿主不传 skeleton_kind → 空串（兼容）；payload 仍带启发式判定。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>\n{_THOUGHT_SKELETON_BODY}\n</thought>",
        },
    )
    thought = r.json()["thought"]
    assert thought["stored"] is True
    assert thought["skeleton_kind"] == ""
    got = client.get(f"/memory/{thought['node_id']}").json()
    assert got["payload"]["skeleton_kind"] == ""
    assert got["payload"]["thought_origin"] == "skeleton"


def test_post_turn_cross_check_catches_unfollowed_skeleton(db_path):
    """注入了骨架但模型没照写：事实字段与启发式字段交叉暴露差异。"""
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "user_message": "今天天气不错",
            "assistant_message": f"<thought>{_THOUGHT_NATIVE_BODY}</thought>",
            "skeleton_kind": "code",
        },
    )
    thought = r.json()["thought"]
    assert thought["stored"] is True
    assert thought["skeleton_kind"] == "code"
    assert thought["thought_origin"] == "native"

    got = client.get(f"/memory/{thought['node_id']}").json()
    assert got["payload"]["skeleton_kind"] == "code"
    assert got["payload"]["thought_origin"] == "native"
