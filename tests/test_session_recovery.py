"""契约测试：会话胶囊「断线自愈」（孤儿缓冲扫描 + 自动补产）。

验收闸门（任务书逐条钉行为；隔离同 test_session_capsule：conftest 临时库 +
fake embedder，绝不触碰生产库）：

  1. 孤儿补产      updated_at 早于阈值的 active 缓冲 → 补产 type=task+kind=handoff
     胶囊、原缓冲 closed、胶囊 payload 带 recovered=True / recovered_at
  2. 新鲜缓冲不动  updated_at 是现在 → recovered==[]，缓冲仍 active
  3. 老数据兜底    只有 started_at（无 updated_at）→ 用 started_at 判定，可补产
  4. 幂等          连跑两次扫描，第二次 recovered==[]（不重复建胶囊、node_id 不变）
  5. 失败隔离      第一个 session 补产抛异常 → 进 failed，第二个照常补产
  6. dry_run       不写库：缓冲仍 active、无胶囊，返回体可见待处理 session
  7. 端点契约      POST /lifecycle/recover-orphan-capsules 返回体键名固定
  8. 活动时间戳    _session_buffer_activity_ts 三形态：updated_at 优先 /
     started_at 兜底 / 都缺失 0.0

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib
import time

import pytest

THOUGHT_BLOCK = (
    "<thought>\n"
    "1. 本轮改什么 / 新增什么（一句话）：给骨架加胶囊字段\n"
    "2. 失败路径（哪里会坏、坏了什么表现、怎么兜住）：解析抛异常会拖垮落库\n"
    "3. 怎么验（跑什么、看到什么算数）：pytest 全绿\n"
    "4. 交付物（PR / 文件路径 / 节点号）：core/strategy.py\n"
    "</thought>"
)


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本测试新建的节点。

    与 test_session_capsule 同款隔离模式：conftest 用 session 级临时库，所有
    测试文件共享；残留节点会污染其他测试的孤儿判定（别人残留的 active 缓冲
    一旦超阈值会被本测试的扫描误捞）。删除节点会级联删边 + 清 FTS，只需追踪
    node_id 集合差集。
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


def _post_turn(client, session_id: str, turn_index: int = 1) -> None:
    """走真实 post-turn 路径给 session 累进一轮缓冲（实质轮样本，buffered=True）。"""
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": session_id,
            "turn_index": turn_index,
            "user_message": f"第 {turn_index} 轮：帮我看看胶囊相关的问题",
            "assistant_message": f"正文\n{THOUGHT_BLOCK}\n",
            "skeleton_kind": "code",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["session_buffer"]["buffered"] is True


def _buffer(session_id: str):
    """该 session 的缓冲节点 (nid, payload)，没有则 None。"""
    from mcp_tools import store

    nodes = [
        (nid, payload)
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict) and payload.get("type") == "session" and payload.get("session_id") == session_id
    ]
    assert len(nodes) <= 1, f"同一 session_id 只允许一条缓冲节点，实际 {len(nodes)} 条"
    return nodes[0] if nodes else None


def _capsule_ids(session_id: str) -> list[int]:
    """该 session 已产出的会话胶囊节点 id（type=task + kind=handoff）。"""
    from mcp_tools import store

    return [
        nid
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict)
        and payload.get("type") == "task"
        and payload.get("kind") == "handoff"
        and payload.get("session_id") == session_id
    ]


def _make_orphan(client, session_id: str, *, age_seconds: float = 3600.0) -> tuple[int, dict]:
    """造一条已静默 age_seconds 的活跃缓冲（走真实 post-turn，再把 updated_at 拨旧）。"""
    from mcp_tools import store

    _post_turn(client, session_id)
    buf = _buffer(session_id)
    assert buf is not None
    buf_nid, payload = buf
    store.update_payload(buf_nid, {"updated_at": time.time() - age_seconds})
    return buf_nid, payload


# ---------------------------------------------------------------------------
# 8. _session_buffer_activity_ts — 三形态判定（纯函数）
# ---------------------------------------------------------------------------


def test_activity_ts_updated_at_wins_started_at_fallback_missing_zero():
    from main import _session_buffer_activity_ts

    assert _session_buffer_activity_ts({"updated_at": 5.0, "started_at": 1.0}) == 5.0
    assert _session_buffer_activity_ts({"started_at": 1.0}) == 1.0
    assert _session_buffer_activity_ts({}) == 0.0  # 都缺失：视为最老（可补）


# ---------------------------------------------------------------------------
# 1. 孤儿补产：胶囊产出 + 缓冲关闭 + 留痕
# ---------------------------------------------------------------------------


def test_orphan_buffer_recovered_into_capsule(db_path):
    from main import _recover_orphan_session_buffers
    from mcp_tools import store

    client = _client()
    buf_nid, payload = _make_orphan(client, "rec-1")
    # 需求 1 的事实检验：真实 post-turn 路径写出的缓冲必须带 updated_at
    assert isinstance(payload.get("updated_at"), float)
    # 老库里的既有缓冲（改动前建的）没有 updated_at 也不受影响——本条已拨旧

    out = _recover_orphan_session_buffers(stale_after=1800.0)
    assert out["ok"] is True
    assert out["dry_run"] is False
    assert [r["session_id"] for r in out["recovered"]] == ["rec-1"]
    rec = out["recovered"][0]
    assert rec["turns"] == 1
    assert rec["node_id"] is not None

    # 胶囊产出：type=task + kind=handoff + 留痕字段
    cap = store.get_node(rec["node_id"]).get("payload", {})
    assert cap["type"] == "task"
    assert cap["kind"] == "handoff"
    assert cap["recovered"] is True
    assert cap["recovered_at"]  # ISO 时间字符串

    # 原缓冲 closed + CONSOLIDATED_INTO 边（补产走的是同一条 finalize 路径）
    after = store.get_node(buf_nid).get("payload", {})
    assert after["status"] == "closed"
    edges = store.get_edges(buf_nid)
    assert any(getattr(e, "target_id", None) == rec["node_id"] for e in edges)


# ---------------------------------------------------------------------------
# 2. 新鲜缓冲不动
# ---------------------------------------------------------------------------


def test_fresh_buffer_untouched(db_path):
    from main import _recover_orphan_session_buffers
    from mcp_tools import store

    client = _client()
    _post_turn(client, "rec-fresh")  # 刚写的缓冲：updated_at 就是现在
    buf = _buffer("rec-fresh")
    assert buf is not None
    buf_nid, _ = buf

    out = _recover_orphan_session_buffers(stale_after=1800.0)
    assert out["recovered"] == []
    assert out["skipped"] == []
    assert out["failed"] == []

    after = store.get_node(buf_nid).get("payload", {})
    assert after["status"] == "active"


# ---------------------------------------------------------------------------
# 3. 缺时间戳的历史缓冲：started_at 兜底
# ---------------------------------------------------------------------------


def test_legacy_buffer_without_updated_at_uses_started_at(db_path):
    from main import _recover_orphan_session_buffers
    from mcp_tools import store

    # 老数据形态：只有 started_at（两小时前），没有任何 updated_at
    content = "[会话缓冲] session=rec-legacy（老数据形态）"
    store.insert_node(
        {
            "type": "session",
            "content": content,
            "importance": 0.5,
            "session_id": "rec-legacy",
            "status": "active",
            "started_at": time.time() - 7200,
            "turns": [
                {
                    "turn_index": 1,
                    "skeleton_kind": "default",
                    "sections": {"thought": "老数据的思路"},
                    "raw": "1. 老数据",
                }
            ],
            "last_turn_index": 1,
        },
        store.embed_text(content),
    )

    out = _recover_orphan_session_buffers(stale_after=1800.0)
    assert [r["session_id"] for r in out["recovered"]] == ["rec-legacy"]
    rec = out["recovered"][0]
    assert rec["turns"] == 1
    cap = store.get_node(rec["node_id"]).get("payload", {})
    assert cap["type"] == "task" and cap["kind"] == "handoff"
    assert "老数据的思路" in cap["content"]


# ---------------------------------------------------------------------------
# 4. 幂等：第二次扫描不重复建胶囊
# ---------------------------------------------------------------------------


def test_recovery_is_idempotent(db_path):
    from main import _recover_orphan_session_buffers
    from mcp_tools import store

    client = _client()
    _post_turn(client, "rec-idem", turn_index=1)
    _post_turn(client, "rec-idem", turn_index=2)
    buf = _buffer("rec-idem")
    assert buf is not None
    buf_nid, _ = buf
    store.update_payload(buf_nid, {"updated_at": time.time() - 3600})

    first = _recover_orphan_session_buffers(stale_after=1800.0)
    assert len(first["recovered"]) == 1
    cap_nid = first["recovered"][0]["node_id"]
    assert first["recovered"][0]["turns"] == 2

    # 第二次：缓冲已 closed，孤儿判定不再命中——不重复建胶囊，node_id 不变
    second = _recover_orphan_session_buffers(stale_after=1800.0)
    assert second["recovered"] == []
    assert _capsule_ids("rec-idem") == [cap_nid]


# ---------------------------------------------------------------------------
# 5. 失败不拖垮整批
# ---------------------------------------------------------------------------


def test_single_failure_does_not_block_batch(db_path, monkeypatch):
    import main as main_mod

    client = _client()
    _make_orphan(client, "rec-fail-a")
    _make_orphan(client, "rec-fail-b")

    original = main_mod._finalize_session_capsule

    def _flaky(session_id: str) -> dict | None:
        if session_id == "rec-fail-a":
            raise RuntimeError("mock: 补产失败")
        return original(session_id)

    monkeypatch.setattr(main_mod, "_finalize_session_capsule", _flaky)
    out = main_mod._recover_orphan_session_buffers(stale_after=1800.0)

    assert len(out["failed"]) == 1
    assert out["failed"][0]["session_id"] == "rec-fail-a"
    assert "mock" in out["failed"][0]["error"]
    # 第二个 session 照常补产
    assert [r["session_id"] for r in out["recovered"]] == ["rec-fail-b"]


# ---------------------------------------------------------------------------
# 6. dry_run 不写库
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(db_path):
    from main import _recover_orphan_session_buffers
    from mcp_tools import store

    client = _client()
    buf_nid, _ = _make_orphan(client, "rec-dry")

    out = _recover_orphan_session_buffers(dry_run=True, stale_after=1800.0)
    assert out["dry_run"] is True
    # 待处理 session 在返回体里可见（node_id=None = 还没写）
    assert [r["session_id"] for r in out["recovered"]] == ["rec-dry"]
    assert out["recovered"][0]["node_id"] is None
    assert out["recovered"][0]["turns"] == 1

    # 库未动：缓冲仍 active、没有胶囊
    after = store.get_node(buf_nid).get("payload", {})
    assert after["status"] == "active"
    assert _capsule_ids("rec-dry") == []


# ---------------------------------------------------------------------------
# 7. 端点契约
# ---------------------------------------------------------------------------


def test_endpoint_contract(db_path):
    from mcp_tools import store

    client = _client()
    _make_orphan(client, "rec-endpoint")

    r = client.post("/lifecycle/recover-orphan-capsules", json={"dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    # 返回体键名固定（任务书规格）
    assert set(body) == {"ok", "dry_run", "stale_after", "scanned", "recovered", "skipped", "failed"}
    assert body["ok"] is True
    assert body["dry_run"] is False
    assert isinstance(body["stale_after"], float)
    assert [x["session_id"] for x in body["recovered"]] == ["rec-endpoint"]
    # 补产真的发生（不是嘴上说说）
    cap = store.get_node(body["recovered"][0]["node_id"]).get("payload", {})
    assert cap["recovered"] is True

    # dry_run 透传：只报告不写库
    r2 = client.post("/lifecycle/recover-orphan-capsules", json={"dry_run": True})
    assert r2.status_code == 200
    assert r2.json()["dry_run"] is True
    assert all(x["node_id"] is None for x in r2.json()["recovered"])
