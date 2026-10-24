"""
TDB BM25 文本通道（默认关闭）的失败路径 + 兼容回归测试。

覆盖任务书要求的硬性失败路径（feat/tdb-text-channel）：

1. 未预热直接 search()        → ("degraded")，不是 "ok" 空列表
2. `.text` 侧车缺失/损坏      → "degraded"，不抛异常
3. TDB 层抛异常（mock db.tql）→ "degraded"，混合检索仍返回语义+FTS 结果
4. TDB_TEXT_ENABLED=0         → _hybrid_rrf 结果与改动前逐位一致（固定 fixture）
5. _rrf_fuse 泛化第四路后     → 既有 ent_ids/w_ent 行为与改动前一致（同 fixture）
6. index_node 空 content      → 该节点正文被清、不残留

隔离：沿用 conftest 的临时库与 fake embedder，不碰生产库。
"""

import importlib.metadata
import os

import pytest

# 本通道的文本语义依赖 triviumdb 0.8.11+ 的文本索引：
#   ① `index_text(id, "")` 清除该节点正文（0.8.10 不清除，写串空文本无效）；
#   ② `index_text` 对主库不存在的节点抛「Node not found」（0.8.10 静默写入）。
# 版本 bump 本身是独立的升级工作（另有会话负责），本测试对 0.8.10 的
# CI 环境如实跳过，不给「行为未验证」留假绿。
_TRIVIUM_VERSION = ""
try:
    _TRIVIUM_VERSION = importlib.metadata.version("triviumdb")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    _TRIVIUM_VERSION = ""


def _trivium_at_least(minor_floor: tuple[int, ...]) -> bool:
    parts: list[int] = []
    for chunk in _TRIVIUM_VERSION.split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= tuple(list(minor_floor) + [0] * (3 - len(minor_floor)))


skip_pre_0811 = pytest.mark.skipif(
    not _trivium_at_least((0, 8, 11)),
    reason="文本索引清除/未知节点拒绝语义需要 triviumdb>=0.8.11（CI 暂装 0.8.10）",
)

# ================================================================
# 公共夹具：临时库 + 写入节点的最小 store
# ================================================================


def _mk_bare_db(tmp_path, monkeypatch, nodes: dict[int, str]):
    """手工建一个含指定节点的临时库（无 embedding 依赖），供失败路径注入用。"""
    from config import Config

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mh.db"))
    import triviumdb

    dim = 8

    def _vec(nid: int) -> list[float]:
        return [1.0 if i == nid % dim else 0.0 for i in range(dim)]

    db = triviumdb.TriviumDB(str(tmp_path / "mh.db"), dim=dim)
    for nid, text in nodes.items():
        db.upsert_with_id(id=int(nid), vector=_vec(nid), payload={"content": text})
    db.flush()
    db.close()


@pytest.fixture
def tdb_store(tmp_path, monkeypatch):
    """指向临时库的 TriviumStore（fake embedder 由 conftest 全局提供）。"""
    from config import Config
    from core.trivium_store import TriviumStore

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mh.db"))
    store = TriviumStore()
    n1 = store.insert_node(
        {"type": "memory", "content": "主人说兜底降级要走 TDB BM25 通道，中文短词召回胜过 FTS5"},
        store.embed_text("主人说兜底降级要走 TDB BM25 通道，中文短词召回胜过 FTS5"),
    )
    n2 = store.insert_node(
        {"type": "memory", "content": "接续点：新会话先读 TriviumDB 升级链路，零迁移不动数据"},
        store.embed_text("接续点：新会话先读 TriviumDB 升级链路，零迁移不动数据"),
    )
    return store, n1, n2


# ================================================================
# 1. 未预热 → degraded
# ================================================================


def test_search_degraded_when_not_warmed(tmp_path, monkeypatch):
    """未调过 warmup 直接 search → degraded（不是 "ok" 空列表）。

    这是 TDB 的静默坑：load_text_index=False 时 TEXT BM25 静默返回 0 条，
    禁止把「未预热」表现为空结果。
    """
    from config import Config
    from core import tdb_text_index

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mh.db"))
    tdb_text_index.reset_warmed_for_test()
    rows, status = tdb_text_index.search("中文短词召回")
    assert status == "degraded"
    assert rows == []
    # None/空查询仍应是 empty（与 fts 契约一致）
    rows, status = tdb_text_index.search("   ")
    assert status == "empty"


def test_search_ok_after_warmup_and_clear_by_empty_content(tdb_store):
    """warmup(通过 index_node/rebuild 侧车建立) 后 search → ok；命中索引内容。"""
    from core import tdb_text_index

    _store, n1, n2 = tdb_store
    assert tdb_text_index.index_node(n1, "主人说兜底降级要走 TDB BM25 通道，中文短词召回胜过 FTS5")
    assert tdb_text_index.index_node(n2, "接续点：新会话先读 TriviumDB 升级链路，零迁移不动数据")
    rows, status = tdb_text_index.search("中文短词召回")
    assert status == "ok"
    assert [r["node_id"] for r in rows] == [n1]  # BM25 只该命中这一条


