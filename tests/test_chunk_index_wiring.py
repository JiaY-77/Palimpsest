"""三方写入接入的契约测试（chunk 索引第 2 步：mem_ingest / reembed / PUT-PATCH）。

验收闸门（本文件钉行为）：
  1. ``mem_ingest`` 2000+ 字多段落内容 → ``chunk_stats`` 该 node ≥ 2 行
  2. ``PUT /memory/{id}`` 改 content 成另一段 2000+ 字 → 旧行被替换
     （行数按新内容、旧独有文字在 chunk_text 里查不到）
  3. 短内容（< 1500 字单段）→ 该 node 在 chunk 表里 0 行
  4. ``CHUNK_INDEX_ENABLED=false`` → 三处接入都不写 chunk（chunk_stats 不变）
  5. 写入失败（mock ``embed_text`` 抛异常）→ 主写入照常成功、节点可读
  6. ``POST /memory/{id}/reembed`` → chunk 行随重嵌重建

缺陷 1 / 缺陷 2 的存储层回归在 tests/test_chunk_index.py（本文件只管接线）。

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder；本文件的
``_chunk_db`` 夹具把 CHUNK_DB_PATH 指向 per-test 独立文件，node_chunks
数据与主库随测试全部隔离；autouse 清理夹具删掉本文件新建的记忆节点。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3

import pytest

import mcp_tools.memory as memory_tool
from config import Config
from core import chunk_index as ci
from mcp_tools import store

_LONG_CACHE: dict[str, str] = {}  # 按标签缓存，避免每个测试重复拼接


def _long_content(tag: str = "甲") -> str:
    """三段 × ~700 字的 2000+ 字多段落内容（保证 split_chunks 出 ≥ 2 段）。"""
    if tag not in _LONG_CACHE:
        _LONG_CACHE[tag] = "\n\n".join(
            (f"第{n}段开头{tag}。" + f"{tag}字叙述细节填充很长的一段内容。" * 85) for n in (1, 2, 3)
        )
    return _LONG_CACHE[tag]


def _rows(node_id: int) -> list[tuple]:
    db = os.environ["CHUNK_DB_PATH"]
    if not os.path.isfile(db):
        return []
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT chunk_idx, chunk_text, model, dim FROM node_chunks WHERE node_id = ? ORDER BY chunk_idx",
            (node_id,),
        ).fetchall()
    finally:
        conn.close()


def _get(result: str) -> dict:
    return json.loads(result)


@pytest.fixture(autouse=True)
def _chunk_db(tmp_path, monkeypatch):
    """每测试独立 node_chunks.db 文件，天然隔离（与 test_chunk_index 同款）。"""
    db = os.path.join(str(tmp_path), "node_chunks_wiring.db")
    monkeypatch.setenv("CHUNK_DB_PATH", db)
    yield db


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 临时库里跑集成测试——测完删掉本测试新建的节点。"""

    def _ids() -> set:
        try:
            return {nid for nid, _ in store.iter_payloads()}
        except Exception:  # noqa: BLE001 —— 清理尽力而为
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


def _ingest(**kwargs) -> dict:
    defaults = {"content": _long_content(), "type": "memory", "domain": "chunk_test"}
    return _get(memory_tool.mem_ingest(**{**defaults, **kwargs}))


# ---------- 1. mem_ingest 接线 ----------


def test_ingest_multi_paragraph_writes_chunks():
    r = _ingest()
    assert r["stored"] is True, r
    rows = _rows(r["node_id"])
    assert len(rows) >= 2, (r["node_id"], rows)
    # model 标识来自 Config 推导（不硬编码），且每行 dim 与 fake embedder 一致
    expected_model = ci.embedding_model_id(store)
    assert all(row[2] == expected_model for row in rows), rows
    assert all(row[3] == store.dim for row in rows), rows
    stats = ci.chunk_stats()
    assert stats["by_dim"] == {str(store.dim): stats["total_chunks"]}, stats


