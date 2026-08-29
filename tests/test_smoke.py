# -*- coding: utf-8 -*-
"""
              —— 核心冒烟测试 ——
覆盖 Palimpsest 记忆主链路：写入 → 语义检索 → 全文读取 → 图谱建边/邻居
→ 敏感信息拦截 → 混合检索（FTS 侧命中）。

隔离保证：conftest 已把 DB_PATH 指向临时库，全部测试不触碰正式库 data/mh_memory.db。
"""

import json

from mcp_tools import (  # noqa: E402
    graph_neighbors, mem_get_full, mem_hybrid_search, mem_ingest, mem_link,
    mem_search,
)


def _get(result: str) -> dict:
    """MCP 工具返回 JSON 字符串 → dict"""
    return json.loads(result)


def test_ingest_search_roundtrip(db_path):
    """写入一条记忆 → mem_search 能检索到 → mem_get_full 能取全文。"""
    content = "护栏冒烟：小帕在临时库写下的一条独一无二的记忆片段"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is True, r
    nid = r["node_id"]

    # mem_search 应能检索到该节点
    s = _get(mem_search(content, scope="memory"))
    assert s["results"], "mem_search 未返回任何结果"
    assert any(item["id"] == nid for item in s["results"]), (
        f"mem_search 未命中刚写入的节点 {nid}"

    )

    # mem_get_full 应能取回全文
    full = _get(mem_get_full(nid))
    assert full["found"] is True
    assert full["payload"]["content"] == content


def test_link_graph(db_path):
    """写入两条记忆 → mem_link 建边 → graph_neighbors 能看到邻居。"""
    a = _get(mem_ingest(content="图谱冒烟记忆甲：概念 X 与概念 Y 相关联", type="memory"))
    b = _get(mem_ingest(content="图谱冒烟记忆乙：概念 Y 是概念 X 的子集", type="memory"))
    assert a["stored"] and b["stored"]

    link = _get(mem_link(a["node_id"], b["node_id"], relation="RELATED_TO"))
    assert link["linked"] is True

    g = _get(graph_neighbors(a["node_id"], depth=1))
    assert g["count"] >= 1, g
    assert any(item["target_id"] == b["node_id"] for item in g["relations"]), g


def test_secret_scan(db_path):
    """写入含 API key 的内容 → 拒绝入库，stored:False 且命中 openai_key 规则。"""
    content = "这里泄露了一个密钥：sk-abcdefghijklmnopqrstuvwxyz 请勿保存"
    r = _get(mem_ingest(content=content, type="memory"))
    assert r["stored"] is False, r
    assert "openai_key" in r.get("rules", []), r


def test_fts_hybrid(db_path):
    """写入后 mem_hybrid_search 应能命中（FTS 侧 trigram 精确子串）。"""
    needle = "混合检索护栏标记词xyzabc"
    r = _get(mem_ingest(content=needle, type="memory"))
    assert r["stored"] is True
    nid = r["node_id"]

    h = _get(mem_hybrid_search(needle, scope="memory", top_k=5))
    assert h["results"], "mem_hybrid_search 未返回任何结果"
    hit = next((item for item in h["results"] if item.get("id") == nid), None)
    assert hit is not None, f"混合检索未命中刚写入节点 {nid}: {h}"
    # FTS 侧应标记命中（mem_ingest 已同步 index_node 到临时 fts.db）
    assert hit["meta"].get("fts_hit") is True, hit


def test_consolidate_dryrun(db_path):
    """写入两条几乎相同的记忆 → consolidate(dry_run=True) 只预览候选，不真正合并。

    用 store.insert_node 直写（绕过 mem_ingest 的冲突检测会自动把相似旧记忆标
    outdated，导致找不到 active+active 对）。两条内容几乎一致、均为 active，
    应能被 find_similar_pairs 扫出为候选对。
    """
    from core.consolidator import consolidate
    from mcp_tools import store

    base = "容量合并护栏：一条几乎完全相同的记忆内容，用于验证 dry-run 预览候选"
    emb_a = store.embed_text(base)
    emb_b = store.embed_text(base + "（副本甲）")
    a_id = store.insert_node({"type": "memory", "content": base}, emb_a)
    b_id = store.insert_node({"type": "memory", "content": base + "（副本甲）"}, emb_b)
    assert a_id != b_id

    r = consolidate(store, dry_run=True)
    assert r["dry_run"] is True, r
    assert isinstance(r.get("candidates"), list), r
    assert r["candidates"], "高相似记忆应产生至少一对候选"

    # 单个候选对结构完整（a/b/score/a_imp/b_imp/a_content/b_content）
    c = r["candidates"][0]
    for k in ("a", "b", "score", "a_imp", "b_imp", "a_content", "b_content"):
        assert k in c, f"候选缺字段 {k}: {c}"
    assert {c["a"], c["b"]} == {a_id, b_id}, c

    # dry_run 不真正合并：不应包含 merged 结果，两个原始节点仍在库中（active）
    assert "merged" not in r, r
    assert store.get_node(a_id)["payload"]["status"] == "active"
    assert store.get_node(b_id)["payload"]["status"] == "active"
