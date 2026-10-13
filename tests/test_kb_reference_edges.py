"""
「记忆 → 知识切片」REFERENCES 边测试
================================================
回归背景：`mem_ingest` 的知识关联检测（v1.1）会把相似的知识/小说/技能切片 id 写进
`payload.linked_from`，但**只写 payload、从不建图边** —— 实测生产库「记忆 → 知识切片」
的边为 0，知识切片在图谱上不会被记忆引用（图检索 / 邻居扩散到不了）。

本模块覆盖两条路径：
  1. 写入路径：`mem_ingest` 写入时即时建 REFERENCES 边；
  2. 不变量：REFERENCES 出边集合 == payload.linked_from 集合。

隔离级别：**自建独立库**（tempfile + 临时 DB_PATH + 替换 memory 模块的 store），
避免写进 conftest 的共享 session 库污染数节点/数簇的用例。
"""

import contextlib
import json
import shutil

import pytest

import mcp_tools.memory as memory_tool
from config import Config
from core.trivium_store import TriviumStore


@pytest.fixture
def iso_store(tmp_path, monkeypatch):
    db_path = tmp_path / "kb_reference_edges_test.db"
    monkeypatch.setattr(Config, "DB_PATH", str(db_path))
    store = TriviumStore()
    original = memory_tool.store
    memory_tool.store = store
    try:
        yield store
    finally:
        memory_tool.store = original
        with contextlib.suppress(Exception):
            store._acquire().close()
        shutil.rmtree(tmp_path, ignore_errors=True)


def _get(result: str) -> dict:
    return json.loads(result)


def _references_of(store, node_id: int) -> set:
    """该节点的 REFERENCES 出边目标集合。"""
    return {e.target_id for e in store.get_edges(node_id) if (getattr(e, "label", "") or "").upper() == "REFERENCES"}


def test_mem_ingest_creates_reference_edge_to_kb_chunk(iso_store):
    """写入记忆时，命中的知识切片应即时落成 REFERENCES 边（此前只写 payload）。"""
    kb = _get(memory_tool.mem_ingest(content="三角洲行动 游戏 内存优化 排查", type="kb_chunk", domain="kb"))
    kb_id = kb["node_id"]

    mem = _get(memory_tool.mem_ingest(content="三角洲行动 游戏 内存优化 排查", type="memory", domain="hermes"))
    mem_id = mem["node_id"]

    payload = iso_store.get_node(mem_id)["payload"]
    linked = {int(x) for x in (payload.get("linked_from") or [])}
    assert kb_id in linked, f"知识关联检测未命中切片: {payload.get('linked_from')}"

    refs = _references_of(iso_store, mem_id)
    assert kb_id in refs, f"linked_from 未落成 REFERENCES 边: {refs}"


def test_reference_edges_match_linked_from(iso_store):
    """不变量：REFERENCES 出边集合 == payload.linked_from 集合。"""
    _get(memory_tool.mem_ingest(content="Palimpsest 记忆写入与知识关联", type="kb_chunk", domain="kb"))
    mem = _get(memory_tool.mem_ingest(content="Palimpsest 记忆写入与知识关联", type="memory", domain="hermes"))
    mem_id = mem["node_id"]

    linked = {int(x) for x in (iso_store.get_node(mem_id)["payload"].get("linked_from") or [])}
    assert _references_of(iso_store, mem_id) == linked, "REFERENCES 边与 linked_from 不一致"
