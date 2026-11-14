"""chunk 级向量召回并入 mem_search 的契约测试（#295 第 3 步）。

验收闸门（本文件钉行为）：
  1. ``CHUNK_SCORE_WEIGHT=0`` / ``CHUNK_INDEX_ENABLED=false`` 两种关闭方式下，
     ``_mem_search_impl`` 的结果 id 顺序 + 得分与「主向量独走」**逐位一致**（回归红线）
  2. chunk 路命中但主向量未召回的节点会被补进候选（manifold：patch 掉主向量召回，
     只靠真实 chunk 行命中），并走同一套 outdated 过滤（不绕过）
  3. node_score = max(主向量分, chunk cos × 权重)：两端都命中时取大
  4. 底层 search_chunks 抛异常（表损坏 / 任意故障）→ 静默降级为主向量独走，不抛

隔离保证：conftest 临时库 + fake embedder（2-gram 确定性向量）；每测试
独立 CHUNK_DB_PATH；autouse 清理夹具删掉本文件新建的记忆节点。
"""

from __future__ import annotations

import contextlib
import os

import pytest

import mcp_tools.memory as memory_tool
from config import Config
from core import chunk_index as ci

_LONG_CACHE: dict[str, str] = {}


def _long_content(tag: str = "甲") -> str:
    """三段 × ~700 字 + 独立标题行的多段落内容（保证 split_chunks 出 ≥ 3 段）。"""
    if tag not in _LONG_CACHE:
        _LONG_CACHE[tag] = f"接续点 测试标题{tag}（用于 chunk 召回测试）\n\n" + "\n\n".join(
            (f"第{n}段开头{tag}。" + f"{tag}字叙述细节填充很长的一段内容。" * 85) for n in (1, 2, 3)
        )
    return _LONG_CACHE[tag]


@pytest.fixture(autouse=True)
def _chunk_db(tmp_path, monkeypatch):
    db = os.path.join(str(tmp_path), "node_chunks_search.db")
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


def _ingest(**kwargs) -> dict:
    import json as _json

    defaults = {"content": _long_content(), "type": "memory", "domain": "chunk_search_test"}
    return _json.loads(memory_tool.mem_ingest(**{**defaults, **kwargs}))


def _snapshot(query: str, **kwargs) -> list[tuple]:
    """(id, score) 有序快照——关态等价性按它逐位比对。"""
    r = memory_tool._mem_search_impl(query, top_k=5, **kwargs)
    return [(it["id"], it["score"]) for it in r["results"]]


# ---------- 1 & 2. 两关态与主向量独走逐位一致 ----------


def test_both_kill_switches_bitwise_equal_main_only():
    """主向量必召回 + chunk 行已存在的场景下，两关态结果 = 关态主向量独走。"""
    _ingest()
    _ingest(content=_long_content("乙"))
    _ingest(content="一条与标题完全无关的短记忆甲，落点在收纳盒与晾衣绳。")

    query = "接续点 会话接续 新聊天先读"
    # 主向量独走基线：关闭写入开关（chunk 行还在库里，但检索侧整条路不读）
    from config import Config as _C

    old_enabled = _C.CHUNK_INDEX_ENABLED
    _C.CHUNK_INDEX_ENABLED = False
    try:
        baseline = _snapshot(query)
    finally:
        _C.CHUNK_INDEX_ENABLED = old_enabled
    assert baseline, "主向量基线不应为空（query 设计成命中标题节点）"

    # 关态 1：CHUNK_SCORE_WEIGHT=0（行存在、开关开，但权重 0 关整条路）
    old_w = _C.CHUNK_SCORE_WEIGHT
    _C.CHUNK_SCORE_WEIGHT = 0.0
    try:
        assert _snapshot(query) == baseline
    finally:
        _C.CHUNK_SCORE_WEIGHT = old_w

    # 关态 2：CHUNK_INDEX_ENABLED=false
    _C.CHUNK_INDEX_ENABLED = False
    try:
        assert _snapshot(query) == baseline
    finally:
        _C.CHUNK_INDEX_ENABLED = old_enabled


# ---------- 3. chunk-only 候选补召回 + 过滤不绕过 ----------


def test_chunk_only_hit_injected_and_filtered(monkeypatch):
    """主向量召回被 patch 成空 → 只有 chunk 行能命中：结果必须来自 chunk 路。"""
    r = _ingest()
    nid = r["node_id"]
    rows = os.environ["CHUNK_DB_PATH"]
    # 确认该节点 chunk 行真实存在（写入接线在 test_chunk_index_wiring 钉过）
    import sqlite3

    conn = sqlite3.connect(rows)
    try:
        n = conn.execute("SELECT COUNT(*) FROM node_chunks WHERE node_id = ?", (nid,)).fetchone()[0]
    finally:
        conn.close()
    assert n >= 2, n

    # 主向量召回 patch 成空（模拟主向量没把这个长文档节点召回进来）
    monkeypatch.setattr(memory_tool.store, "search_similar", lambda *a, **k: [])
    hits = memory_tool._mem_search_impl("接续点 会话接续 新聊天先读", top_k=5)
    assert [it["id"] for it in hits["results"]] == [nid], hits["results"]

    # 权重 0 关闭后同查询 → 空结果（主向量被 patch 空，chunk 路也关了）
    old = Config.CHUNK_SCORE_WEIGHT
    Config.CHUNK_SCORE_WEIGHT = 0.0
    try:
        hits_off = memory_tool._mem_search_impl("接续点 会话接续 新聊天先读", top_k=5)
        assert hits_off["results"] == []
    finally:
        Config.CHUNK_SCORE_WEIGHT = old

    # 过滤不绕过：节点标 outdated 后即使 chunk 命中也不进结果
    node = memory_tool.store.get_node(nid)
    assert node is not None
    memory_tool.store.update_payload(nid, {**node["payload"], "status": "outdated"})
    hits = memory_tool._mem_search_impl("接续点 会话接续 新聊天先读", top_k=5)
    assert nid not in [it["id"] for it in hits["results"]]


