"""契约测试：手写胶囊结构字段（mem_ingest 可选 sections / pending，issue #190）。

验收闸门（校验集中在 core/capsule_guard.py，REST 与 MCP 转调同一实现）：

  1. 基线回归：不传 sections/pending → 落库 payload 形状与改动前完全一致
     （payload 不含 sections / pending 键，REST 与 MCP 均如此）
  2. 传 sections → 落库 payload.sections 原样保留
  3. pending 自动计算：只给 thought / outcome 有值 → pending == ["next", "pitfalls"]
     （sections 缺失或值为空白的节都算待补，顺序固定 thought/outcome/next/pitfalls）
  4. 显式传 pending → 原样使用，不做自动推断覆盖
  5. 非法 sections 键（白名单外）→ REST 422 / MCP stored:False，库无新节点
  6. 非字符串 value → REST 422 / MCP 拒绝，库无新节点
  7. pending 与 sections 矛盾（列了非空节）→ 422，库无新节点
  8. MCP 与 REST 同构：同一份校验对两边都生效（同一错误信息）

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库；
本文件 autouse 夹具在测后清理新建节点（与 test_session_capsule 同款）。
"""

from __future__ import annotations

import contextlib
import json

import pytest


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


def _payload_of(node_id: int) -> dict:
    from mcp_tools import store

    for nid, payload in store.iter_payloads():
        if nid == node_id:
            return payload
    raise AssertionError(f"节点 {node_id} 不存在")


def _node_count() -> int:
    from mcp_tools import store

    return sum(1 for _ in store.iter_payloads())


def _ingest_rest(client, **overrides):
    body = {"content": "手写胶囊测试内容", "type": "task", "domain": "test_capsule"}
    body.update(overrides)
    return client.post("/mem/ingest", json=body)


def _ingest_mcp(**overrides) -> dict:
    from mcp_tools import mem_ingest

    body = {"content": "手写胶囊测试内容", "type": "task", "domain": "test_capsule"}
    body.update(overrides)
    return json.loads(mem_ingest(**body))


# ---------------------------------------------------------------------------
# 1. 基线回归：不传 sections/pending，payload 形状与改动前完全一致
# ---------------------------------------------------------------------------


