"""tests for core/chunk_index.py (存储层 + 切分)。"""

import os
import sqlite3

import pytest

from core import chunk_index as ci


@pytest.fixture()
def chunk_db(tmp_path, monkeypatch):
    """每个测试用独立 DB 文件，天然隔离。"""
    db = os.path.join(str(tmp_path), "node_chunks_test.db")
    monkeypatch.setenv("CHUNK_DB_PATH", db)
    yield db


def _vec(prefix: str, dim: int = 8) -> list[float]:
    return [float(b) for b in prefix.encode("utf-8")] + [0.0] * max(0, dim - len(prefix))


def _unit(i: int, dim: int = 8) -> list[float]:
    v = [0.0] * dim
    v[i % dim] = 1.0
    return v


# ---------- split_chunks ----------


def test_split_short_text_single_chunk():
    text = "这是一段没有空行的短文本" * 10
    assert ci.split_chunks(text) == [text]


def test_split_by_empty_lines():
    text = "段落一开头甲" + "甲" * 205 + "\n\n段落二开头乙" + "乙" * 205 + "\n\n段落三开头丙" + "丙" * 205
    parts = ci.split_chunks(text)
    assert len(parts) == 3
    assert all(len(p) >= 200 for p in parts)
    assert parts[0].startswith("段落一")
    assert parts[1].startswith("段落二")
    assert parts[2].startswith("段落三")


def test_split_merges_short_paragraphs():
    text = "甲" * 100 + "\n\n" + "乙" * 100 + "\n\n" + "丙" * 250
    parts = ci.split_chunks(text)
    # 100+100 过短合并成一段（合并后 200 仍在阈值内），丙独立
    assert len(parts) == 2
    assert parts[0].startswith("甲")
    assert parts[1].startswith("丙")


def test_split_oversized_paragraph_sentence_split():
    text = ("这句话很长很重要。" * 100) + "\n\n" + "尾巴段。" * 60  # ~1100 + 300
    parts = ci.split_chunks(text)
    assert len(parts) >= 2
    assert all(len(p) <= 1500 + 20 for p in parts)  # 允许句尾多算一个字符
    # 重接短尾巴到上一段
    joined = "".join(parts)
    assert "尾巴段。" * 60 in joined


def test_split_deterministic():
    text = "段落一。\n\n段落二。\n\n段落三。" * 20
    assert ci.split_chunks(text) == ci.split_chunks(text)


# ---------- sync / stats ----------


def test_sync_stats_and_idempotent(chunk_db):
    chunks = [_vec("a-vec-0"), _vec("a-vec-1")]
    texts = ["第一段", "第二段"]
    n = ci.sync_node_chunks(1, chunks, texts, "modelA")
    assert n == 2
    stats = ci.chunk_stats()
    assert stats["total_chunks"] == 2
    assert stats["total_nodes"] == 1
    assert stats["by_model"] == {"modelA": 2}

    # 幂等：同 node 重复 sync 不产生重复行
    ci.sync_node_chunks(1, chunks, texts, "modelA")
    ci.sync_node_chunks(1, chunks, texts, "modelA")
    stats = ci.chunk_stats()
    assert stats["total_chunks"] == 2
    assert stats["total_nodes"] == 1


def test_sync_multi_node_counts(chunk_db):
    ci.sync_node_chunks(10, [_vec("x")], ["x 文本"], "m")
    ci.sync_node_chunks(20, [_vec("y"), _vec("z")], ["y 文本", "z 文本"], "m")
    stats = ci.chunk_stats()
    assert stats["total_chunks"] == 3
    assert stats["total_nodes"] == 2


# ---------- search ----------


def test_remove_then_search_empty(chunk_db):
    ci.sync_node_chunks(1, [_unit(0), _unit(1)], ["t1", "t2"], "modelA")
    assert ci.remove_node_chunks(1) == 2
    assert ci.search_chunks(_unit(0), 10, "modelA") == []
    assert ci.chunk_stats()["total_chunks"] == 0


def test_search_model_isolation(chunk_db):
    ci.sync_node_chunks(1, [_unit(0)], ["mA"], "modelA")
    ci.sync_node_chunks(2, [_unit(0)], ["mB"], "modelB")
    hits_a = ci.search_chunks(_unit(0), 10, "modelA")
    hits_b = ci.search_chunks(_unit(0), 10, "modelB")
    assert [(nid, idx) for nid, idx, _ in hits_a] == [(1, 0)]
    assert [(nid, idx) for nid, idx, _ in hits_b] == [(2, 0)]