def test_ingest_short_content_no_chunks():
    short = "这是一条单段短记忆，长度远小于一千五百字的切块阈值。"
    r = _ingest(content=short)
    assert r["stored"] is True, r
    assert _rows(r["node_id"]) == [], _rows(r["node_id"])
    assert ci.chunk_stats()["total_chunks"] == 0


# ---------- 2. PUT / PATCH 改 content 接线 ----------


def test_put_content_change_replaces_chunks():
    r = _ingest()
    nid = r["node_id"]
    old_rows = _rows(nid)
    assert len(old_rows) >= 2

    new_content = _long_content("乙")
    resp = _client().put(f"/memory/{nid}", json={"content": new_content})
    assert resp.status_code == 200, resp.text
    new_rows = _rows(nid)
    # 行数按新内容（同分块器同输入形态 → 段数一致）
    assert len(new_rows) == len(old_rows), (len(old_rows), len(new_rows))
    # 旧独有文字在新 chunk_text 里查不到（旧行被整组替换）
    built = "".join(row[1] for row in new_rows)
    assert "第1段开头甲" not in built
    assert "第1段开头乙" in built


def test_put_short_content_clears_old_chunks():
    r = _ingest()
    nid = r["node_id"]
    assert len(_rows(nid)) >= 2
    resp = _client().put(f"/memory/{nid}", json={"content": "改成一条单段短记忆。"})
    assert resp.status_code == 200, resp.text
    assert _rows(nid) == [], _rows(nid)


# ---------- 3. reembed 接线 ----------


def test_reembed_rebuilds_chunks():
    r = _ingest()
    nid = r["node_id"]
    before = len(_rows(nid))
    assert before >= 2
    # 手工清空 chunk 行，模拟历史缺口 → reembed 后重建
    assert ci.remove_node_chunks(nid) == before
    assert _rows(nid) == []
    resp = _client().post(f"/memory/{nid}/reembed")
    assert resp.status_code == 200, resp.text
    after = _rows(nid)
    assert len(after) == before, after
    assert all(row[3] == store.dim for row in after), after


# ---------- 4. 总开关 CHUNK_INDEX_ENABLED ----------


def test_chunk_index_disabled_skips_all_wiring(monkeypatch):
    monkeypatch.setattr(Config, "CHUNK_INDEX_ENABLED", False)
    c = _client()
    # 接入 1：mem_ingest
    r = _ingest()
    assert r["stored"] is True, r
    assert _rows(r["node_id"]) == []
    # 接入 2：PUT 改 content（也不写）
    resp = c.put(f"/memory/{r['node_id']}", json={"content": _long_content("乙")})
    assert resp.status_code == 200, resp.text
    assert _rows(r["node_id"]) == []
    # 接入 3：reembed（也不重建）
    resp = c.post(f"/memory/{r['node_id']}/reembed")
    assert resp.status_code == 200, resp.text
    assert _rows(r["node_id"]) == []
    # chunk_stats 全程不变（开关关闭零写入）
    stats = ci.chunk_stats()
    assert stats["total_chunks"] == 0, stats


# ---------- 5. 失败不阻塞主写路径 ----------


def test_embed_failure_does_not_block_ingest(monkeypatch):
    def _boom(text: str) -> list[float]:
        raise RuntimeError("embed 管线故障（测试注入）")

    # 类级替换（conftest fake 只替换了实例与类），每测试 teardown 自动还原：
    # embed_text 抛错但 embed_text_with_audit 仍是 fake → 主写入照常，chunk 索引失败
    monkeypatch.setattr(type(store), "embed_text", staticmethod(_boom))
    monkeypatch.setattr(store, "embed_text", _boom)

    r = _ingest()
    assert r["stored"] is True, r
    node = store.get_node(r["node_id"])
    assert node is not None
    assert node["payload"]["content"] == _long_content()
    assert _rows(r["node_id"]) == []  # 失败没写成任何 chunk，也没半删半写
