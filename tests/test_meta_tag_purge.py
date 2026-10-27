"""REST 契约：/mem/ingest 的 meta 打标 + /mem/purge-by-meta 按元数据批量删除。

动机（酒馆 / SillyTavern 记忆桥）：用户在宿主侧删除聊天记录时，需要按
「域名 + 自定义元数据」精确清空对应记忆。本测试锁定两端契约与误删防护：

  1. ingest 带 meta → payload.meta 子字典存在
  2. ingest 不带 meta → payload 无 meta 键（旧行为不变）
  3. meta 校验拒绝：非法键名 / 值超 256 / 超过 8 个键
  4. purge 命中 → dry_run=false 后节点消失
  5. dry_run=true（含缺省调用）→ 节点仍在
  6. domain 缺失 / 空串 → 400
  7. 同 meta_key/meta_value 但不同 domain 的节点不被删（误删防护）
  8. 匹配数超 max_delete → 409 且一个都没删
  9. 删除后关联边被清理
 10. 真实删除后写 type=record 审计节点（本身不带 meta）
 11. MCP 工具与 REST 同构共享 meta 写入路径

隔离：conftest 已把 DB_PATH 指向 session 级临时库 + fake embedder，不触碰
正式库；_no_leak_to_shared_db 测完清掉本测试新建的节点（顺序敏感测试的
共享库卫生，模式沿 tests/test_lifecycle_endpoints.py）。
"""

import contextlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑契约测试——测完删掉本测试新建的节点。"""
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
    from main import app

    return TestClient(app)


def _ingest(client, content, domain="rp", meta="unset"):
    """POST /mem/ingest；meta 用哨兵 "unset" 区分「不传」与「显式传 None」。"""
    body = {"content": content, "type": "memory", "domain": domain}
    if meta != "unset":
        body["meta"] = meta
    resp = client.post("/mem/ingest", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stored"], data
    return data["node_id"]


def _payload(node_id):
    from mcp_tools import store

    node = store.get_node(node_id)
    return node["payload"] if node else None


# ---------------------------------------------------------------------------
# R1：/mem/ingest 可选 meta
# ---------------------------------------------------------------------------


def test_ingest_with_meta_writes_meta_subdict():
    c = _client()
    nid = _ingest(c, "meta 打标：酒馆会话 chat-42 的记忆", meta={"chat_id": "chat-42"})
    payload = _payload(nid)
    assert payload["meta"] == {"chat_id": "chat-42"}


def test_ingest_without_meta_keeps_old_payload_shape():
    c = _client()
    nid = _ingest(c, "不带 meta 的普通写入（旧行为不变）")
    payload = _payload(nid)
    assert "meta" not in payload


def test_ingest_rejects_invalid_meta_key_name():
    c = _client()
    for bad_key in ("chat-id", "会话", "a" * 33, ""):
        resp = c.post(
            "/mem/ingest",
            json={"content": "非法键名", "domain": "rp", "meta": {bad_key: "v"}},
        )
        assert resp.status_code == 422, f"{bad_key!r}: {resp.text}"


def test_ingest_rejects_overlong_meta_value():
    c = _client()
    resp = c.post(
        "/mem/ingest",
        json={"content": "值超长", "domain": "rp", "meta": {"chat_id": "x" * 257}},
    )
    assert resp.status_code == 422, resp.text
    # 边界值 256 应当通过
    nid = _ingest(c, "边界值 256 视为合法", meta={"chat_id": "x" * 256})
    assert _payload(nid)["meta"]["chat_id"] == "x" * 256


def test_ingest_rejects_more_than_8_meta_keys():
    c = _client()
    meta = {f"key_{i}": "v" for i in range(9)}
    resp = c.post("/mem/ingest", json={"content": "键过多", "domain": "rp", "meta": meta})
    assert resp.status_code == 422, resp.text


def test_mcp_tool_shares_meta_write_path():
    """MCP mem_ingest 与 REST 同构：别处只维护一条写入实现。"""
    from mcp_tools import store
    from mcp_tools.memory import mem_ingest as tool_fn

    result = json.loads(tool_fn(content="MCP 通道 meta 打标", type="memory", domain="rp", meta={"chat_id": "mcp-1"}))
    assert result["stored"], result
    nid = result["node_id"]
    node = store.get_node(nid)
    assert node is not None and node["payload"]["meta"] == {"chat_id": "mcp-1"}


# ---------------------------------------------------------------------------
# R2：/mem/purge-by-meta
# ---------------------------------------------------------------------------


def test_purge_rejects_missing_or_blank_domain():
    c = _client()
    body = {"meta_key": "chat_id", "meta_value": "v", "dry_run": True}
    resp = c.post("/mem/purge-by-meta", json=body)
    assert resp.status_code == 400, resp.text
    resp = c.post("/mem/purge-by-meta", json={**body, "domain": "  "})
    assert resp.status_code == 400, resp.text


def test_purge_dry_run_leaves_nodes_intact():
    c = _client()
    nid1 = _ingest(c, "干跑守卫 预览一", meta={"chat_id": "dry-1"})
    nid2 = _ingest(c, "干跑守卫 预览二", meta={"chat_id": "dry-1"})
    resp = c.post(
        "/mem/purge-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "meta_value": "dry-1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_run"] is True
    assert data["matched_count"] == 2
    assert sorted(data["ids"]) == sorted([nid1, nid2])
    assert data["previews"] == ["干跑守卫 预览一", "干跑守卫 预览二"]
    # dry_run 默认 true：缺省调用不删任何东西
    assert _payload(nid1) is not None and _payload(nid2) is not None


def test_purge_deletes_matches_and_only_within_domain():
    c = _client()
    keep_rival = _ingest(c, "novel 域同 meta 同值 Without 误删", domain="novel", meta={"chat_id": "hit-1"})
    to_del1 = _ingest(c, "rp 域命中一", meta={"chat_id": "hit-1"})
    to_del2 = _ingest(c, "rp 域命中二", meta={"chat_id": "hit-1"})
    keep_off_meta = _ingest(c, "rp 域但 meta 值不同", meta={"chat_id": "hit-2"})

    resp = c.post(
        "/mem/purge-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "meta_value": "hit-1", "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["deleted_count"] == 2
    assert data["failed"] == []
    assert sorted(data["deleted_ids"]) == sorted([to_del1, to_del2])
    # 节点已删（get_node 返回 None）
    from mcp_tools import store

    assert store.get_node(to_del1) is None
    assert store.get_node(to_del2) is None
    # 误删防护：不同 domain 同 meta 不删；同 domain 不同 meta 值不删
    assert store.get_node(keep_rival) is not None
    assert store.get_node(keep_off_meta) is not None


def test_purge_refuses_when_over_max_delete_and_deletes_nothing():
    c = _client()
    nids = [_ingest(c, f"超限守卫 第{i}条", meta={"chat_id": "over"}) for i in range(3)]
    resp = c.post(
        "/mem/purge-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "meta_value": "over", "dry_run": False, "max_delete": 2},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["matched_count"] == 3
    assert len(detail["previews"]) == 3
    from mcp_tools import store

    assert all(store.get_node(nid) is not None for nid in nids)


def test_purge_cleans_associated_edges():
    c = _client()
    anchor = _ingest(c, "边清理 锚点(不匹配)", meta={"chat_id": "edge-keep"})
    victim = _ingest(c, "边清理 被删节点", meta={"chat_id": "edge-gone"})
    resp = c.post("/mem/link", json={"source_id": anchor, "target_id": victim, "relation": "RELATED_TO"})
    assert resp.status_code == 200, resp.text
    from mcp_tools import store

    assert len(store.get_edges(anchor)) >= 1

    resp = c.post(
        "/mem/purge-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "meta_value": "edge-gone", "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_count"] == 1
    assert store.get_edges(anchor) == []


def test_purge_records_audit_node_without_meta():
    c = _client()
    _ingest(c, "审计 审计一", meta={"chat_id": "aud-1"})
    _ingest(c, "审计 审计二", meta={"chat_id": "aud-1"})

    resp = c.post(
        "/mem/purge-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "meta_value": "aud-1", "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["audit_node_id"] is not None

    from mcp_tools import store

    node = store.get_node(data["audit_node_id"])
    assert node is not None
    payload = node["payload"]
    assert payload["type"] == "record"
    assert payload["domain"] == "rp"
    # 审计节点本身不带 meta——避免被后续 purge 连坐删除
    assert "meta" not in payload
    assert "chat_id" in payload["content"] and "aud-1" in payload["content"]