def test_search_cos_desc_order_orthogonal(chunk_db):
    # e0 与 query 同向；e1 与 query 正交 → cos 顺序 0 > 1 > 2
    e0, e1 = _unit(0), _unit(1)
    e1_neg = [-1.0] + [0.0] * 7  # 负方向：cos = -1
    ci.sync_node_chunks(1, [e1_neg], ["neg"], "modelA")
    ci.sync_node_chunks(2, [e1], ["orth"], "modelA")
    ci.sync_node_chunks(3, [e0], ["same"], "modelA")
    hits = ci.search_chunks(_unit(0), 3, "modelA")
    order = [nid for nid, _, _ in hits]
    assert order == [3, 2, 1]
    assert hits[0][2] > hits[1][2] > hits[2][2]
    assert abs(hits[0][2] - 1.0) < 1e-6
    assert abs(hits[1][2]) < 1e-6


def test_search_top_m_limit(chunk_db):
    for i in range(5):
        ci.sync_node_chunks(i, [_unit(i)], [f"t{i}"], "modelA")
    hits = ci.search_chunks(_unit(0), 2, "modelA")
    assert len(hits) == 2


# ---------- 失败路径 ----------


def test_sync_length_mismatch_raises(chunk_db):
    with pytest.raises(ValueError, match="长度不一致"):
        ci.sync_node_chunks(1, [_vec("x")], ["a", "b"], "modelA")


def test_sync_empty_is_fine(chunk_db):
    assert ci.sync_node_chunks(1, [], [], "modelA") == 0
    assert ci.chunk_stats()["total_chunks"] == 0


def test_sync_failure_keeps_old_chunks_atomic_rollback(chunk_db):
    """缺陷 1 回归：INSERT 中途失败必须整事务回滚，旧 chunk 原样保留。

    旧代码在 ``BEGIN IMMEDIATE`` 事务内用 ``executescript`` 建表，sqlite3 的
    executescript 会先隐式提交 pending 事务 → 事务在建表那刻被吞掉，后面的
    DELETE + INSERT 处在自动提交模式，中途失败 rollback 失效，旧数据丢空。
    本测试钉死行为：注入一条会让 executemany 中途抛错的非法行（chunk_text
    传 None 违反 NOT NULL），断言旧数据仍在、没有半删半写。
    """
    ci.sync_node_chunks(1, [_unit(0), _unit(1)], ["旧段一", "旧段二"], "modelA")

    # 第 2 行 chunk_text=None → NOT NULL 约束在 executemany 中途抛 IntegrityError
    bad_texts = ["新段一", None]
    with pytest.raises(sqlite3.IntegrityError):
        ci.sync_node_chunks(1, [_unit(0), _unit(1)], bad_texts, "modelA")

    # 旧数据必须原样保留（两段都在），不能被先删后插的失败丢空
    stats = ci.chunk_stats()
    assert stats["total_chunks"] == 2
    conn = sqlite3.connect(os.environ["CHUNK_DB_PATH"])
    try:
        rows = conn.execute("SELECT chunk_text FROM node_chunks WHERE node_id = 1 ORDER BY chunk_idx").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["旧段一", "旧段二"]


def test_search_dim_mismatch_row_skipped(chunk_db):
    """缺陷 2 回归：dim 与 query 不同的 chunk 行必须被跳过（不返回、不抛异常）。

    旧代码 zip(q, v, strict=False) 在维度不同时静默按短的截断，算出无意义的
    余弦值——换 embedding 模型/维度后的脏行就是靠这个混进结果的。
    """
    ci.sync_node_chunks(1, [_unit(0, dim=8)], ["dim8 行"], "modelA")
    ci.sync_node_chunks(2, [[0.0, 0.0, 1.0, 0.0, 0.0]], ["dim5 行"], "modelA")
    hits = ci.search_chunks(_unit(0, dim=8), 10, "modelA")
    returned = [(nid, idx) for nid, idx, _ in hits]
    assert (2, 0) not in returned
    assert (1, 0) in returned
    # 维度分布进入 stats（便于发现脏行）
    stats = ci.chunk_stats()
    assert stats["by_dim"] == {"8": 1, "5": 1}
