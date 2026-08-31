# -*- coding: utf-8 -*-
"""
T067/T068 工程优化回归测试
===========================
覆盖：
  1. main.py GET /memory/{node_id}：命中返回 payload，不存在 404
  2. core/fts_index.py search_fts 含双引号查询：不抛异常且走 LIKE 兜底
  3. mcp_tools.memory mem_review 脏数据：importance 为非数值字符串不抛异常（_to_float 兜底）
  4. scripts/check_fts_consistency 内容级对账：FTS content 与主库不一致 → content_drift

隔离保证：conftest 已把 DB_PATH 指向临时库，全部测试不触碰正式库 data/mh_memory.db。
"""

import json


def _get(result: str) -> dict:
    return json.loads(result)


# ---------------------------------------------------------------------------
# 1. main.py GET /memory/{node_id}
# ---------------------------------------------------------------------------
def test_get_memory_returns_payload(db_path):
    """建节点后 GET /memory/{nid} 返回完整 payload。"""
    from fastapi.testclient import TestClient

    from main import app
    from mcp_tools import store

    emb = store.embed_text("GET 端点护栏：一条用于读取端点的记忆内容")
    nid = store.insert_node(
        {"type": "memory", "content": "GET 端点护栏：一条用于读取端点的记忆内容",
         "importance": 0.7, "domain": "hero"},
        emb,
    )

    client = TestClient(app)
    resp = client.get(f"/memory/{nid}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == nid
    assert data["payload"]["content"] == "GET 端点护栏：一条用于读取端点的记忆内容"
    assert data["payload"]["type"] == "memory"
    assert data["payload"]["importance"] == 0.7


def test_get_memory_not_found(db_path):
    """GET 不存在的节点应返回 404。"""
    from fastapi.testclient import TestClient

    from main import app

    missing_id = 999_999_999
    client = TestClient(app)
    resp = client.get(f"/memory/{missing_id}")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 2. core/fts_index.py search_fts 含双引号查询健壮性
# ---------------------------------------------------------------------------
def test_search_fts_quote_query_like_fallback(db_path):
    """查询含双引号 → 不走 FTS MATCH（避免破坏 FTS5 语法），走 LIKE 兜底且不抛异常。"""
    from core.fts_index import index_node, search_fts
    from mcp_tools import store

    emb = store.embed_text("双引号护栏：memory 内容 with 引号查询目标片段")
    nid = store.insert_node(
        {"type": "memory", "content": "双引号护栏：memory 内容 with 引号查询目标片段"},
        emb,
    )
    index_node(nid, "双引号护栏：memory 内容 with 引号查询目标片段")

    # 含双引号的查询：不应抛异常，返回 LIKE 兜底结果
    results = search_fts('含"引号"', limit=10)
    assert isinstance(results, list), results
    assert results == [] or all("node_id" in r for r in results), results

    # 不含引号的正常查询仍走 FTS MATCH，能命中
    normal = search_fts("双引号护栏", limit=10)
    assert any(r["node_id"] == nid for r in normal), normal


def test_search_fts_short_query_like(db_path):
    """短查询（<3字符）走 LIKE 兜底，不抛异常。"""
    from core.fts_index import index_node, search_fts
    from mcp_tools import store

    emb = store.embed_text("短查询护栏内容 ab")
    nid = store.insert_node({"type": "memory", "content": "短查询护栏内容 ab"}, emb)
    index_node(nid, "短查询护栏内容 ab")

    results = search_fts("ab", limit=10)
    assert isinstance(results, list), results
    assert any(r["node_id"] == nid for r in results), results


# ---------------------------------------------------------------------------
# 3. mcp_tools.memory mem_review 脏数据防御
# ---------------------------------------------------------------------------
def test_mem_review_dirty_importance(db_path):
    """importance 为非数值字符串的节点，mem_review 不应抛异常（_to_float 兜底）。"""
    from core.utils import _to_float
    from mcp_tools import store
    from mcp_tools.memory import mem_review

    assert _to_float("abc", 0) == 0.0      # 非数值字符串 → 默认值
    assert _to_float(None, 0) == 0.0       # None → 默认值
    assert _to_float("0.8", 0) == 0.8      # 合法数字字符串 → 数值

    # 直写一个 importance 为非数值字符串的节点
    emb = store.embed_text("脏数据护栏：importance 是字符串的记忆内容")
    store.insert_node(
        {"type": "memory", "content": "脏数据护栏：importance 是字符串的记忆内容",
         "importance": "not-a-number", "status": "active"},
        emb,
    )

    # mem_review 应正常返回 JSON，不抛异常（高/低价值候选过滤用 _to_float 兜底）
    raw = mem_review(days=7)
    data = _get(raw)
    assert "stats" in data, data
    assert isinstance(data.get("high_value_candidates"), list), data
    assert isinstance(data.get("low_value_candidates"), list), data


# ---------------------------------------------------------------------------
# 4. scripts/check_fts_consistency 内容级对账（content_drift）
# ---------------------------------------------------------------------------
def test_check_fts_consistency_content_drift(db_path):
    """主库 content 与 FTS content 不一致 → 报 content_drift 且 consistent=False。"""
    from core.fts_index import _db_path, sync_node
    from mcp_tools import store
    from scripts.check_fts_consistency import check

    emb = store.embed_text("内容对账护栏：主库全文内容 A")
    nid = store.insert_node(
        {"type": "memory", "content": "内容对账护栏：主库全文内容 A"}, emb)

    # 往 FTS 写入与主库不一致的 content（模拟漂移）
    sync_node(nid, "内容对账护栏：这是 FTS 侧的旧内容")
    assert _db_path()  # 确认 fts.db 已就位

    result = check(store)
    assert result["consistent"] is False, result
    assert any(d["node_id"] == nid for d in result["content_drift"]), result

    # 修复：重建后一致
    from core.fts_index import rebuild
    rebuild(store)
    result2 = check(store)
    assert result2["consistent"] is True, result2
    assert result2["content_drift"] == [], result2
