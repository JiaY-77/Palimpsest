"""
tests/test_rerank_mode.py —— 检索排序元数据加权测试（soft ε 软加权 vs hard 乘性硬加权）

覆盖：
  a) soft：语义分差 > 2ε 时，元数据（importance / 新鲜度）不得改变相对顺序
  b) soft：语义分差 < ε 时，importance 更高 / 更新的节点排在前面
  c) hard：结果与旧乘性公式一致（回归保护，一键回退路径）
  d) kb_chunk 在 soft 下获得固定 +ε×KB_SOFT_RERANK_MULT，且不随时间变化
  e) apply_decay=False 时不加任何项

隔离保证：不真连 Ollama——注入假 search_advanced 返回可控分数与 payload，
无真实图/向量计算；Config 相关项用 monkeypatch 隔离，不动 .env / 正式库。
"""

import time

import pytest

from config import Config
from core.trivium_store import TriviumStore

# ---------------------------------------------------------------------------
# 假组件：可控 search_advanced 返回（替代真实向量检索）
# ---------------------------------------------------------------------------

class _FakeHit:
    """模拟 search_advanced 的 hit：score 由测试精确控制。"""

    def __init__(self, nid: int, score: float, payload: dict):
        self.id = nid
        self.score = score
        self.payload = payload


class _FakeDB:
    """最小假库：只实现 search_similar 用到的 search_advanced / get / close。

    search_advanced 透传测试给定 hits；get 返回 None 使命中计数 best-effort 静默跳过。
    """

    def __init__(self, hits):
        self._hits = list(hits)

    def search_advanced(self, query_embedding, **kwargs):
        return list(self._hits)

    def get(self, nid):
        return None

    def close(self):
        pass


@pytest.fixture
def _rerank_store(monkeypatch):
    """构造 TriviumStore 实例，_acquire 换成返回可控 _FakeDB。"""

    def _make(hits):
        s = TriviumStore()
        fake = _FakeDB(hits)
        monkeypatch.setattr(s, "_acquire", lambda: fake)
        return s

    return _make


def _mem_payload(importance: float, created_at: float, type_: str = "memory") -> dict:
    return {"type": type_, "importance": importance, "created_at": created_at}


def _run(store, top_k=3, apply_decay=True):
    res = store.search_similar([0.0] * 4, top_k=top_k, expand_depth=1,
                               apply_decay=apply_decay)
    return {r["id"]: r["score"] for r in res}, [r["id"] for r in res]


# ---------------------------------------------------------------------------
# a) soft：语义分差 > 2ε → 元数据不得翻转顺序
# ---------------------------------------------------------------------------

def test_soft_metadata_cannot_flip_large_semantic_gap(monkeypatch, _rerank_store):
    eps = 0.02
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    monkeypatch.setattr(Config, "SOFT_RERANK_EPS", eps)
    now = time.time()
    hits = [
        _FakeHit(1, 0.80, _mem_payload(0.0, now)),  # 语义更高但元数据最差（importance 0）
        _FakeHit(2, 0.75, _mem_payload(1.0, now)),  # 语义更低但元数据最好（importance 1）
    ]
    store = _rerank_store(hits)
    by_id, order = _run(store, top_k=2)
    # 等新鲜度下 importance 极值 swing 恰为 2ε=0.04，分差 0.05 > 2ε → 顺序不得翻转
    assert order == [1, 2], f"大分差下元数据不得翻转顺序: {order}"
    assert by_id[1] > by_id[2]
    # 且两处 final 均与公式一致（语义分为主线）
    assert by_id[1] == pytest.approx(0.80 + eps * (-1.0 + 1.0))
    assert by_id[2] == pytest.approx(0.75 + eps * (1.0 + 1.0))


def test_soft_large_gap_recency_biased_against_semantic(monkeypatch, _rerank_store):
    """语义分差 > 2ε 时，新鲜度偏向低语义节点也不得翻转（recency swing ≤ 2ε）。"""
    eps = 0.02
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    monkeypatch.setattr(Config, "SOFT_RERANK_EPS", eps)
    now = time.time()
    hits = [
        _FakeHit(1, 0.90, _mem_payload(1.0, now - 600 * 86400)),  # 语义高但很老
        _FakeHit(2, 0.85, _mem_payload(1.0, now)),                # 语义低但很新，同 importance
    ]
    store = _rerank_store(hits)
    _, order = _run(store, top_k=2)
    assert order == [1, 2], f"分差 0.05 > 2ε 不应被新鲜度翻转: {order}"


# ---------------------------------------------------------------------------
# b) soft：语义分差 < ε → 元数据决定顺序
# ---------------------------------------------------------------------------

def test_soft_higher_importance_wins_on_tie(monkeypatch, _rerank_store):
    eps = 0.02
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    monkeypatch.setattr(Config, "SOFT_RERANK_EPS", eps)
    now = time.time()
    hits = [
        _FakeHit(1, 0.80, _mem_payload(1.0, now)),   # importance 高
        _FakeHit(2, 0.80, _mem_payload(0.0, now)),   # importance 低，语义分相同
    ]
    store = _rerank_store(hits)
    _, order = _run(store, top_k=2)
    assert order == [1, 2], f"同分时 importance 更高者应在前: {order}"