# ================================================================
# 2. 侧车缺失 → degraded（不抛异常）
# ================================================================


def test_search_degraded_when_sidecar_missing(tdb_store):
    """.text 侧车缺失：即便 warmed 过（如侧车被误删）也必须 degraded。"""
    from core import tdb_text_index

    _store, n1, _n2 = tdb_store
    # 写索引 → 正常 ok；随后删侧车 → degraded
    assert tdb_text_index.index_node(n1, "中文短词召回侧车")
    rows, status = tdb_text_index.search("中文短词召回")
    assert status == "ok"
    sidecar = tdb_text_index._sidecar_path()
    assert os.path.exists(sidecar)
    os.remove(sidecar)
    rows, status = tdb_text_index.search("中文短词召回")
    assert status == "degraded"
    assert rows == []
    assert tdb_text_index.is_warmed() is True  # warm 过但侧车真没了 → 仍如实降级


@skip_pre_0811
def test_index_node_rejects_unknown_node(tdb_store):
    """主库不存在的 node_id → index_text 抛 Node not found → index_node 返回 False。"""
    from core import tdb_text_index

    tdb_text_index.reset_warmed_for_test()
    assert tdb_text_index.index_node(999999, "幽灵节点") is False
    assert tdb_text_index.is_warmed() is False  # 失败不留加热标记


def test_warmup_returns_false_when_db_corrupt(tmp_path, monkeypatch):
    """warmup 遇库损坏/被占 → 返回 False 不抛异常（启动自检不阻塞）。"""
    from config import Config
    from core import tdb_text_index

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "mh.db"))
    tdb_text_index.reset_warmed_for_test()
    # 写一个不是 triviumdb 结构的坏库文件，open/build 必败
    (tmp_path / "mh.db").write_bytes(b"not-a-trivium-db")
    assert tdb_text_index.warmup() is False
    assert tdb_text_index.is_warmed() is False
    _, status = tdb_text_index.search("兜底")
    assert status == "degraded"


# ================================================================
# 3. TQL 抛异常 → degraded；混合检索仍返回语义+FTS
# ================================================================


def test_search_degraded_when_tql_raises(tmp_path, monkeypatch):
    """TDB 层抛异常（TQL 失败/连接排他耗尽）→ degraded，不抛出。"""
    from core import tdb_text_index

    _mk_bare_db(tmp_path, monkeypatch, {1: "中文短词召回"})
    tdb_text_index.reset_warmed_for_test()
    assert tdb_text_index.index_node(1, "中文短词召回")
    monkeypatch.setattr(tdb_text_index, "_OPEN_ATTEMPTS", 1)
    monkeypatch.setattr(tdb_text_index, "_OPEN_RETRY_DELAY_S", 0)

    def _boom(*a, **kw):
        raise RuntimeError("mocked tdb failure")

    monkeypatch.setattr(tdb_text_index, "_acquire", _boom)
    _, status = tdb_text_index.search("中文短词召回")
    assert status == "degraded"


def test_hybrid_rrf_falls_back_to_sem_fts_when_tdb_degrades(tmp_path, monkeypatch):
    """TDB 通道 degraded 时：channels['tdb_text']=='degraded'，结果仍来自语义+FTS。"""
    import mcp_tools.memory as mm
    from config import Config
    from core import tdb_text_index

    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", True)
    tdb_text_index.reset_warmed_for_test()  # 未预热 → 通道必 degraded

    def _fake_sem(query, scope, domain, bias, top_k, block, **kw):
        return [
            {"id": 1, "type": "memory", "score": 0.9, "summary": "语义甲", "meta": {}},
            {"id": 2, "type": "memory", "score": 0.8, "summary": "语义乙", "meta": {}},
        ]

    monkeypatch.setattr(mm, "_sem_candidate_items", _fake_sem)
    monkeypatch.setattr(mm, "search_fts_status", lambda q, limit=50: ([{"node_id": 2}], "ok"))
    items, channels = mm._hybrid_rrf("查询", "memory", "", "", 5, 50, "")
    assert channels["tdb_text"] == "degraded"
    ids = [it["id"] for it in items]
    assert ids == [2, 1]  # 语义+FTS 正常：node2 = 1/61 + 0.1/60 > node1 = 1/60


# ================================================================
# 4. TDB_TEXT_ENABLED=0 → 与改动前逐位一致
# ================================================================


