"""post-turn 同轮去重与域归属测试（issue #123 / #125）。

覆盖：
  - #125：同一 (session_id, turn_index) 第二次提交不落第二条 type=thought
    （stored=false + skip_reason="duplicate"）；换 turn_index 正常落库；
    新会话不受影响。判重在库内（跨重启有效）。
  - #123：post-turn 可选 domain 落进 thought payload；不传仍为隐式 general；
    mem_recent(domain="general") 能取回隐式 general 节点；
    mem_recent(domain="hermes") 不被查宽。

隔离保证：REST 测试只经端点写库并按节点差集清理，不触碰正式库。
"""

import contextlib

import pytest


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """经端点写库的测试测完清理（照 test_lifecycle_endpoints 的差集模式）。"""
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


def _post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _turn_body(session, turn, user="闲聊：帮我看下契约测试相关资料", thought=None):
    thought = thought or (
        "<thought>契约测试的决断：判重依据必须落在库内节点上，"
        "重启之后依然有效；query 上限的阈值要从 policy 取，"
        "不在端点写魔数。</thought>好的，我已确认这个设计。"
    )
    return {
        "session_id": session,
        "turn_index": turn,
        "user_message": user,
        "assistant_message": thought,
    }


# ---------------------------------------------------------------------------
# issue #125 —— 同轮去重
# ---------------------------------------------------------------------------


def test_post_turn_duplicate_turn_not_stored_twice():
    client = _client()
    body = _turn_body("ddp-x", 41)
    r1 = _post(client, "/lifecycle/post-turn", body)
    assert r1["thought"]["stored"] is True, r1
    assert r1["thought"]["node_id"] is not None

    r2 = _post(client, "/lifecycle/post-turn", body)
    assert r2["thought"]["stored"] is False, r2
    assert r2["thought"]["skip_reason"] == "duplicate"
    assert r2["thought"]["node_id"] is None
    # 两条落库结果互不影响两边 narrative；thought 计数只在第一次 +1
    assert r2["tier_distribution"].get("logs", 0) == 0


def test_post_turn_different_turn_still_stored():
    client = _client()
    body = _turn_body("ddp-y", 42)
    r1 = _post(client, "/lifecycle/post-turn", body)
    assert r1["thought"]["stored"] is True

    body2 = {**body, "turn_index": 43}
    r2 = _post(client, "/lifecycle/post-turn", body2)
    assert r2["thought"]["stored"] is True, r2


def test_post_turn_turn_index_landed_in_payload():
    client = _client()
    body = _turn_body("ddp-t", 55)
    r = _post(client, "/lifecycle/post-turn", body)
    nid = r["thought"]["node_id"]
    from mcp_tools import store

    payload = store.get_node(nid).get("payload", {})
    assert payload.get("turn_index") == 55
    assert payload.get("source") == "lifecycle:post-turn:ddp-t"


def test_post_turn_turn0_no_dedup_keeps_old_hosts_unchanged():
    """turn_index=0（宿主未提供真实序号）：不判重，每次都落库（老行为）。"""
    client = _client()
    body = _turn_body("ddp-zero", 0)
    body["turn_index"] = 0
    r1 = _post(client, "/lifecycle/post-turn", body)
    assert r1["thought"]["stored"] is True, r1
    body_again = {**body, "assistant_message": body["assistant_message"].replace("好的", "好")}
    r2 = _post(client, "/lifecycle/post-turn", body_again)
    assert r2["thought"]["stored"] is True, r2


# ---------------------------------------------------------------------------
# issue #123 —— post-turn domain 与 mem_recent 隐式 general
# ---------------------------------------------------------------------------


def _recent(client, domain):
    r = client.post("/mem/recent", json={"domain": domain, "limit": 50})
    assert r.status_code == 200, r.text
    return r.json()


def test_post_turn_domain_lands_in_payload():
    client = _client()
    body = {**_turn_body("dom-x", 41), "domain": "probe-x"}
    r = _post(client, "/lifecycle/post-turn", body)
    nid = r["thought"]["node_id"]
    assert nid is not None
    from mcp_tools import store

    payload = store.get_node(nid).get("payload", {})
    assert payload.get("domain") == "probe-x"


def test_post_turn_without_domain_stays_implicit_general():
    client = _client()
    body = _turn_body("dom-none", 41)
    r = _post(client, "/lifecycle/post-turn", body)
    nid = r["thought"]["node_id"]
    assert nid is not None
    from mcp_tools import store

    payload = store.get_node(nid).get("payload", {})
    assert payload.get("domain", "") in ("", "general")


def test_mem_recent_general_finds_implicit_general():
    """隐式 general 节点（domain 缺省为空）在 domain="general" 下可达。"""
    client = _client()
    marker = _turn_body("dom-g", 77, user="闲聊：契约隐式 general 探针话题")
    r = _post(client, "/lifecycle/post-turn", marker)
    nid = r["thought"]["node_id"]
    assert nid is not None

    data = _recent(client, "general")
    assert data["total"] > 0, data
    assert any(item["id"] == nid for item in data["results"]), data["results"][:3]


def test_mem_recent_hermes_domain_not_widened():
    """domain="hermes" 只含显式 hermes 节点——隐式 general 节点不混入。"""
    client = _client()
    r = _post(client, "/lifecycle/post-turn", _turn_body("dom-h", 88))
    implicit_nid = r["thought"]["node_id"]
    assert implicit_nid is not None

    data = _recent(client, "hermes")
    assert all(item["id"] != implicit_nid for item in data["results"]), data["results"][:3]


def test_mem_recent_raw_json_contract():
    """mem_recent 返回 JSON 文本，结构契约不回退。"""
    client = _client()
    data = _recent(client, "general")
    assert set(data.keys()) >= {"results", "total"}
    if data["results"]:
        for item in data["results"][:2]:
            assert keys_ok(item)


def keys_ok(item) -> bool:
    return {"id", "type", "content", "domain"} <= set(item.keys())
