# -*- coding: utf-8 -*-
"""
语义主序与图扩散解耦的回归测试
==============================
覆盖改动清单第 4 项：Config 默认值、_rrf_fuse 向后兼容与权重生效、
_Retrieval_EXPAND_DEPTH 透传到底层检索。
"""

# ---------------------------------------------------------------------------
# 1. Config 默认值断言
# ---------------------------------------------------------------------------
def test_config_defaults():
    from config import Config
    assert Config.RETRIEVAL_EXPAND_DEPTH == 0
    assert Config.RRF_SEM_WEIGHT == 1.0
    assert Config.RRF_FTS_WEIGHT == 0.1


# ---------------------------------------------------------------------------
# 2. _rrf_fuse 向后兼容：省略权重 == (1.0, 1.0)
# ---------------------------------------------------------------------------
def test_rrf_fuse_backward_compat():
    from mcp_tools.memory import _rrf_fuse
    sem = [10, 20, 30]
    fts = [20, 40]
    default = _rrf_fuse(sem, fts, top_k=10, k=60.0)
    explicit = _rrf_fuse(sem, fts, top_k=10, k=60.0, w_sem=1.0, w_fts=1.0)
    assert default == explicit, (
        "默认参数 (w_sem=1.0, w_fts=1.0) 必须与省略权重行为完全一致"
    )


# ---------------------------------------------------------------------------
# 3. _rrf_fuse 权重生效：w_fts=0 时结果按纯语义序，w_fts=1 时改变顺序
# ---------------------------------------------------------------------------
def test_rrf_fuse_weight_effect():
    from mcp_tools.memory import _rrf_fuse
    sem_ids = [1, 2, 3]
    fts_ids = [3, 2, 1]

    # w_fts=0 → 纯语义侧决定排序：1, 2, 3
    no_fts = _rrf_fuse(sem_ids, fts_ids, top_k=3, k=60.0, w_sem=1.0, w_fts=0.0)
    assert [r[0] for r in no_fts] == [1, 2, 3], (
        f"w_fts=0 应退化为纯语义序，实际: {[r[0] for r in no_fts]}"
    )

    # w_fts=1 → 双侧融合，节点 3 语义 rank2 + FTS rank0，
    # 节点 1 语义 rank0 + FTS rank2 → 融合后 3 超过 1
    both = _rrf_fuse(sem_ids, fts_ids, top_k=3, k=60.0, w_sem=1.0, w_fts=1.0)
    both_order = [r[0] for r in both]
    assert both_order != [1, 2, 3], (
        f"w_fts=1 应改变排序，实际仍为纯语义序: {both_order}"
    )
    # 节点 2 (rank1 双侧) 与节点 3 (sem rank2 + FTS rank0) 交换位置，
    # 实测顺序 [1, 3, 2]
    assert both_order == [1, 3, 2], (
        f"w_fts=1 实测顺序 [1, 3, 2]，实际: {both_order}"
    )


# ---------------------------------------------------------------------------
# 4. _hybrid_rrf 通过 RETRIEVAL_EXPAND_DEPTH 影响底层检索
# ---------------------------------------------------------------------------
def test_hybrid_rrf_expand_depth_config(monkeypatch):
    """monkeypatch store.search_similar 记录 kwargs，断言 expand_depth
    随 Config.RETRIEVAL_EXPAND_DEPTH 变化。"""
    from config import Config
    from mcp_tools._common import store

    captured = {}

    def _spy_search_similar(embedding, top_k=5, expand_depth=1,
                            apply_decay=True, block="", include_outdated=False,
                            **kw):
        captured["expand_depth"] = expand_depth
        # 返回空结果，避免依赖真实库
        return []

    monkeypatch.setattr(store, "search_similar", _spy_search_similar)

    # 也 monkeypatch embed_text 返回假向量
    monkeypatch.setattr(store, "embed_text", lambda q: [0.0] * 4)

    # monkeypatch search_fts 返回空
    from core.fts_index import search_fts as _real_fts
    monkeypatch.setattr("mcp_tools.memory.search_fts", lambda q, limit=10: [])

    # --- 场景 A：默认配置 expand_depth=0 ---
    monkeypatch.setattr(Config, "RETRIEVAL_EXPAND_DEPTH", 0)
    from mcp_tools.memory import _hybrid_rrf
    _hybrid_rrf("test query", "all", "", "", top_k=5, fts_limit=10,
                block="", include_outdated=False)
    assert captured.get("expand_depth") == 0, (
        f"默认配置下 expand_depth 应为 0，实际: {captured.get('expand_depth')}"
    )

    # --- 场景 B：手动设为 1 ---
    monkeypatch.setattr(Config, "RETRIEVAL_EXPAND_DEPTH", 1)
    _hybrid_rrf("test query", "all", "", "", top_k=5, fts_limit=10,
                block="", include_outdated=False)
    assert captured.get("expand_depth") == 1, (
        f"配置 RETRIEVAL_EXPAND_DEPTH=1 后 expand_depth 应为 1，实际: {captured.get('expand_depth')}"
    )
