"""检索排序/权重两处治本的护栏（接续点排序见 test_task_state.py）。

覆盖：
  1. ``core.search.is_exact_identifier_query``：commit hash / PR 号 / 工单号命中，
     中文长句与普通英文词不命中（保守：判不了就走原权重）；
  2. ``_rrf_fuse`` 的数学下限：语义候选满员时，FTS-only 命中能否进 top_k 决定性
     地取决于 ``w_fts``——这是 ``RRF_FTS_EXACT_WEIGHT`` 必须取 ~1.0 的量化依据；
  3. 混合检索端到端：标识符 query 走抬高权重并在 ``channels`` 透出，
     普通自然语言 query 保持原权重（不动全局行为）。

隔离：本文件写入的节点在 teardown 全删（含 FTS 条目）——conftest 是 session 级
共享临时库，``test_smoke.test_fts_check`` 对「别人插的节点」非常敏感。
"""

from __future__ import annotations

import uuid

import pytest

from config import Config
from core.search import is_exact_identifier_query
from mcp_tools import store
from mcp_tools.memory import _rrf_fuse, mem_hybrid_search, mem_ingest


def _get(result):
    """MCP 工具在测试里可能返回 str(JSON) 或 dict，统一成 dict。"""
    import json

    return json.loads(result) if isinstance(result, str) else result


@pytest.fixture
def cleanup_nodes():
    """删掉本文件插入的节点 + 对应 FTS 条目，保证共享库回到原状。"""
    from core.fts_index import remove_node

    created: list[int] = []
    yield created
    for nid in created:
        try:
            store.delete_node(nid)
        finally:
            remove_node(nid)


# ---------------------------------------------------------------------------
# 1. 判定函数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query",
    [
        "d60240b",
        "fix d60240b boundary handling",
        "PR #131 merged",
        "见 WEB-1234",
        "ISSUE-45 出问题了",
        "abc1234 之后的行为",
    ],
)
def test_exact_identifier_positive(query):
    assert is_exact_identifier_query(query) is True, query


@pytest.mark.parametrize(
    "query",
    [
        "接续点排序应当按时间而不是权重",
        "混合检索的权重怎么调",
        # 全 hex 字母但无数字的英文词必须不命中（否则 defaced/decade 会被当 hash）
        "defaced decade facade",
        "hello world plugin",
        "",
        "   ",
    ],
)
def test_exact_identifier_negative(query):
    assert is_exact_identifier_query(query) is False, query


# ---------------------------------------------------------------------------
# 2. RRF 数学：FTS-only 命中能否进 top_k 由 w_fts 决定
# ---------------------------------------------------------------------------
def test_rrf_elevated_weight_promotes_fts_only_hit():
    """语义候选满员 + 目标只在 FTS 第 2 名时：0.1/0.5 都出局，1.0 才进。

    这是 RRF_FTS_EXACT_WEIGHT 默认取 1.0 的依据：语义第 0 名得 1/(60+0)≈0.0164，
    FTS 第 2 名得 w/(60+1)；w=0.5 时仅 0.0081，永远追不上。
    """
    sem_ids = list(range(1000, 1040))  # 40 条语义候选，全部排在前
    fts_ids = [9999, 8888, 7777]  # 目标 9999 在 FTS 第 1（0-based rank 0）

    def ranks(w_fts):
        fused = _rrf_fuse(sem_ids, fts_ids, 10, 60.0, w_sem=1.0, w_fts=w_fts)
        return [row[0] for row in fused]

    assert 9999 not in ranks(0.1), "原权重下 FTS-only 命中不该挤进 top10"
    assert 9999 not in ranks(0.5), "0.5 在数学上仍追不上语义头部"
    assert 9999 in ranks(1.0), "等权时精确命中应进入前列"


def test_rrf_default_weights_unchanged():
    """等权默认值行为不变：单侧命中按 1/(k+rank) 叠加。"""
    fused = _rrf_fuse([1, 2], [2, 3], 5, 60.0)
    ids = [row[0] for row in fused]
    assert ids[0] == 2, "双侧命中的节点应排第一"
    assert set(ids) == {1, 2, 3}


# ---------------------------------------------------------------------------
# 3. 端到端：通道权重随查询类型切换
# ---------------------------------------------------------------------------
def test_exact_query_uses_elevated_weight_and_hits(db_path, cleanup_nodes):
    """含 commit hash 的 query：走抬高权重、channels 透出、目标节点能命中。"""
    marker = uuid.uuid4().hex[:7]  # 7 位含数字的伪 hash，确保库里唯一
    assert is_exact_identifier_query(marker), marker
    r = _get(mem_ingest(content=f"边界处理记录 {marker} 与检索无关的随机说明", type="memory"))
    assert r["stored"] is True
    nid = r["node_id"]
    cleanup_nodes.append(nid)

    h = _get(mem_hybrid_search(marker, scope="memory", top_k=10))
    assert h["channels"]["exact_query"] is True, h.get("channels")
    assert h["channels"]["fts_weight"] == pytest.approx(Config.RRF_FTS_EXACT_WEIGHT)
    assert any(item["id"] == nid for item in h["results"]), f"精确 hash 未命中 {nid}: {h['results']}"


def test_natural_language_query_keeps_default_weight(db_path):
    """纯中文 query：不触发抬高，权重与全局默认一致（防止误伤所有检索）。"""
    h = _get(mem_hybrid_search("接续点排序应当按时间而不是权重", scope="memory", top_k=5))
    assert h["channels"]["exact_query"] is False, h.get("channels")
    assert h["channels"]["fts_weight"] == pytest.approx(Config.RRF_FTS_WEIGHT)
    assert pytest.approx(0.1) == Config.RRF_FTS_WEIGHT, "全局默认权重不该被本改动修改"
