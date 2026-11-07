"""scripts/backfill_chunks.py 的契约测试（回填主脚本本轮 only「只写不跑」——
生产执行须停服窗口另行批准；这里在临时库上验证 plan / apply 的判定逻辑与幂等）。

钉四件事：
  1. 长文档 + 无 chunk 行 → 计入 missing；--apply 后行数 = split_chunks 预期
  2. 短内容（不该有行）+ 残留行 → 计入 short_orphan；apply 后行清空
  3. 行存在但 model 不匹配 → 计入 stale_model；apply 后 model 全部对齐
  4. 幂等：同内容重复 apply，行数与 chunk_text 序列逐位不变
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3

import pytest

import mcp_tools.memory as memory_tool
from core import chunk_index as ci
from scripts import backfill_chunks as bf

_LONG_TEXT = "\n\n".join((f"第{n}段回填立证。" + "回填叙述细节填充很长的一段内容。" * 85) for n in (1, 2, 3))


@pytest.fixture(autouse=True)
def _chunk_db(tmp_path, monkeypatch):
    db = os.path.join(str(tmp_path), "node_chunks_backfill.db")
    monkeypatch.setenv("CHUNK_DB_PATH", db)
    yield db


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    def _ids() -> set:
        try:
            from mcp_tools import store

            return {nid for nid, _ in store.iter_payloads()}
        except Exception:  # noqa: BLE001 —— 清理尽力而为
            return set()

    before = _ids()
    yield
    for nid in _ids() - before:
        with contextlib.suppress(Exception):
            from mcp_tools import store

            store.delete_node(nid)


def _ingest_long():
    r = json.loads(memory_tool.mem_ingest(content=_LONG_TEXT, type="memory", domain="backfill_test"))
    return r["node_id"]


def _ingest_short():
    r = json.loads(
        memory_tool.mem_ingest(content="一条单段短记忆，不该出现在 chunk 表里。", type="memory", domain="backfill_test")
    )
    return r["node_id"]


def _rows(nid: int) -> list[tuple]:
    conn = sqlite3.connect(os.environ["CHUNK_DB_PATH"])
    try:
        return conn.execute(
            "SELECT chunk_idx, chunk_text, model FROM node_chunks WHERE node_id = ? ORDER BY chunk_idx", (nid,)
        ).fetchall()
    finally:
        conn.close()


def test_plan_missing_and_apply():
    from mcp_tools import store

    nid = _ingest_long()
    ci.remove_node_chunks(nid)  # 模拟历史缺口
    plan = bf.plan_backfill(store)
    mine = [it for it in plan["items"] if it["node_id"] == nid]
    assert mine and mine[0]["reason"] == "missing"
    expected_rows = mine[0]["expected_rows"]
    assert expected_rows >= 2

    res = bf.apply_backfill(store, plan["items"])
    assert res["ok"] >= 1 and not res["failed"], res
    rows = _rows(nid)
    assert len(rows) == expected_rows
    assert rows == [(i, t, ci.embedding_model_id(store)) for i, t in enumerate(ci.split_chunks(_LONG_TEXT))]


def test_plan_short_orphan_cleanup():
    from mcp_tools import store

    nid = _ingest_short()
    # 短内容本不该有行；人为塞两行残渣模拟脏数据
    ci.sync_node_chunks(nid, [[1.0, 0.0], [0.0, 1.0]], ["旧的残留一", "旧的残留二"], ci.embedding_model_id(store))
    plan = bf.plan_backfill(store)
    mine = [it for it in plan["items"] if it["node_id"] == nid]
    assert mine and mine[0]["reason"] == "short_orphan" and mine[0]["expected_rows"] == 0
    res = bf.apply_backfill(store, [it for it in plan["items"] if it["node_id"] == nid])
    assert res["removed"] == 1 and not res["failed"]
    assert _rows(nid) == []


def test_plan_stale_model():
    from mcp_tools import store

    nid = _ingest_long()
    plan = bf.plan_backfill(store)
    assert not [it for it in plan["items"] if it["node_id"] == nid], "刚写入的节点不该被判待回填"

    # 全部行改成不匹配的模型标识 → stale_model
    conn = sqlite3.connect(os.environ["CHUNK_DB_PATH"])
    try:
        conn.execute("UPDATE node_chunks SET model = 'modelX'")
        conn.commit()
    finally:
        conn.close()
    plan = bf.plan_backfill(store)
    mine = [it for it in plan["items"] if it["node_id"] == nid]
    assert mine and mine[0]["reason"] == "stale_model"
    res = bf.apply_backfill(store, mine)
    assert not res["failed"]
    models = {r[2] for r in _rows(nid)}
    assert models == {ci.embedding_model_id(store)}
    assert not [it for it in bf.plan_backfill(store)["items"] if it["node_id"] == nid]


def test_apply_idempotent():
    from mcp_tools import store

    nid = _ingest_long()
    plan = bf.plan_backfill(store)
    items = [it for it in plan["items"] if it["node_id"] == nid]
    if not items:  # 刚 ingest 时行已同步 → 制造缺口再验两条路径
        ci.remove_node_chunks(nid)
        items = [it for it in bf.plan_backfill(store)["items"] if it["node_id"] == nid]
    assert items
    bf.apply_backfill(store, items)
    first = _rows(nid)
    bf.apply_backfill(store, items)  # 重跑
    second = _rows(nid)
    assert first == second
    # 复核：该节点已不再被判待回填
    assert not [it for it in bf.plan_backfill(store)["items"] if it["node_id"] == nid]
