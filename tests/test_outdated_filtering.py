# -*- coding: utf-8 -*-
"""
outdated 检索语义测试（v4.0，2026-09-05 产品决策）
==================================================
产品语义：搜索（mem_search / mem_hybrid_search / mem_retrieve）默认只返回当前有效
（status != "outdated"）节点；outdated 旧版本保留库中可追溯，普通检索不出现，
走显式 include_outdated=True 通道才可见。图谱关联区（neighbors）同理不展示旧版本。

触发 outdated：mem_ingest 写入完全相同内容两次（fake embedder 是字符 2-gram，
相同文本向量完全相同 → score≈1.0 > 0.75 → 触发 REVISED_BY：旧版标 outdated）。

隔离设计：
  - 每测试用唯一 domain 命名空间，断言一律按 namespace 过滤自己插入的节点；
  - content 用彼此词面差异大的句子：共享短前缀的 content 会互相高相似，
    干扰 test_consolidate_dryrun 的候选断言。
"""
import json
import uuid


def _ns():
    return f"odf_{uuid.uuid4().hex[:10]}"


def _get(result: str) -> dict:
    """MCP 工具返回 JSON 字符串 → dict"""
    return json.loads(result)


def _ingest_pair(store, mem_ingest, text, ns):
    """同内容写两次触发 REVISED_BY；返回 (old_id, new_id)。"""
    r1 = _get(mem_ingest(content=text, type="memory", domain=ns))
    r2 = _get(mem_ingest(content=text, type="memory", domain=ns))
    assert r1["stored"] is True and r2["stored"] is True, (r1, r2)
    assert r2["conflict_found"] is True, r2
    old_id, new_id = r1["node_id"], r2["node_id"]
    assert old_id in r2["outdated_ids"], r2
    assert store.get_node(old_id)["payload"]["status"] == "outdated"
    assert store.get_node(new_id)["payload"]["status"] != "outdated"
    return old_id, new_id


def test_mem_search_default_hides_outdated():
    """mem_search 默认只回 active 新版；include_outdated=True 时新旧两版都可见。"""
    from mcp_tools import mem_ingest, mem_search, store

    ns = _ns()
    text = "晨雾中的峡谷在日出时泛出金色光晕"
    old_id, new_id = _ingest_pair(store, mem_ingest, text, ns)

    default = _get(mem_search(text, scope="memory", domain=ns, top_k=20))
    ids = [it["id"] for it in default["results"]]
    assert new_id in ids, f"新版应出现在常规检索: {ids}"
    assert old_id not in ids, f"outdated 旧版不应出现在常规检索: {ids}"

    incl = _get(mem_search(text, scope="memory", domain=ns, top_k=20,
                           include_outdated=True))
    ids2 = [it["id"] for it in incl["results"]]
    assert new_id in ids2, f"include_outdated 应含新版: {ids2}"
    assert old_id in ids2, f"include_outdated 应含旧版(可追溯): {ids2}"


def test_mem_search_include_outdated_false_explicit():
    """显式 include_outdated=False 与缺省行为一致（泊车逃生通道关闭）。"""
    from mcp_tools import mem_ingest, mem_search, store

    ns = _ns()
    text = "石砌的钟楼整点敲响悠长的铜钟声"
    old_id, new_id = _ingest_pair(store, mem_ingest, text, ns)

    data = _get(mem_search(text, scope="memory", domain=ns, top_k=20,
                           include_outdated=False))
    ids = [it["id"] for it in data["results"]]
    assert new_id in ids
    assert old_id not in ids


def test_mem_retrieve_default_hides_outdated():
    """mem_retrieve（用户检索入口）默认同样过滤 outdated；include_outdated=True 全返。"""
    from mcp_tools import mem_ingest, mem_retrieve, store

    ns = _ns()
    text = "琥珀色的黄昏挂在老梧桐的枝桠间"
    old_id, new_id = _ingest_pair(store, mem_ingest, text, ns)

    default = _get(mem_retrieve(text, domain=ns, top_k=20))
    ids = [it["id"] for it in default["results"]]
    assert new_id in ids, f"新版应出现在 mem_retrieve: {ids}"
    assert old_id not in ids, f"outdated 旧版不应出现在 mem_retrieve: {ids}"

    incl = _get(mem_retrieve(text, domain=ns, top_k=20, include_outdated=True))
    ids2 = [it["id"] for it in incl["results"]]
    assert old_id in ids2, f"include_outdated 通道应含旧版: {ids2}"
    assert new_id in ids2