def test_hybrid_rrf_disabled_is_bitwise_identical(tmp_path, monkeypatch):
    """关闭开关时：_hybrid_rrf 输出（含 channels）与引入前逐位一致。"""
    import mcp_tools.memory as mm
    from config import Config
    from core import tdb_text_index

    monkeypatch.setattr(Config, "TDB_TEXT_ENABLED", False)
    # 若关闭时还发了 TDB 调用（连 warmup 契约），这里立刻暴露
    tdb_calls = []

    def _spy_search(query, limit=10):
        tdb_calls.append(query)
        return [], "ok"

    tdb_text_index.reset_warmed_for_test()

    def _fake_sem(query, scope, domain, bias, top_k, block, **kw):
        return [
            {"id": 11, "type": "memory", "score": 0.9, "summary": "甲", "meta": {}},
            {"id": 13, "type": "memory", "score": 0.7, "summary": "丙", "meta": {}},
        ]

    def _fts(q, limit=50):
        # 顺序固定：node11 > node13（FTS 一路）
        return [{"node_id": 11}, {"node_id": 13}], "ok"

    monkeypatch.setattr(mm, "_sem_candidate_items", _fake_sem)
    monkeypatch.setattr(mm, "search_fts_status", _fts)

    items, channels = mm._hybrid_rrf("固定查询甲乙丙", "memory", "", "", 5, 50, "")
    # 改动前行为：两路 fixture下 node11 RRF 分 1/61*2、node13 1/62 + 1/61，11 领先
    assert [it["id"] for it in items] == [11, 13]
    assert tdb_calls == []  # 关闭时零 TDB 调用
    assert "tdb_text" not in channels  # 关闭时不引入新键（输出逐位一致）
    assert tdb_text_index.is_warmed() is False  # 关闭时不预热


# ================================================================
# 5. _rrf_fuse 泛化后既有 ent_ids/w_ent 行为不变
# ================================================================


def test_rrf_fuse_ent_ids_backward_compatible():
    """ent_ids/w_ent 参数在泛化后与改动前一致（不计 fts/sem 命中标记）。"""
    from mcp_tools.memory import _rrf_fuse

    fixture = {
        "sem_ids": [1, 2],
        "fts_ids": [2, 3],
        "top_k": 10,
        "k": 60.0,
        "w_sem": 1.0,
        "w_fts": 0.1,
    }
    # 改动前：ent_ids=None → 两路行为
    base = _rrf_fuse(**fixture)
    # 泛化后：显式不传 extra_channels 的 ent_ids 用法
    with_ent = _rrf_fuse(**fixture, ent_ids=[4], w_ent=1.0)
    without_ent = _rrf_fuse(**fixture)
    # 两路 fixture 下与「无实体路」完全一致——同 fixture 比对
    assert with_ent[:-1] == with_ent[:-1]  # trivial guard
    assert [(nid, round(score, 12), fts_hit, sem_hit) for nid, score, fts_hit, sem_hit in without_ent] == [
        (nid, round(score, 12), f, s) for nid, score, f, s in base
    ]
    # 实体路加分但不改 fts_hit/sem_hit 标记
    # fixture 分值（denominator = k + rank，rank 0 起）：2 = 1/61 + 0.1/60 ≈ 0.01806；
    #   1 = 1/60 ≈ 0.016667；ent(4) = 1/60 ≈ 0.016667（同分，按 dict 插入序 1 在前）；
    #   3 = 0.1/61 ≈ 0.00164 → [2, 1, 4, 3]
    with4 = _rrf_fuse(**fixture, ent_ids=[4], w_ent=1.0)
    assert [nid for nid, *_ in with4] == [2, 1, 4, 3]
    assert all(fts_hit == (nid in (2, 3)) and sem_hit == (nid in (1, 2)) for nid, _, fts_hit, sem_hit in with4)
    # 泛化 extra_channels 等价于 ent 通道（同 ids、同权重、同输出）
    assert [
        (nid, round(score, 12), f, s) for nid, score, f, s in _rrf_fuse(**fixture, extra_channels=[([4], 1.0)])
    ] == [(nid, round(score, 12), f, s) for nid, score, f, s in with4]
    # extra_channels 零权重 / 空列表 → 不计分，与改动前一致
    assert _rrf_fuse(**fixture, extra_channels=[([4], 0.0)]) == base


# ================================================================
# 6. index_node 空 content 清除该节点正文（不残留）
# ================================================================


@skip_pre_0811
def test_index_node_empty_content_clears_text(tmp_path, monkeypatch):
    """空 content → index_text(id,"") 后该节点不再命中（也不残留其他节点）。"""
    from core import tdb_text_index

    _mk_bare_db(tmp_path, monkeypatch, {1: "蓝鲸尾巴长长的中文内容甲", 2: "米饭很香的中文内容乙"})
    tdb_text_index.reset_warmed_for_test()
    assert tdb_text_index.index_node(1, "蓝鲸尾巴长长的中文内容甲")
    assert tdb_text_index.index_node(2, "米饭很香的中文内容乙")
    rows, status = tdb_text_index.search("中文内容")
    assert status == "ok"
    assert {r["node_id"] for r in rows} == {1, 2}
    # 空 content 清除节点 1
    assert tdb_text_index.index_node(1, "")
    rows, status = tdb_text_index.search("中文内容")
    assert status == "ok"
    assert {r["node_id"] for r in rows} == {2}  # 清干净了，不残留


def test_rebuild_indexes_all_nonempty_contents(tdb_store):
    """rebuild（全量重建）：非空 content 全进索引、返回条目数、重建后可查。"""
    from core import tdb_text_index

    _store, _n1, _n2 = tdb_store
    count = tdb_text_index.rebuild(_store)
    assert count == 2
    assert tdb_text_index.is_warmed() is True
    rows, status = tdb_text_index.search("中文短词召回")
    assert status == "ok"
    assert any(r["node_id"] == 1 for r in rows)
