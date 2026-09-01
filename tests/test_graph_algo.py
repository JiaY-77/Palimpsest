# -*- coding: utf-8 -*-
"""
T069 · TriviumDB 0.8.3 特性测试 —— 内嵌图算法（P1）
====================================================
覆盖：
  · leiden_cluster 方法：min_community_size / max_iterations / compute_centroids
  · TQL WITH 图算法阶段：PAGERANK / WCC / DEGREE / LABEL_PROPAGATION / SA_PPR
  · 参数边界
  · 小图手算对照（leiden 连通社区）

备注：BETWEENNESS 在本 build 不是合法 TQL 阶段关键字（解析报错），
非 TQL 的 betweenness 亦无公开 Python 方法 —— 见测试内备注。

隔离保证：%TEMP%/tdb_ftest/ 独立临时库。
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
    path = os.path.join(_dir, "graph_algo.db")
    for f in os.listdir(_dir):
        if f.startswith("graph_algo"):
            os.remove(os.path.join(_dir, f))
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)

    def vec(text, dim=8):
        out = [0.0] * dim
        for ch in text:
            out[ord(ch) % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]

    ids = [db.insert(vec(f"n{i}"), {"num": i}) for i in range(9)]
    # 两个连通分量：A={0..4} 全连通三角，B={5..8} 环
    edges = [
        (0, 1), (1, 2), (2, 0),          # A 三角
        (0, 3), (3, 4), (4, 0),          # A 延伸
        (5, 6), (6, 7), (7, 8), (8, 5),  # B 环
    ]
    for a, b in edges:
        db.link(ids[a], ids[b], "REL")
    yield db
    db.close()


V8 = "0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5"
_FULLSET = f"SEARCH VECTOR [{V8}] TOP 100 AS seed WITH seed "


def _stage(tdb, stage):
    """执行单个 WITH 图算法阶段，返回 {node_id: row}。"""
    q = f"{_FULLSET}{stage} seed AS out_ RETURN out_"
    rows = tdb.tql(q)
    return {r.row["out_"]["id"]: r.row["out_"] for r in rows}


# ---------------------------------------------------------------------------
# leiden_cluster 方法
# ---------------------------------------------------------------------------
def test_leiden_cluster_small_graph(tdb):
    """小图手算对照：应把两连通分量 A={0..4}, B={5..8} 分出若干社区，
    且总节点覆盖全部 9 个。"""
    res = tdb.leiden_cluster(min_community_size=1)
    assert res["num_clusters"] >= 2
    communities = res["communities"]
    all_nodes = [n for comm in communities for n in comm]
    assert sorted(all_nodes) == list(range(1, 10))  # 节点 id 1..9
    # 每簇非空
    assert all(len(c) >= 1 for c in communities)


def test_leiden_min_community_size(tdb):
    """min_community_size 提升 → 小社区被并入/过滤，簇数量不增。"""
    small = tdb.leiden_cluster(min_community_size=1)
    large = tdb.leiden_cluster(min_community_size=4)
    assert large["num_clusters"] <= small["num_clusters"]


def test_leiden_compute_centroids_flag(tdb):
    with_c = tdb.leiden_cluster(compute_centroids=True)
    without = tdb.leiden_cluster(compute_centroids=False)
    assert "centroids" in with_c
    assert not without.get("centroids")


def test_leiden_max_iterations_bound(tdb):
    """max_iterations=15（默认）能产出社区；max_iterations=0 不迭代不崩溃
    （结果结构完整：communities/centroids/num_clusters 键均存在）。"""
    res15 = tdb.leiden_cluster(max_iterations=15, min_community_size=1)
    assert res15["num_clusters"] >= 1
    res0 = tdb.leiden_cluster(max_iterations=0, min_community_size=1)
    # 边界行为：0 次迭代也应收敛为合法结构，不抛异常
    assert set(res0.keys()) >= {"communities", "centroids", "num_clusters"}


# ---------------------------------------------------------------------------
# TQL WITH 图算法阶段
# ---------------------------------------------------------------------------
def test_pagerank_stage(tdb):
    res = _stage(tdb, "PAGERANK")
    assert len(res) == 9  # 每个节点都产出 pagerank 行


def test_wcc_stage(tdb):
    res = _stage(tdb, "WCC")
    assert len(res) == 9


def test_degree_stage(tdb):
    res = _stage(tdb, "DEGREE")
    assert len(res) == 9


def test_label_propagation_stage(tdb):
    res = _stage(tdb, "LABEL_PROPAGATION")
    assert len(res) == 9


def test_sa_ppr_stage(tdb):
    res = _stage(tdb, "SA_PPR")
    assert len(res) == 9


def test_all_stages_cover_all_nodes(tdb):
    """所有图算法阶段都应产出与全库节点数一致的覆盖。"""
    for stage in ["PAGERANK", "WCC", "DEGREE", "LABEL_PROPAGATION", "SA_PPR"]:
        res = _stage(tdb, stage)
        assert len(res) == tdb.node_count(), stage


# ---------------------------------------------------------------------------
# 参数边界
# ---------------------------------------------------------------------------
def test_betweenness_not_a_keyword(tdb):
    """BETWEENNESS 不是 0.8.3 的合法 TQL 阶段关键字，也不存在公开 Python
    方法 —— 记录为「能力缺失」而非崩溃。非法关键字必须报解析错误而非 panic。"""
    with pytest.raises(RuntimeError):
        tdb.tql(f"{_FULLSET}BETWEENNESS seed AS be RETURN be")


def test_graph_stage_undefined_alias_rejected(tdb):
    """引用未定义的图算法输出别名 → 应报错而非静默。"""
    with pytest.raises(RuntimeError):
        tdb.tql(f"{_FULLSET}PAGERANK seed AS pr RETURN nope")