def test_baseline_without_structured_fields_unchanged():
    client = _client()
    r = _ingest_rest(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["stored"] is True
    payload = _payload_of(int(data["node_id"]))
    assert "sections" not in payload
    assert "pending" not in payload

    m = _ingest_mcp()
    assert m["stored"] is True
    payload = _payload_of(int(m["node_id"]))
    assert "sections" not in payload
    assert "pending" not in payload


# ---------------------------------------------------------------------------
# 2. 传 sections → 原样落库；四节全有值时自动 pending 为空列表（同自动胶囊）
# ---------------------------------------------------------------------------


def test_sections_roundtrip_rest_and_mcp():
    client = _client()
    sections = {
        "thought": "先摸清自动胶囊形状再动手",
        "outcome": "补齐手写胶囊的结构化字段",
        "next": "跑门禁",
        "pitfalls": "别在 endpoint 里写散逻辑",
    }
    r = _ingest_rest(client, sections=dict(sections))
    assert r.status_code == 200, r.text
    data = r.json()
    payload = _payload_of(int(data["node_id"]))
    assert payload["sections"] == sections
    assert payload["pending"] == []

    m = _ingest_mcp(sections=dict(sections))
    assert m["stored"] is True
    payload = _payload_of(int(m["node_id"]))
    assert payload["sections"] == sections
    assert payload["pending"] == []


# ---------------------------------------------------------------------------
# 3. pending 自动计算：缺失或值为空白的节都算待补，顺序固定
# ---------------------------------------------------------------------------


def test_pending_auto_computed():
    client = _client()
    r = _ingest_rest(client, sections={"thought": "只写思路", "outcome": "只写产出"})
    assert r.status_code == 200, r.text
    data = r.json()
    payload = _payload_of(int(data["node_id"]))
    assert payload["sections"] == {"thought": "只写思路", "outcome": "只写产出"}
    assert payload["pending"] == ["next", "pitfalls"]

    # 值为空白同样算待补（与自动胶囊「空节进 pending」同口径）
    m = _ingest_mcp(sections={"thought": "思路", "outcome": "  ", "pitfalls": "坑"})
    assert m["stored"] is True
    payload = _payload_of(int(m["node_id"]))
    assert payload["pending"] == ["outcome", "next"]


# ---------------------------------------------------------------------------
# 4. 显式传 pending → 原样使用（不覆盖、不与自动算合并）
# ---------------------------------------------------------------------------


def test_explicit_pending_used_as_is():
    client = _client()
    r = _ingest_rest(client, sections={"thought": "有值"}, pending=["outcome"])
    assert r.status_code == 200, r.text
    data = r.json()
    payload = _payload_of(int(data["node_id"]))
    assert payload["pending"] == ["outcome"]  # 自动算会给出 ["outcome", "next", "pitfalls"]
    assert payload["sections"] == {"thought": "有值"}


# ---------------------------------------------------------------------------
# 5. 非法 sections 键 → REST 422 / MCP stored:False，库无新节点
# ---------------------------------------------------------------------------


def test_invalid_section_key_rejected():
    client = _client()
    before = _node_count()
    r = _ingest_rest(client, sections={"summary": "白名单外的键"})
    assert r.status_code == 422, r.text
    assert _node_count() == before

    m = _ingest_mcp(sections={"summary": "白名单外的键"})
    assert m["stored"] is False
    assert _node_count() == before


# ---------------------------------------------------------------------------
# 6. 非字符串 value → REST 422 / MCP 拒绝，库无新节点
# ---------------------------------------------------------------------------


def test_non_string_value_rejected():
    client = _client()
    before = _node_count()
    r = _ingest_rest(client, sections={"thought": 123})
    assert r.status_code == 422, r.text
    assert _node_count() == before

    # MCP 直调（绕过 pydantic 类型层）：core 校验兜底拒绝
    m = _ingest_mcp(sections={"thought": 123})
    assert m["stored"] is False
    assert _node_count() == before


# ---------------------------------------------------------------------------
# 7. pending 与 sections 矛盾（列了非空节）→ 拒绝，库无新节点
# ---------------------------------------------------------------------------


def test_contradiction_pending_vs_sections_rejected():
    client = _client()
    before = _node_count()
    r = _ingest_rest(client, sections={"thought": "非空内容"}, pending=["thought"])
    assert r.status_code == 422, r.text
    assert _node_count() == before

    m = _ingest_mcp(sections={"thought": "非空内容"}, pending=["thought"])
    assert m["stored"] is False
    assert _node_count() == before


# ---------------------------------------------------------------------------
# 8. MCP 与 REST 同构：同一非法输入，同一错误信息，两边都拒
# ---------------------------------------------------------------------------


def test_mcp_rest_same_validation():
    client = _client()
    r = _ingest_rest(client, sections={"summary": "x"}, pending=["summary"])
    assert r.status_code == 422, r.text
    rest_detail = r.json()["detail"]

    m = _ingest_mcp(sections={"summary": "x"}, pending=["summary"])
    assert m["stored"] is False
    assert m["error"] == rest_detail


# ---------------------------------------------------------------------------
# 9. core.capsule_guard 纯函数直测（校验与 pending 解析的唯一裁决点）
# ---------------------------------------------------------------------------


def test_validate_capsule_fields_unit():
    from core.capsule_guard import validate_capsule_fields

    # 全不传 → 旧行为：不落任何键（pending 解析结果 None）
    ok, err, pending = validate_capsule_fields(None, None)
    assert ok and err == "" and pending is None

    # 自动算：缺失节按固定顺序补全
    ok, err, pending = validate_capsule_fields({"thought": "x"}, None)
    assert ok and pending == ["outcome", "next", "pitfalls"]

    # 空白值节也算待补
    ok, err, pending = validate_capsule_fields({"thought": "  "}, None)
    assert ok and pending == ["thought", "outcome", "next", "pitfalls"]

    # 显式 pending 原样
    ok, err, pending = validate_capsule_fields({"thought": "x"}, ["pitfalls"])
    assert ok and pending == ["pitfalls"]

    # 非法键
    ok, err, _ = validate_capsule_fields({"summary": "x"}, None)
    assert not ok and "summary" in err

    # pending 非法元素
    ok, err, _ = validate_capsule_fields(None, ["summary"])
    assert not ok

    # 矛盾：pending 列了 sections 里非空的节
    ok, err, _ = validate_capsule_fields({"next": "有值"}, ["next"])
    assert not ok

    # 「（待补）」占位与 pending 相容：2026-10-28 生产回归——自动胶囊只要有一节
    # 为空，compose 就会把「（待补）」写进 sections 并同时把该节列进 pending，
    # 旧判定把它当「非空却列待补」整条拒绝，结果胶囊一条都写不进去。
    ok, err, pending = validate_capsule_fields({"thought": "x", "pitfalls": "（待补）"}, ["pitfalls"])
    assert ok and err == "" and pending == ["pitfalls"]
    # 自动算 pending 时，占位节同样要算待补
    ok, err, pending = validate_capsule_fields({"thought": "x", "pitfalls": "（待补）"}, None)
    assert ok and pending == ["outcome", "next", "pitfalls"]
    # 占位常量与 strategy 侧同源（防两处漂移）
    from core.capsule_guard import CAPSULE_SECTION_TBD as _guard_tbd
    from core.strategy import CAPSULE_SECTION_TBD as _strategy_tbd

    assert _guard_tbd == _strategy_tbd

    # 类型错：sections 不是字典 / pending 不是列表
    ok, err, _ = validate_capsule_fields(["not a dict"], None)  # type: ignore[arg-type]
    assert not ok
    ok, err, _ = validate_capsule_fields(None, "next")  # type: ignore[arg-type]
    assert not ok