def test_mem_hybrid_search_rrf_hides_outdated():
    """mem_hybrid_search(rrf) 默认过滤 outdated；include_outdated=True 双版可见。"""
    from mcp_tools import mem_hybrid_search, mem_ingest, store

    ns = _ns()
    text = "深秋的银杏叶铺满湿漉漉的青石路"
    old_id, new_id = _ingest_pair(store, mem_ingest, text, ns)

    default = _get(mem_hybrid_search(text, scope="memory", domain=ns, top_k=20,
                                     mode="rrf"))
    ids = [it["id"] for it in default["results"]]
    assert new_id in ids, f"混合(rrf)检索应含新版: {ids}"
    assert old_id not in ids, f"outdated 旧版不应出现在混合(rrf)检索: {ids}"

    incl = _get(mem_hybrid_search(text, scope="memory", domain=ns, top_k=20,
                                  mode="rrf", include_outdated=True))
    ids2 = [it["id"] for it in incl["results"]]
    assert old_id in ids2, f"include_outdated 混合(rrf)应含旧版: {ids2}"
    assert new_id in ids2


def test_mem_hybrid_search_cascade_hides_outdated():
    """mem_hybrid_search(cascade) 默认过滤 outdated；include_outdated=True 双版可见。"""
    from mcp_tools import mem_hybrid_search, mem_ingest, store

    ns = _ns()
    text = "雪松的枝头挂满昨夜落下的新雪"
    old_id, new_id = _ingest_pair(store, mem_ingest, text, ns)

    default = _get(mem_hybrid_search(text, scope="memory", domain=ns, top_k=20,
                                     mode="cascade"))
    ids = [it["id"] for it in default["results"]]
    assert new_id in ids, f"混合(cascade)检索应含新版: {ids}"
    assert old_id not in ids, f"outdated 旧版不应出现在混合(cascade)检索: {ids}"

    incl = _get(mem_hybrid_search(text, scope="memory", domain=ns, top_k=20,
                                  mode="cascade", include_outdated=True))
    ids2 = [it["id"] for it in incl["results"]]
    assert old_id in ids2, f"include_outdated 混合(cascade)应含旧版: {ids2}"
    assert new_id in ids2


def test_graph_neighbors_filter_outdated():
    """图关联区：outdated 旧版不作为『当前事实』展示（正例见 test_core_algorithms 单测）。"""
    from mcp_tools import mem_ingest, mem_link, mem_search, store

    ns = _ns()
    a_text = "夜深时灯塔的灯光扫过寂静的海面"
    c_text = "海豚群在蔚蓝的浪尖跳跃翻腾"

    ra = _get(mem_ingest(content=a_text, type="memory", domain=ns))
    old_c, new_c = _ingest_pair(store, mem_ingest, c_text, ns)
    a_id = ra["node_id"]

    # a --RELATED_TO--> old_c（outdated 邻居）
    _get(mem_link(a_id, old_c, relation="RELATED_TO"))

    data = _get(mem_search(a_text, scope="memory", domain=ns, top_k=5,
                           include_neighbors=True))
    sem_ids = [it["id"] for it in data["results"]]
    assert a_id in sem_ids, f"检索应命中 a: {sem_ids}"

    neighbor_ids = [n["id"] for n in data.get("neighbors", [])]
    assert old_c not in neighbor_ids, f"outdated 邻居不应展示: {neighbor_ids}"
    # 新版同内容节点未建边，也不应出现
    assert new_c not in neighbor_ids


def test_kb_chunk_outdated_also_filtered():
    """kb_chunk 若被标 outdated 同样过滤（不区分类型，保持一致性）。"""
    from mcp_tools import mem_search, store
    from mcp_tools.memory import index_node

    ns = _ns()
    text = f"知识库切片检索词{ns}"
    emb = store.embed_text(text)
    nid = store.insert_node(
        {"type": "kb_chunk", "content": text, "domain": "kb"}, emb)
    store.update_payload(nid, {"status": "outdated"})
    # insert_node 直写不走 FTS 同步：手动补索引，避免破坏 test_fts_check 一致性
    index_node(nid, text)

    default = _get(mem_search(text, scope="kb", top_k=20))
    ids = [it["id"] for it in default["results"]]
    assert nid not in ids, f"outdated kb_chunk 不应出现在常规检索: {ids}"

    incl = _get(mem_search(text, scope="kb", top_k=20, include_outdated=True))
    ids2 = [it["id"] for it in incl["results"]]
    assert nid in ids2, f"include_outdated 应含当时 kb_chunk: {ids2}"


def test_active_nodes_unaffected_by_default():
    """默认过滤只针对 outdated：普通 active 记忆检索不受影响（回归护栏）。"""
    from mcp_tools import mem_ingest, mem_search, store

    ns = _ns()
    text = "溪水绕过圆石在暮色中低语"
    r = _get(mem_ingest(content=text, type="memory", domain=ns))
    nid = r["node_id"]

    data = _get(mem_search(text, scope="memory", domain=ns, top_k=20))
    ids = [it["id"] for it in data["results"]]
    assert nid in ids, f"active 记忆应正常被检索: {ids}"