def test_soft_newer_node_wins_on_tie(monkeypatch, _rerank_store):
    eps = 0.02
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    monkeypatch.setattr(Config, "SOFT_RERANK_EPS", eps)
    now = time.time()
    hits = [
        _FakeHit(1, 0.80, _mem_payload(1.0, now - 500 * 86400)),  # 老
        _FakeHit(2, 0.80, _mem_payload(1.0, now)),                # 新，语义分相同
    ]
    store = _rerank_store(hits)
    _, order = _run(store, top_k=2)
    assert order == [2, 1], f"同分时更新的节点应在前: {order}"


def test_soft_sub_eps_gap_metadata_can_dominate(monkeypatch, _rerank_store):
    eps = 0.02
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    monkeypatch.setattr(Config, "SOFT_RERANK_EPS", eps)
    now = time.time()
    hits = [
        _FakeHit(1, 0.800, _mem_payload(1.0, now)),                  # 语义略低但元数据强
        _FakeHit(2, 0.801, _mem_payload(0.0, now - 500 * 86400)),    # 语义略高但元数据弱
    ]
    store = _rerank_store(hits)
    _, order = _run(store, top_k=2)
    # 分差 0.001 < ε：元数据把弱语义但高 importance+更新的节点顶到前面
    assert order == [1, 2], f"分差 < ε 时元数据应 tie-break: {order}"


# ---------------------------------------------------------------------------
# c) hard：与旧乘性公式逐位一致（回归保护）
# ---------------------------------------------------------------------------

def test_hard_mode_matches_old_multiplicative_formula(monkeypatch, _rerank_store):
    decay = 0.9
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "hard")
    monkeypatch.setattr(Config, "MEMORY_DECAY_FACTOR", decay)
    now = time.time()
    old_ts = now - 300 * 86400  # 300 天前
    hits = [
        _FakeHit(1, 0.80, _mem_payload(1.0, now)),
        _FakeHit(2, 0.75, _mem_payload(0.5, old_ts)),
        _FakeHit(3, 0.60, _mem_payload(0.0, old_ts, type_="kb_chunk")),
    ]
    store = _rerank_store(hits)
    by_id, _ = _run(store, top_k=3)
    expected = {
        1: 0.80 * 1.0 * (decay ** 0),
        2: 0.75 * 0.5 * (decay ** (300 / 30.0)),
        3: 0.60,  # kb_chunk 整条跳过：不乘 importance 不乘衰减
    }
    for nid, exp in expected.items():
        assert by_id[nid] == pytest.approx(exp), (nid, by_id[nid], exp)


# ---------------------------------------------------------------------------
# d) soft：kb_chunk 固定加成、不随时间变化
# ---------------------------------------------------------------------------

def test_soft_kb_chunk_fixed_bonus_time_invariant(monkeypatch, _rerank_store):
    eps = 0.02
    kb_mult = 1.5
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    monkeypatch.setattr(Config, "SOFT_RERANK_EPS", eps)
    monkeypatch.setattr(Config, "KB_SOFT_RERANK_MULT", kb_mult)
    now = time.time()
    hits = [
        _FakeHit(1, 0.80, _mem_payload(0.0, now, type_="kb_chunk")),
        _FakeHit(2, 0.80, _mem_payload(1.0, now - 500 * 86400, type_="kb_chunk")),
    ]
    store = _rerank_store(hits)
    by_id, _ = _run(store, top_k=2)
    assert by_id[1] == pytest.approx(0.80 + eps * kb_mult)
    assert by_id[2] == pytest.approx(0.80 + eps * kb_mult)
    assert by_id[1] == by_id[2], "kb_chunk 加成应不随 created_at 变化"


# ---------------------------------------------------------------------------
# e) apply_decay=False：不加任何元数据项
# ---------------------------------------------------------------------------

def test_apply_decay_false_adds_nothing(monkeypatch, _rerank_store):
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    now = time.time()
    hits = [
        _FakeHit(1, 0.80, _mem_payload(1.0, now)),
        _FakeHit(2, 0.75, _mem_payload(0.0, now - 500 * 86400)),
    ]
    store = _rerank_store(hits)
    by_id, order = _run(store, top_k=2, apply_decay=False)
    assert by_id[1] == pytest.approx(0.80)
    assert by_id[2] == pytest.approx(0.75)
    assert order == [1, 2], "apply_decay=False 时语义分原样保留"


def test_apply_decay_false_ignores_kb_bonus(monkeypatch, _rerank_store):
    monkeypatch.setattr(Config, "MEMORY_RERANK_MODE", "soft")
    now = time.time()
    hits = [
        _FakeHit(1, 0.80, _mem_payload(1.0, now, type_="kb_chunk")),
        _FakeHit(2, 0.75, _mem_payload(1.0, now)),
    ]
    store = _rerank_store(hits)
    by_id, _ = _run(store, top_k=2, apply_decay=False)
    assert by_id[1] == pytest.approx(0.80)
    assert by_id[2] == pytest.approx(0.75)