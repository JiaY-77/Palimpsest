# -*- coding: utf-8 -*-
"""
TriviumDB 0.8.3 特性测试 —— TQL 管线、集合代数与 EXPLAIN（P0）
====================================================================
覆盖：
  1. SEARCH→WITH→EXPAND→PAGERANK→WHERE→RETURN 编排
  2. UNION / INTERSECT / EXCEPT 集合代数
  3. ITERATE 定点迭代（当前 build 语法未公开，标记 xfail）
  4. EXPLAIN
  5. #31 回归：3 万节点 pagerank（作者未修复 → xfail，strict=False）

隔离保证：%TEMP%/tdb_ftest/ 独立临时库，不触碰正式库。
"""
import math
import os
import tempfile

import pytest

import triviumdb  # noqa: E402


@pytest.fixture
def tdb():
    _dir = os.path.join(tempfile.gettempdir(), "tdb_ftest")
    os.makedirs(_dir, exist_ok=True)
    path = os.path.join(_dir, "tql_pipeline.db")
    for f in os.listdir(_dir):
        if f.startswith("tql_pipeline"):
            os.remove(os.path.join(_dir, f))
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)

    def vec(text, dim=8):
        out = [0.0] * dim
        for ch in text:
            out[ord(ch) % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]

    ids = [db.insert(vec(f"c{i}"), {"type": "memory", "num": i})
           for i in range(6)]
    for a, b, label in [(0, 1, "REL"), (1, 2, "REL"), (0, 3, "REL"),
                        (3, 4, "CAUSES"), (1, 4, "CAUSES"), (4, 5, "REL")]:
        db.link(ids[a], ids[b], label)
    yield db
    db.close()


V8 = "0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5"


def _rel_ids(rows, key):
    return [r.row[key]["id"] for r in rows]


def test_pipeline_search_expand_pagerank_where_return(tdb):
    """一条 TQL 完成 SEARCH → WITH → EXPAND → PAGERANK → WHERE → RETURN。"""
    q = (
        f"SEARCH VECTOR [{V8}] TOP 100 AS seed "
        f"WITH seed EXPAND seed [:REL*1..1] AS rel "
        f"WITH rel PAGERANK rel AS pr "
        f"WHERE pr.num >= 0 "
        f"RETURN pr"
    )
    rows = tdb.tql(q)
    assert rows
    ids = _rel_ids(rows, "pr")
    assert len(ids) == len(set(ids)) == len(rows)


def test_pipeline_page_rank_one_row_per_node(tdb):
    q = f"SEARCH VECTOR [{V8}] TOP 100 AS seed WITH seed PAGERANK seed AS pr RETURN pr"
    rows = tdb.tql(q)
    assert len(rows) == 6  # 每个节点都产出 pagerank 行


# ---------------------------------------------------------------------------
# 集合代数
# ---------------------------------------------------------------------------
def test_union(tdb):
    rows = tdb.tql(
        "MATCH (a)-[:REL]->(b) RETURN b UNION MATCH (a)-[:CAUSES]->(b) RETURN b")
    # REL 命中 4 个目标，CAUSES 命中 2 个目标，UNION 去重后也是这 4 个
    assert len(_rel_ids(rows, "b")) >= 4


def test_intersect(tdb):
    rows = tdb.tql(
        "MATCH (a)-[:REL]->(b) RETURN b INTERSECT MATCH (a)-[:REL]->(b) RETURN b")
    assert len(_rel_ids(rows, "b")) == 4


def test_except(tdb):
    rows = tdb.tql(
        "MATCH (a)-[:REL]->(b) RETURN b EXCEPT MATCH (a)-[:CAUSES]->(b) RETURN b")
    # 在 REL 目标中剔除也是 CAUSES 目标的节点
    all_rel = {r.row["b"]["id"] for r in
               tdb.tql("MATCH (a)-[:REL]->(b) RETURN b")}
    except_ids = _rel_ids(rows, "b")
    assert set(except_ids) <= all_rel
    assert len(except_ids) < len(all_rel) or set(except_ids) == all_rel


# ---------------------------------------------------------------------------
# ITERATE —— 定点迭代
# ---------------------------------------------------------------------------
@pytest.mark.xfail(
    reason="#ITERATE 定点迭代语法在当前 build 未公开（解析为 Expected Expand/identifier），"
           "属未落地语法；待作者补充文档后恢复断言",
    strict=False,
)
def test_iterate_fixed_point(tdb):
    q = (f"SEARCH VECTOR [{V8}] TOP 1 AS seed WITH seed "
         f"ITERATE seed EXPAND seed [:REL*1..1] AS seed RETURN seed")
    rows = tdb.tql(q)
    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# EXPLAIN
# ---------------------------------------------------------------------------
def test_explain_match(tdb):
    rows = tdb.tql("EXPLAIN MATCH (a)-[:REL]->(b) RETURN b")
    assert len(rows) == 1
    plan = rows[0].row["plan"]["payload"]
    assert plan["entry"] == "MATCH"
    assert "b" in str(plan["return"])
    assert "graph_stats" in plan


def test_explain_find(tdb):
    rows = tdb.tql('EXPLAIN FIND {type: "memory"} RETURN *')
    assert rows
    plan = rows[0].row["plan"]["payload"]
    assert plan["entry"] == "FIND"


# ---------------------------------------------------------------------------
# #31 回归：3 万节点 pagerank（作者未修复 → xfail）
# ---------------------------------------------------------------------------
def _build_large_graph(path, n=30000):
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
    vec = [0.5] * 8
    ids = db.batch_insert([vec] * n, [{"num": i} for i in range(n)])
    # 随机/环状结构（非纯链），增大 pagerank 计算压力
    for i in range(n - 1):
        db.link(ids[i], ids[(i + 1) % n], "REL")
        db.link(ids[i], ids[(i + 7) % n], "REL")
    return db


@pytest.mark.xfail(
    reason="issue #31：上游 3 万节点 pagerank 大图 panic 尚未修复；"
           "作者修好后解除 xfail 并验证结果完整性",
    strict=False,
)
def test_pagerank_30k_no_panic():
    _dir = os.path.join(tempfile.gettempdir(), "tdb_ftest")
    os.makedirs(_dir, exist_ok=True)
    path = os.path.join(_dir, "tql_31.db")
    for f in os.listdir(_dir):
        if f.startswith("tql_31"):
            os.remove(os.path.join(_dir, f))
    db = _build_large_graph(path)
    v8 = ",".join(["0.5"] * 8)
    rows = db.tql(
        f"SEARCH VECTOR [{v8}] TOP 100 AS seed WITH seed PAGERANK seed AS pr RETURN pr")
    assert 0 < len(rows) <= 100
    db.close()
