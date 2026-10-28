"""契约测试：会话胶囊的厚度门槛与轮次号续接（issue #219）。

验收闸门（策略判定在 core/strategy.py 纯函数，端点执行在 main.py，本文件钉行为）：

  1. 薄会话（1 个实质轮 / 或实质轮 ≥2 但实质节 <2）→ 不产胶囊节点、返回体
     ``capsule_skipped`` 带跳过原因与厚度数字（不静默）、**缓冲仍关闭**（否则
     断线自愈扫描器每 5 分钟反复重试）；
  2. 厚会话（≥2 实质轮且 ≥2 实质节）→ 照旧产胶囊、缓冲 closed、建边；
  3. 断线自愈遇到薄缓冲 → 第一轮扫描把它关闭（skip 可观测），第二轮扫描
     **无事可做**（不再重试，不能只写注释）；
  4. post-turn 返回体回传**解析后的真实序号**（``turn_index_resolved``），宿主
     不发序号（恒 0）也能学到该接几号。

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib

import pytest

from core.strategy import SESSION_BUFFER_STATUS_ACTIVE, SESSION_BUFFER_STATUS_CLOSED, SESSION_BUFFER_TYPE


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本测试新建的节点。

    与 test_session_capsule 同款隔离模式：conftest 用 session 级临时库，
    所有测试文件共享；残留节点会污染顺序敏感测试。
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


_CODE_BLOCK = (
    "<thought>\n"
    "1. 本轮改什么 / 新增什么（一句话）：给胶囊加厚度门槛\n"
    "2. 失败路径（哪里会坏、坏了什么表现、怎么兜住）：跳过也要关缓冲，否则自愈重试\n"
    "3. 怎么验（跑什么、看到什么算数）：pytest 全绿\n"
    "4. 交付物（PR / 文件路径 / 节点号）：main.py\n"
    "</thought>"
)

# 原生思考：有 <thought> 但没有编号行——只有【思路】节能有内容（issue #216 行为）。
_FREE_TEXT_THOUGHT = "<thought>\n先把厚度门槛的判定想清楚：只数非占位的轮与节，两个阈值分别可断言。\n</thought>"


def _buffer_by_session(session_id: str) -> tuple[int, dict] | None:
    from mcp_tools import store

    nodes = [
        (nid, payload)
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict)
        and payload.get("type") == SESSION_BUFFER_TYPE
        and payload.get("session_id") == session_id
    ]
    return nodes[0] if nodes else None


def _capsule_by_session(session_id: str) -> tuple[int, dict] | None:
    from mcp_tools import store

    for nid, payload in store.iter_payloads():
        if (
            isinstance(payload, dict)
            and payload.get("type") == "task"
            and payload.get("kind") == "handoff"
            and payload.get("session_id") == session_id
        ):
            return nid, payload
    return None


def _post_turn(client, session_id: str, turn_index: int | None, block: str, kind: str = "code"):
    payload = {
        "session_id": session_id,
        "user_message": "帮我改胶囊门槛相关的问题",
        "assistant_message": f"正文\n{block}\n正文结束",
        "skeleton_kind": kind,
    }
    if turn_index is not None:
        payload["turn_index"] = turn_index
    return client.post("/lifecycle/post-turn", json=payload)


def _session_end(client, session_id: str):
    return client.post("/lifecycle/session-end", json={"session_id": session_id, "messages": []})


# ---------------------------------------------------------------------------
# 1. 薄会话 → 跳过可观测 + 缓冲关闭
# ---------------------------------------------------------------------------


def test_thin_one_substantive_turn_skips_and_closes_buffer(db_path):
    """1 个实质轮（节再多也不够）→ 无胶囊 + capsule_skipped 带原因 + 缓冲 closed。

    阈值单测一：substantive_turns=1 < 2，即使 filled_sections=4 也跳过——
    两个阈值独立生效。
    """
    client = _client()
    r = _post_turn(client, "gate-thin-1", 1, _CODE_BLOCK)
    assert r.json()["session_buffer"]["buffered"] is True
    buf = _buffer_by_session("gate-thin-1")
    assert buf is not None

    end = _session_end(client, "gate-thin-1")
    assert end.status_code == 200, end.text
    body = end.json()
    assert body.get("capsule") is None, "薄会话不得产胶囊"
    skipped = body.get("capsule_skipped")
    assert skipped is not None, f"跳过要可观测，实际返回：{body}"
    assert skipped["skipped"] == "too_thin"
    assert skipped["substantive_turns"] == 1
    assert skipped["filled_sections"] == 4, "单轮 code 骨架四节其实填满了（节数不是理由，轮数才是）"

    buf_after = _buffer_by_session("gate-thin-1")
    assert buf_after is not None
    assert buf_after[1]["status"] == SESSION_BUFFER_STATUS_CLOSED, "门槛不过也必须关缓冲（否则自愈无限重试）"
    assert _capsule_by_session("gate-thin-1") is None


def test_thin_few_filled_sections_skips_and_closes_buffer(db_path):
    """2 个实质轮但每轮只有【思路】有内容 → filled_sections=1 < 2 → 跳过。

    阈值单测二：filled_sections 单独可断言（substantive_turns 已达 2）。
    """
    client = _client()
    for i in (1, 2):
        r = _post_turn(client, "gate-thin-2", i, _FREE_TEXT_THOUGHT, kind="default")
        assert r.json()["session_buffer"]["buffered"] is True, r.text

    end = _session_end(client, "gate-thin-2")
    body = end.json()
    assert body.get("capsule") is None
    skipped = body.get("capsule_skipped")
    assert skipped is not None, f"跳过要可观测，实际返回：{body}"
    assert skipped["skipped"] == "too_thin"
    assert skipped["substantive_turns"] == 2
    assert skipped["filled_sections"] == 1

    buf_after = _buffer_by_session("gate-thin-2")
    assert buf_after is not None
    assert buf_after[1]["status"] == SESSION_BUFFER_STATUS_CLOSED


# ---------------------------------------------------------------------------
# 2. 厚会话 → 照旧产胶囊
# ---------------------------------------------------------------------------


def test_thick_session_still_produces_capsule(db_path):
    """2 个实质轮且 ≥2 节有内容 → 照旧产胶囊（不回归）。"""
    client = _client()
    for i in (1, 2):
        r = _post_turn(client, "gate-thick", i, _CODE_BLOCK)
        assert r.json()["session_buffer"]["buffered"] is True

    end = _session_end(client, "gate-thick")
    body = end.json()
    assert body.get("capsule_skipped") is None, f"厚会话不该有 skip 字段：{body}"
    capsule = body.get("capsule")
    assert capsule is not None, f"厚会话必须照旧产胶囊，实际返回：{body}"
    assert capsule["node_id"] is not None
    assert capsule["turns"] == 2

    buf_after = _buffer_by_session("gate-thick")
    assert buf_after is not None
    assert buf_after[1]["status"] == SESSION_BUFFER_STATUS_CLOSED
    from mcp_tools import store

    edges = store.get_edges(buf_after[0])
    assert any(getattr(e, "target_id", None) == capsule["node_id"] for e in edges)


# ---------------------------------------------------------------------------
# 3. 断线自愈：薄缓冲第一轮关闭、第二轮无事可做
# ---------------------------------------------------------------------------


def test_recovery_skips_thin_buffer_and_never_retries(db_path):
    """自愈遇到薄缓冲 → 本轮 skip（带原因）+ 缓冲关闭；下一轮扫描无事可做。"""
    from main import _recover_orphan_session_buffers
    from mcp_tools import store

    client = _client()
    r = _post_turn(client, "gate-thin-recover", None, _CODE_BLOCK)
    assert r.json()["session_buffer"]["buffered"] is True
    buf = _buffer_by_session("gate-thin-recover")
    assert buf is not None
    buf_nid, _buf_payload = buf

    # 把缓冲改「静默超过阈值」，模拟进程硬砍后留下的悬空缓冲
    old = 0.0  # 时间戳回拨到 0 → 任何阈值下都是最老
    store.update_payload(buf_nid, {"started_at": old, "updated_at": old})
    assert store.get_node(buf_nid)["payload"]["status"] == SESSION_BUFFER_STATUS_ACTIVE

    first = _recover_orphan_session_buffers()
    assert first["ok"] is True
    assert first["recovered"] == [], "薄缓冲不得补产胶囊"
    thin = [e for e in first["skipped"] if isinstance(e, dict) and e.get("session_id") == "gate-thin-recover"]
    assert len(thin) == 1, f"第一轮扫描要把薄缓冲记进 skipped 幂等条目：{first['skipped']}"
    assert thin[0]["skipped"] == "too_thin"
    assert thin[0]["substantive_turns"] == 1

    after = store.get_node(buf_nid)["payload"]
    assert after["status"] == SESSION_BUFFER_STATUS_CLOSED, "自愈路径同样必须关闭缓冲"
    assert _capsule_by_session("gate-thin-recover") is None

    # 第二轮扫描：缓冲已 closed → 不再命中 → 无事可做（这是防「每 5 分钟刷屏」的核心断言）
    result2 = _recover_orphan_session_buffers()
    assert result2["recovered"] == []
    assert result2["skipped"] == [], f"第二轮扫描不得再碰这个薄缓冲：{result2['skipped']}"
    assert result2["failed"] == []


def test_recovery_still_recovers_thick_buffer(db_path):
    """自愈回归保护：厚缓冲照旧补产（issue #219 只拦薄会话）。"""
    from main import _recover_orphan_session_buffers
    from mcp_tools import store

    client = _client()
    for i in (1, 2):
        r = _post_turn(client, "gate-thick-recover", i, _CODE_BLOCK)
        assert r.json()["session_buffer"]["buffered"] is True
    buf = _buffer_by_session("gate-thick-recover")
    assert buf is not None
    store.update_payload(buf[0], {"started_at": 0.0, "updated_at": 0.0})

    out = _recover_orphan_session_buffers()
    recovered = [e for e in out["recovered"] if e.get("session_id") == "gate-thick-recover"]
    assert len(recovered) == 1, f"厚缓冲必须照常自愈补产：{out}"
    assert recovered[0]["node_id"] is not None
    capsule = _capsule_by_session("gate-thick-recover")
    assert capsule is not None
    assert capsule[1].get("recovered") is True


# ---------------------------------------------------------------------------
# 4. post-turn 返回解析后的真实序号
# ---------------------------------------------------------------------------


def test_turn_index_resolved_echoed_in_response(db_path):
    """宿主不发序号 → 返回体 turn_index_resolved 是落库用的真实序号（1, 2, ...）；
    显式发 5 → resolved=5。``turn_index`` 字段保持请求值语义不变。"""
    client = _client()
    r1 = _post_turn(client, "resume-idx-1", None, _CODE_BLOCK)
    body1 = r1.json()
    assert body1["turn_index_resolved"] == 1
    assert body1["turn_index"] == 0  # 请求值语义不变
    assert body1["session_buffer"]["turn_index_resolved"] == 1

    r2 = _post_turn(client, "resume-idx-1", None, _CODE_BLOCK)
    assert r2.json()["turn_index_resolved"] == 2

    r3 = _post_turn(client, "resume-idx-1", 5, _CODE_BLOCK)
    assert r3.json()["turn_index_resolved"] == 5

    buf = _buffer_by_session("resume-idx-1")
    assert buf is not None
    assert buf[1]["last_turn_index"] == 5
