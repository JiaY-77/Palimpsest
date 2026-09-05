# -*- coding: utf-8 -*-
"""
mem_communities 社区发现工具回归测试
====================================
覆盖 leiden 社区发现的输出结构与边界；pagerank 为内部保留函数，仅冒烟验证可调用。

隔离保证：
  - conftest 已把 DB_PATH 指向 session 级临时库 + fake embedder；
  - 本文件用独立 domain 命名空间，断言按自己插入的节点 id 集合，避免互扰；
  - content 用低相似短语（fake embedder 2-gram 共享前缀会互相高相似，
    干扰 test_consolidate_dryrun 的候选断言）。
"""
import json
import uuid

from mcp_tools._common import store


def _mk_cluster_graph():
    """造 2 簇 6 节点图：A={0,1,2 三角}, B={3,4,5 三角}，跨簇桥 (0,3)。"""
    db = store._acquire()
    ids = []
    try:
        for i in range(6):
            vec = [0.0] * store.dim
            vec[i % 2] = 0.5
            nid = db.insert(vec, {
                "type": "memory",
                "domain": f"b1_{uuid.uuid4().hex[:8]}",
                "content": f"分析测试内容节点编号{i}",
            })
            ids.append(nid)
        for a, b in [(0, 1), (1, 2), (2, 0), (3, 4), (4, 5), (5, 3), (0, 3)]:
            db.link(ids[a], ids[b], "REL")
    finally:
        try:
            db.close()
        except Exception:
            pass
    # 直插后必须同步 FTS 索引——否则共享 session 库主库/FTS 不一致，
    # 污染后跑的 test_fts_check（test_mem_recent_behavior 同款教训）
    _sync_fts(ids, [f"分析测试内容节点编号{i}" for i in range(6)])
    return set(ids)


def _sync_fts(ids, contents):
    """对直插节点同步 FTS（与 mem_ingest 事务后 index_node 链路一致）。"""
    try:
        from mcp_tools.memory import index_node
        for nid, content in zip(ids, contents):
            try:
                index_node(nid, content)
            except Exception:
                pass
    except Exception:
        pass


def _call(**kw):
    from mcp_tools.graph import mem_communities
    return json.loads(mem_communities(**kw))


def test_communities_detects_clusters():
    """6 节点 2 簇图：leiden 应产出 >=1 个 size>=2 社区，成员均为自己插入的节点。"""
    mine = _mk_cluster_graph()
    r = _call(min_community_size=2)
    assert r.get("mode") == "communities"
    assert r.get("num_clusters", 0) >= 1
    comms = r.get("communities", [])
    assert comms, "应至少返回一个 size>=2 社区"
    # 社区成员 id 都在我们插入的集合内
    for c in comms:
        assert c.get("size", 0) >= 2
        for m in c.get("members", []):
            assert m["id"] in mine
            assert "title" in m and "domain" in m and "type" in m


def test_communities_without_summary_only_ids():
    """with_summary=False：members 为空数组但 node_ids 完整。"""
    _mk_cluster_graph()
    r = _call(min_community_size=2, with_summary=False)
    assert r.get("communities")
    for c in r["communities"]:
        assert c.get("node_ids"), "node_ids 不应为空"
        assert c.get("members") == []


def test_empty_graph_returns_hint():
    """孤立节点（无边）返回友好提示而非异常。"""
    db = store._acquire()
    content = "孤立节点测试内容"
    try:
        nid = db.insert([0.5] * store.dim, {"type": "memory", "domain": f"iso_{uuid.uuid4().hex[:8]}", "content": content})
    finally:
        try:
            db.close()
        except Exception:
            pass
    _sync_fts([nid], [content])
    r = _call(min_community_size=2)
    # 孤立节点无边：要么返回空社区列表，要么带 hint
    assert r.get("communities") in ([], None) or "hint" in r


def test_do_pagerank_internal_smoke():
    """_do_pagerank 内部函数仍可调用且正常返回结构（保留代码可用性冒烟）。

    注意：不 catch 吞异常——pagerank 虽是内部保留，但代码本身必须健康；
    若炸说明 _do_pagerank 或数据有 bug，应当暴露而非静默通过。
    """
    from mcp_tools.graph import _do_pagerank
    db = store._acquire()
    try:
        node_count = db.node_count()
        result = _do_pagerank(db, top_k=3, node_count=node_count)
        assert isinstance(result, dict)
        assert result.get("mode") == "pagerank"
        assert "top_nodes" in result
    finally:
        try:
            db.close()
        except Exception:
            pass
