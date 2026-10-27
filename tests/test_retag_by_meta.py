"""REST 契约：/mem/retag-by-meta 按「域名 + 自定义元数据」批量改写 meta 值。

动机（酒馆 / SillyTavern 记忆桥）：用户在宿主侧重命名聊天记录时，需要把该
聊天记忆的旧 ``chat_id`` 标统一改成新值，防止按旧标检索不到而变成孤儿。
本测试锁定契约与误改防护：

  1. dry_run=true（含缺省调用）→ 只预览，meta 读回仍是旧值
  2. dry_run=false → 真的改写，读回是新值
  3. 其他 meta 键不被冲掉（先写 {chat_id, season}，retag chat_id 后 season 还在）
  4. 同 meta_key/old_value 但不同 domain 的节点不被改（误改防护）
  5. domain 缺失 / 空串 → 400；meta_key / old_value / new_value 缺失 → 400；
     old_value == new_value → 400
  6. 匹配数超 max_update → 409 且一个都没改
  7. 真实改写后写 type=record 审计节点（本身不带 meta）

隔离：conftest 已把 DB_PATH 指向 session 级临时库 + fake embedder，不触碰
正式库；_no_leak_to_shared_db 测完清掉本测试新建的节点（模式沿
tests/test_meta_tag_purge.py）。
"""

import contextlib

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
# R1：校验护栏（domain / 三必填 / old==new / meta 结构）
# ---------------------------------------------------------------------------


def test_retag_rejects_missing_or_blank_domain():
    c = _client()
    body = {"meta_key": "chat_id", "old_value": "a", "new_value": "b", "dry_run": True}
    resp = c.post("/mem/retag-by-meta", json=body)
    assert resp.status_code == 400, resp.text
    resp = c.post("/mem/retag-by-meta", json={**body, "domain": "  "})
    assert resp.status_code == 400, resp.text


def test_retag_rejects_missing_fields_and_noop_retag():
    c = _client()
    body = {"domain": "rp"}
    resp = c.post("/mem/retag-by-meta", json=body)
    assert resp.status_code == 400, resp.text
    # old_value == new_value：无改写意义，400
    resp = c.post(
        "/mem/retag-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "old_value": "same", "new_value": "same"},
    )
    assert resp.status_code == 400, resp.text


def test_retag_rejects_invalid_new_meta_structure():
    """改写目标 {meta_key: new_value} 过 meta_guard：非法键名 → 400，不产生脏 meta。"""
    c = _client()
    resp = c.post(
        "/mem/retag-by-meta",
        json={"domain": "rp", "meta_key": "chat-id", "old_value": "a", "new_value": "b"},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# R2：干跑 / 真改 / 合并语义
# ---------------------------------------------------------------------------


def test_retag_dry_run_leaves_meta_intact():
    c = _client()
    nid1 = _ingest(c, "干跑守卫 预览一", meta={"chat_id": "dry-1"})
    nid2 = _ingest(c, "干跑守卫 预览二", meta={"chat_id": "dry-1"})
    # 缺省 dry_run：默认 true
    resp = c.post(
        "/mem/retag-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "old_value": "dry-1", "new_value": "dry-2"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_run"] is True
    assert data["matched_count"] == 2
    assert sorted(data["ids"]) == sorted([nid1, nid2])
    assert data["previews"] == ["干跑守卫 预览一", "干跑守卫 预览二"]
    assert data["updated_ids"] == [] and data["updated_count"] == 0
    assert _payload(nid1)["meta"]["chat_id"] == "dry-1"
    assert _payload(nid2)["meta"]["chat_id"] == "dry-1"


def test_retag_rewrites_matches_marking_new_value():
    c = _client()
    to_retag1 = _ingest(c, "真改 命中一", meta={"chat_id": "old-chat"})
    to_retag2 = _ingest(c, "真改 命中二", meta={"chat_id": "old-chat"})
    resp = c.post(
        "/mem/retag-by-meta",
        json={
            "domain": "rp",
            "meta_key": "chat_id",
            "old_value": "old-chat",
            "new_value": "new-chat",
            "dry_run": False,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_run"] is False
    assert data["updated_count"] == 2
    assert data["failed"] == []
    assert sorted(data["updated_ids"]) == sorted([to_retag1, to_retag2])
    assert _payload(to_retag1)["meta"]["chat_id"] == "new-chat"
    assert _payload(to_retag2)["meta"]["chat_id"] == "new-chat"


def test_retag_preserves_other_meta_keys():
    """合并语义：先写 {chat_id, season}，retag chat_id 后 season 还在。"""
    c = _client()
    nid = _ingest(c, "合并语义 其余键保全", meta={"chat_id": "merge-1", "season": "第三季"})
    resp = c.post(
        "/mem/retag-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "old_value": "merge-1", "new_value": "merge-2", "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1
    payload = _payload(nid)
    assert payload["meta"]["chat_id"] == "merge-2"
    assert payload["meta"]["season"] == "第三季"


def test_retag_does_not_touch_other_domains():
    c = _client()
    keep_rival = _ingest(c, "novel 域同 meta 同值 不改", domain="novel", meta={"chat_id": "hit-1"})
    retag1 = _ingest(c, "rp 域命中一", meta={"chat_id": "hit-1"})
    keep_off_meta = _ingest(c, "rp 域但旧值不同", meta={"chat_id": "hit-2"})

    resp = c.post(
        "/mem/retag-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "old_value": "hit-1", "new_value": "hit-9", "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_ids"] == [retag1]
    # 误改防护：不同 domain 同 meta 不改；同 domain 不同旧值不改
    assert _payload(keep_rival)["meta"]["chat_id"] == "hit-1"
    assert _payload(keep_off_meta)["meta"]["chat_id"] == "hit-2"


# ---------------------------------------------------------------------------
# R3：数量护栏 / 审计
# ---------------------------------------------------------------------------


def test_retag_refuses_when_over_max_update_and_rewrites_nothing():
    c = _client()
    nids = [_ingest(c, f"超限守卫 第{i}条", meta={"chat_id": "over"}) for i in range(3)]
    resp = c.post(
        "/mem/retag-by-meta",
        json={
            "domain": "rp",
            "meta_key": "chat_id",
            "old_value": "over",
            "new_value": "flood",
            "dry_run": False,
            "max_update": 2,
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["matched_count"] == 3
    assert len(detail["previews"]) == 3
    from mcp_tools import store

    # 409 时一个都没改
    assert all(store.get_node(nid)["payload"]["meta"]["chat_id"] == "over" for nid in nids)


def test_retag_records_audit_node_without_meta():
    c = _client()
    _ingest(c, "审计 改写一", meta={"chat_id": "aud-old"})
    _ingest(c, "审计 改写二", meta={"chat_id": "aud-old"})

    resp = c.post(
        "/mem/retag-by-meta",
        json={"domain": "rp", "meta_key": "chat_id", "old_value": "aud-old", "new_value": "aud-new", "dry_run": False},
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
    # 审计节点本身不带 meta——避免被后续同条件的 retag / purge 连坐
    assert "meta" not in payload
    content = payload["content"]
    assert "retag-by-meta" in content and "chat_id" in content
    assert "aud-old" in content and "aud-new" in content
