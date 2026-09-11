"""
tests/test_search_candidates.py —— 候选层 outdated 过滤测试

覆盖：
  a) status="outdated" 的节点不出现在 search_similar 候选结果
  b) 无 status 字段 / status="active" 的节点正常保留
  c) 过滤后候选量仍足够（候选扩容生效，不会因过滤把候选掏空）

隔离保证：不真连 Ollama——复用 test_rerank_mode 的 _FakeHit / _FakeDB 注入可控分数。
"""

import pytest

from core.trivium_store import TriviumStore


class _FakeHit:
    """模拟 search_advanced 的 hit。"""

    def __init__(self, nid: int, score: float, payload: dict):
        self.id = nid
        self.score = score
        self.payload = payload


class _FakeDB:
    """最小假库：透传测试给定 hits，get 返回 None。"""

    def __init__(self, hits):
        self._hits = list(hits)

    def search_advanced(self, query_embedding, **kwargs):
        return list(self._hits)

    def get(self, nid):
        return None

    def close(self):
        pass


@pytest.fixture
def _candidate_store(monkeypatch):
    """构造 TriviumStore 实例，_acquire 换成返回可控 _FakeDB。"""

    def _make(hits):
        s = TriviumStore()
        fake = _FakeDB(hits)
        monkeypatch.setattr(s, "_acquire", lambda: fake)
        return s

    return _make


def _run(store, top_k=5):
    """调用 search_similar，返回 (results_by_id, result_ids)。"""
    res = store.search_similar(
        [0.0] * 4, top_k=top_k, expand_depth=1, apply_decay=False,
    )
    return {r["id"]: r["score"] for r in res}, [r["id"] for r in res]


# ---------------------------------------------------------------------------
# a) status="outdated" 的节点不出现
# ---------------------------------------------------------------------------

def test_outdated_nodes_excluded(_candidate_store):
    hits = [
        _FakeHit(1, 0.90, {"type": "memory", "status": "active"}),
        _FakeHit(2, 0.85, {"type": "memory", "status": "outdated"}),
        _FakeHit(3, 0.80, {"type": "memory", "status": "active"}),
        _FakeHit(4, 0.75, {"type": "memory", "status": "outdated"}),
        _FakeHit(5, 0.70, {"type": "memory", "status": "active"}),
    ]
    store = _candidate_store(hits)
    by_id, order = _run(store, top_k=3)
    assert 2 not in by_id, "status=outdated 节点 2 不应出现"
    assert 4 not in by_id, "status=outdated 节点 4 不应出现"
    assert order == [1, 3, 5], f"有效节点应按 score 排序: {order}"


# ---------------------------------------------------------------------------
# b) 无 status 字段 / status="active" 正常保留
# ---------------------------------------------------------------------------

def test_active_and_missing_status_kept(_candidate_store):
    hits = [
        _FakeHit(1, 0.90, {"type": "memory", "status": "active"}),
        _FakeHit(2, 0.85, {"type": "memory"}),  # 无 status 字段
        _FakeHit(3, 0.80, {"type": "memory", "status": "active"}),
    ]
    store = _candidate_store(hits)
    by_id, order = _run(store, top_k=3)
    assert 1 in by_id and 2 in by_id and 3 in by_id
    assert order == [1, 2, 3]


# ---------------------------------------------------------------------------
# c) 过滤后候选量仍足够（扩容生效，不因过滤把候选掏空）
# ---------------------------------------------------------------------------

def test_filtering_leaves_enough_candidates(_candidate_store):
    """模拟 24% outdated 比例：100 个候选中 24 个 outdated，top_k=10 仍可填满。"""
    hits = []
    for i in range(1, 101):
        status = "outdated" if i % 4 == 0 else "active"  # 每第 4 个 outdated → 25%
        hits.append(_FakeHit(i, 1.0 - i * 0.005, {"type": "memory", "status": status}))
    store = _candidate_store(hits)
    by_id, order = _run(store, top_k=10)
    # 应返回恰好 10 条，且无 outdated
    assert len(order) == 10, f"应返回 10 条有效候选，实际 {len(order)}"
    assert all(r not in [4, 8, 12, 16, 20, 24, 28, 32, 36, 40] for r in order), \
        "不应含 outdated 节点"


def test_cand_k_expansion_is_6x(_candidate_store):
    """确认 cand_k = top_k * 6，而非旧的 top_k * 3。"""
    # 构造 60 个候选，其中 12 个 outdated（20%），top_k=10 需要过滤后仍有 ≥10 个有效
    hits = []
    for i in range(1, 61):
        status = "outdated" if i % 5 == 0 else "active"
        hits.append(_FakeHit(i, 1.0 - i * 0.005, {"type": "memory", "status": status}))
    store = _candidate_store(hits)
    by_id, order = _run(store, top_k=10)
    assert len(order) == 10, f"扩容 6x 后应有足够有效候选，实际 {len(order)}"
    # 旧的 3x 扩容：30 个候选 → 过滤掉 6 个 outdated → 24 个有效，仍够
    # 但 6x 更保险，确保即使 24% outdated 也填满