@pytest.mark.parametrize("status", ["outdated", "archived"])
def test_chunk_only_hit_dropped_for_every_inactive_status(monkeypatch, status):
    """#312：chunk 路补候选必须与主向量路同口径，outdated 与 archived 都不得回流。

    回归背景：``_drop_outdated_candidates``（主向量路）剔 outdated + archived，
    而多路合并后的收口只判 ``status == "outdated"`` ——于是 archived 节点（按策略
    退役的历史节点，典型是长文思维底片）只从 chunk 路回流，实测占 top-10 的 41%。
    """
    r = _ingest()
    nid = r["node_id"]
    monkeypatch.setattr(memory_tool.store, "search_similar", lambda *a, **k: [])

    ok = memory_tool._mem_search_impl("接续点 会话接续 新聊天先读", top_k=5)
    assert nid in [it["id"] for it in ok["results"]], "前置：该节点确实只能由 chunk 路召回"

    node = memory_tool.store.get_node(nid)
    assert node is not None
    memory_tool.store.update_payload(nid, {**node["payload"], "status": status})

    hits = memory_tool._mem_search_impl("接续点 会话接续 新聊天先读", top_k=5)
    assert nid not in [it["id"] for it in hits["results"]], f"{status} 节点不得经 chunk 路回流"


# ---------- 4. node_score = max(主向量分, chunk cos × 权重) ----------


def test_merge_takes_max(monkeypatch):
    """两路命中同一 node 时取 max；chunk-only 节点按 chunk cos 参与排序。"""
    a = _ingest(content=_long_content("甲"))
    b = _ingest(content=_long_content("乙"))
    nid_a, nid_b = a["node_id"], b["node_id"]

    # 主向量只召回 B 且分很低；chunk 路两节点都命中（A 的 chunk cos 高于 B）
    fake_main = [{"id": nid_b, "payload": memory_tool.store.get_node(nid_b)["payload"], "score": 0.10}]
    monkeypatch.setattr(memory_tool.store, "search_similar", lambda *a, **k: fake_main)
    monkeypatch.setattr(
        ci, "search_chunks", lambda qv, top_m, model: [(nid_a, 0, 0.90), (nid_b, 0, 0.62)], raising=True
    )

    hits = memory_tool._mem_search_impl("接续点 会话接续 新聊天先读", top_k=5)
    got = {it["id"]: it["score"] for it in hits["results"]}
    # A：chunk-only 注入 → 0.90 × 1.0；B：max(0.10 主向量, 0.62 × 1.0 chunk) = 0.62
    assert got[nid_a] == pytest.approx(0.9, abs=1e-6)
    assert got[nid_b] == pytest.approx(0.62, abs=1e-6)
    assert [it["id"] for it in hits["results"]] == [nid_a, nid_b]

    # 权重缩小到 0.5：A = 0.45，B = max(0.10, 0.31) = 0.31 → A 仍第一
    old = Config.CHUNK_SCORE_WEIGHT
    Config.CHUNK_SCORE_WEIGHT = 0.5
    try:
        hits = memory_tool._mem_search_impl("接续点 会话接续 新聊天先读", top_k=5)
        got = {it["id"]: it["score"] for it in hits["results"]}
        assert got[nid_a] == pytest.approx(0.45, abs=1e-6)
        assert got[nid_b] == pytest.approx(0.31, abs=1e-6)
    finally:
        Config.CHUNK_SCORE_WEIGHT = old


# ---------- 5. 静默降级 ----------


def test_search_chunk_failure_degrades_silently(monkeypatch):
    """底层抛异常 → 降级为主向量独走，结果与基线一致，不向上抛。"""
    _ingest()  # 造点数据 + chunk 行
    monkeypatch.setattr(
        ci, "search_chunks", lambda qv, top_m, model: (_ for _ in ()).throw(RuntimeError("chunk 表损坏"))
    )

    query = "接续点 会话接续 新聊天先读"
    from config import Config as _C

    old_enabled = _C.CHUNK_INDEX_ENABLED
    _C.CHUNK_INDEX_ENABLED = False
    try:
        baseline = _snapshot(query)
    finally:
        _C.CHUNK_INDEX_ENABLED = old_enabled

    # 降级路径开着跑：异常被吞，结果与基线逐位一致
    hits = memory_tool._mem_search_impl(query, top_k=5)
    assert [(it["id"], it["score"]) for it in hits["results"]] == baseline
