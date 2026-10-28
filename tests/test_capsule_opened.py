"""契约测试：胶囊 opened 留痕（issue #208——「被接手」与检索命中是两回事）。

背景：胶囊 payload 里的 ``hit_count`` / ``last_hit_at`` 是 ``mem_search`` 每次
命中自动 +1 的检索计数，与「胶囊被新会话接手」不是一回事；接手留痕由
POST /memory/{id}/open 显式记录（opened_at / open_count / opened_by）。

验收闸门（本文件钉行为）：
  1. 首次 open：``opened_at`` 有值、``open_count == 1``、``opened_by == [sid]``
  2. 同 ``session_id`` 重复调用幂等：不重复计数、列表不重复
  3. 新 ``session_id``：``open_count == 2``、``opened_by`` 两条、按首次接手顺序
  4. ``opened_at`` 首写后不再变化（预置历史旧值也不被刷新）
  5. 节点不存在 → 404（与 PUT 端点同一 404 口径）
  6. 回归保护：open 不动 content / ``last_touched_at``（新鲜度不被接手污染）
  7. tasks_active 条目有则带 ``opened_at`` / ``open_count``；从未接手的不带键
  8. ``opened_by`` 上限 10 条、超限丢最老（最近接手的保留）

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库；
autouse 清理 fixture 保证共享 session 临时库不留本文件残留节点。
"""

from __future__ import annotations

import contextlib

import pytest

_OPEN_404_DETAIL = "更新失败：节点不存在或数据格式错误"  # 与 PUT 端点同一口径


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 临时库里跑集成测试——测完删掉本测试新建的节点。

    与 test_session_capsule 同款隔离模式：删除节点会级联删边 + 清 FTS，
    因此只需追踪 node_id 集合差集。
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


def _make_handoff(content: str = "接续点 · opened 留痕测试\n- 待办：等待被接手", **extra) -> int:
    """造一条胶囊形状的节点（type=task + kind=handoff + active），返回节点 id。"""
    from mcp_tools import store

    payload = {
        "type": "task",
        "status": "active",
        "kind": "handoff",
        "content": content,
        "importance": 0.5,
        **extra,
    }
    return store.insert_node(payload, store.embed_text(content))


def _payload_of(node_id: int) -> dict:
    from mcp_tools import store

    return (store.get_node(node_id) or {}).get("payload") or {}


def _open(node_id: int, session_id: str):
    """调 open 端点的便捷封装（返回响应 JSON）。

    TestClient 不进 with（不触发 lifespan）——app 的 MCP SessionManager
    只允许启动一次，进出 with 的第二次 startup 会炸；与 test_session_capsule
    同款用法。
    """
    client = _client()
    return client.post(f"/memory/{node_id}/open", json={"session_id": session_id}).json()


# ---------------------------------------------------------------------------
# 基础语义：首次 / 幂等 / 新会话
# ---------------------------------------------------------------------------


def test_first_open():
    nid = _make_handoff()
    body = _open(nid, "s1")
    assert body["status"] == "ok"
    assert body["opened_at"]  # 首次写入：ISO 时间字符串（真值即可，格式由 _now_iso 保证）
    assert body["open_count"] == 1
    assert body["opened_by"] == ["s1"]
    # 落库值与返回体一致（不是只改了响应）
    assert _payload_of(nid)["open_count"] == 1
    assert _payload_of(nid)["opened_by"] == ["s1"]


def test_open_idempotent_same_session():
    nid = _make_handoff()
    _open(nid, "s1")
    body = _open(nid, "s1")
    assert body["open_count"] == 1  # 幂等：同 session_id 重复调用不重复计数
    assert body["opened_by"] == ["s1"]  # 列表不重复
    # 第三次仍然不变
    assert _open(nid, "s1")["open_count"] == 1


def test_open_new_session():
    nid = _make_handoff()
    _open(nid, "s1")
    _open(nid, "s1")  # 幂等路径先走一遍
    body = _open(nid, "s2")
    assert body["open_count"] == 2
    assert body["opened_by"] == ["s1", "s2"]  # 按首次接手顺序


# ---------------------------------------------------------------------------
# opened_at 不可变性：首写后永不刷新
# ---------------------------------------------------------------------------


def test_opened_at_immutable():
    nid = _make_handoff()
    # 预置一个「很久以前」的历史值：若实现错误地用 now 覆盖，断言必然炸——
    # 比依赖两次调用跨秒（ISO 秒精度下可能同秒、断言平凡通过）强得多。
    stale_opened_at = "2000-01-01T00:00:00+08:00"
    from mcp_tools import store

    store.update_payload(nid, {"opened_at": stale_opened_at, "open_count": 1, "opened_by": ["s0"]})
    body = _open(nid, "s1")
    assert body["opened_at"] == stale_opened_at  # 新会话首调也不刷新 opened_at
    assert body["open_count"] == 2  # 但计数照常 +1
    assert body["opened_by"] == ["s0", "s1"]
    assert _open(nid, "s1")["opened_at"] == stale_opened_at  # 幂等路径同样不刷新


# ---------------------------------------------------------------------------
# 404 与新鲜度回归保护
# ---------------------------------------------------------------------------


def test_open_missing_node_404():
    resp = _client().post("/memory/999999999/open", json={"session_id": "s1"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == _OPEN_404_DETAIL


def test_open_does_not_touch_freshness_or_content():
    content = "接续点 · 新鲜度回归保护\n- 待办：last_touched_at 不该被接手碰"
    nid = _make_handoff(content=content)
    before = _payload_of(nid)
    assert "last_touched_at" not in before  # 前提：造出来的节点还没有新鲜度标记
    _open(nid, "s1")
    _open(nid, "s2")
    after = _payload_of(nid)
    # 接手留痕绝不污染新鲜度：last_touched_at 不许凭空出现 / 变化
    assert "last_touched_at" not in after
    assert after["content"] == before["content"]  # content 同样零改动


# ---------------------------------------------------------------------------
# tasks_active 暴露 opened 信息（有则带、没有不带）
# ---------------------------------------------------------------------------


def _tasks_active_items():
    resp = _client().get("/tasks/active", params={"include_handoff": "true"})
    assert resp.status_code == 200
    return {item["id"]: item for item in resp.json()["results"]}


def test_tasks_active_shows_opened_fields():
    opened_nid = _make_handoff(content="接续点 · 已被接手的胶囊\n- 待办：A")
    fresh_nid = _make_handoff(content="接续点 · 从未被接手的胶囊\n- 待办：B")
    assert _open(opened_nid, "s1")["status"] == "ok"

    items = _tasks_active_items()
    opened = items[opened_nid]
    assert opened["opened_at"]
    assert opened["open_count"] == 1
    fresh = items[fresh_nid]
    assert "opened_at" not in fresh  # 从未接手：键缺席，而不是塞 None / 0
    assert "open_count" not in fresh


# ---------------------------------------------------------------------------
# opened_by 上限：超限丢最老
# ---------------------------------------------------------------------------


def test_opened_by_cap_drops_oldest():
    from main import CAPSULE_OPENED_BY_LIMIT

    nid = _make_handoff()
    sessions = [f"s{i}" for i in range(CAPSULE_OPENED_BY_LIMIT + 2)]  # 12 个会话
    for sid in sessions:
        body = _open(nid, sid)
    assert body["open_count"] == len(sessions)  # 计数如实（12）
    assert len(body["opened_by"]) == CAPSULE_OPENED_BY_LIMIT  # 列表封顶
    assert body["opened_by"] == sessions[-CAPSULE_OPENED_BY_LIMIT:]  # 保留最近、丢最老
