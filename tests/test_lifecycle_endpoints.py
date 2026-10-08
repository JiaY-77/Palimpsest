"""契约测试：本体 lifecycle 协议端点（策略引擎的宿主无关接口）。

这是「策略引擎下沉」的验收闸门：证明本体自己就能完成
「召不召 / 抽不抽 / 记哪层 / 去重」的全部判断——即宿主适配器被压薄之后，
行为没有退化。

覆盖：
  1. POST /lifecycle/pre-turn      简单输入跳过；命中则返回可注入文本 + 决策日志
  2. POST /lifecycle/post-turn     强信号才写；写 logs 层；无聊文本不写
  3. POST /lifecycle/session-end   要点写 facts 层；近似重复跳过
  4. POST /lifecycle/pre-compress  只回文本、不写库

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本测试新建的节点。

    conftest 用 session 级临时库，所有测试文件共享；后续的顺序敏感测试
    （mem_recent 的「全局最新 N 条」、communities 的图连通性、smoke 的 FTS
    一致性）会被残留节点污染。删除节点会级联删边 + 清 FTS（delete_node 语义），
    因此这里只需追踪 node_id 集合差集。
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
# 1. pre-turn
# ---------------------------------------------------------------------------


def test_pre_turn_trivial_is_skipped(db_path):
    client = _client()
    r = client.post("/lifecycle/pre-turn", json={"user_message": "hi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skip"] is True
    assert body["skip_reason"] == "trivial"
    assert body["inject_text"] == ""


def test_pre_turn_high_min_score_yields_no_hit(db_path):
    """min_score=0.99：现有记忆都达不到 → 不注入（与库里已有哪些节点无关）。"""
    client = _client()
    r = client.post("/lifecycle/pre-turn", json={"user_message": "帮我查一下上周的会议记录", "min_score": 0.99})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inject_text"] == ""


def test_pre_turn_injects_existing_memory(db_path):
    from mcp_tools import store

    emb = store.embed_text("契约测试：项目代号是凤凰，部署在杭州机房")
    nid = store.insert_node(
        {
            "type": "memory",
            "content": "契约测试：项目代号是凤凰，部署在杭州机房",
            "importance": 0.8,
            "domain": "hermes",
        },
        emb,
    )
    client = _client()
    r = client.post("/lifecycle/pre-turn", json={"user_message": "项目代号是什么？部署在哪里？", "domain": "hermes"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skip"] is False
    assert "Palimpsest 记忆注入" in body["inject_text"]
    assert any(b.get("source") == f"mem#{nid}" for b in body["inject_blocks"])
    assert body["decision_log"]["kept"] >= 1


# ---------------------------------------------------------------------------
# 2. post-turn
# ---------------------------------------------------------------------------


def test_post_turn_strong_signal_writes_logs_tier(db_path):
    client = _client()
    r = client.post(
        "/lifecycle/post-turn", json={"user_message": "记住这个偏好：我喜欢深色模式", "assistant_message": "好的"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store"] is True
    assert body["stored"] is True
    assert body["node_id"] is not None
    assert body["tier_distribution"] == {"logs": 1}

    # 回读节点：证明真写进了库，且是 logs 层（type=record）
    got = client.get(f"/memory/{body['node_id']}").json()
    assert got["payload"]["type"] == "record"


def test_post_turn_boring_text_writes_nothing(db_path):
    client = _client()
    r = client.post("/lifecycle/post-turn", json={"user_message": "今天天气不错", "assistant_message": "是的"})
    body = r.json()
    assert body["store"] is False
    assert body.get("node_id") is None


def test_post_turn_operational_tool_output_not_ingested(db_path):
    # 快捷键：模拟工具输出里的「启动/安排」高频词不得触发落库
    client = _client()
    r = client.post(
        "/lifecycle/post-turn", json={"user_message": "正在启动程序包安装...已成功完成\n接下来安排安装顺序"}
    )
    assert r.json()["store"] is False


# ---------------------------------------------------------------------------
# 3. session-end
# ---------------------------------------------------------------------------


def test_session_end_writes_facts_tier(db_path):
    client = _client()
    msgs = [
        {"role": "user", "content": "记住这个偏好：报告用极简风"},
        {"role": "user", "content": "不对，标题要左对齐"},
    ]
    r = client.post("/lifecycle/session-end", json={"messages": msgs})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store"] is True
    assert body["stored"] is True
    assert body["tier_distribution"] == {"facts": 1}
    got = client.get(f"/memory/{body['node_id']}").json()
    assert got["payload"]["type"] == "memory"  # facts 层


def test_session_end_near_duplicate_is_skipped(db_path):
    from mcp_tools import store

    content = "会话要点（Palimpsest 策略引擎提炼）：\n[user] 记住：项目代号凤凰"
    emb = store.embed_text(content)
    store.insert_node(
        {"type": "memory", "content": content, "importance": 0.55, "domain": "hermes"},
        emb,
    )
    # 同一段消息再走一次 → 触发近似重复跳过
    client = _client()
    msgs = [{"role": "user", "content": "记住：项目代号凤凰"}]
    r = client.post("/lifecycle/session-end", json={"messages": msgs})
    body = r.json()
    assert body["store"] is False
    assert body.get("skip_reason") == "near_duplicate"


def test_session_end_no_points_writes_nothing(db_path):
    client = _client()
    r = client.post("/lifecycle/session-end", json={"messages": [{"role": "user", "content": "今天天气不错"}]})
    assert r.json()["store"] is False


# ---------------------------------------------------------------------------
# 4. pre-compress
# ---------------------------------------------------------------------------


def test_pre_compress_returns_text_without_writing(db_path):
    client = _client()
    before = client.get("/mem/stats").json().get("total")
    msgs = [{"role": "user", "content": "记住这个偏好：深色模式"}]
    r = client.post("/lifecycle/pre-compress", json={"messages": msgs})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "记住" in body["points_text"]
    after = client.get("/mem/stats").json().get("total")
    assert before == after, "pre-compress must not write to the store"


# ---------------------------------------------------------------------------
# 5. 契约：一次完整多轮会话（10 条模拟 turn）不崩、分层正确
# ---------------------------------------------------------------------------


def test_full_session_cycle_layering(db_path):
    """喂一批混合 turn，断言只有该写的写了，且分层各自正确。"""
    client = _client()
    pre_turn = client.post("/lifecycle/pre-turn", json={"user_message": "回忆一下项目代号", "domain": "hermes"})
    assert pre_turn.status_code == 200

    turns = [
        "hi",  # trivial → 不写
        "帮我看看这个日志",  # 无强信号 → 不写
        "正在启动安装包...",  # 操作词 → 不写
        "记住：周报用极简风模板",  # 强信号 → logs
        "以后定稿只留 md",  # 强信号 → logs
        "不对，标题左对齐",  # 纠正 → logs (importance 0.7)
        "好的",  # trivial → 不写
        "继续",  # trivial → 不写
        "这个方案怎么推进",  # 操作词 → 不写
        "优先用开源方案",  # 操作词 → 不写
    ]
    stored = 0
    for i, t in enumerate(turns):
        r = client.post("/lifecycle/post-turn", json={"session_id": "s1", "turn_index": i, "user_message": t})
        body = r.json()
        if body.get("store") and body.get("stored"):
            stored += 1
            assert body["tier_distribution"] == {"logs": 1}

    assert stored == 3, f"expected 3 logs-tier writes, got {stored}"

    # 会话结束 → facts 层一条
    end = client.post(
        "/lifecycle/session-end",
        json={"session_id": "s1", "messages": [{"role": "user", "content": "记住：周报用极简风模板"}]},
    )
    ebody = end.json()
    assert ebody["store"] is True
    assert ebody["tier_distribution"] == {"facts": 1}
