"""
FTS 长查询 3-gram OR 改写测试
=============================
覆盖 build_fts_query 纯函数单测 + search_fts 长/短查询集成测试。
"""

import sqlite3

import pytest

from core.fts_index import build_fts_query, index_node, search_fts


class TestBuildFtsQuery:
    """build_fts_query 纯函数单测"""

    def test_long_sentence_produces_multiple_grams(self):
        q = "之前压测用的那些脚本都放在哪了"
        result = build_fts_query(q)
        assert result, "应产出非空 OR 查询"
        grams = result.split(" OR ")
        assert len(grams) > 1, f"应有多个 3-gram，实际: {result}"

    def test_gram_count_limit(self):
        q = "这是一句非常非常非常非常非常非常非常非常非常非常非常非常长的测试句子用来验证片段上限"
        result = build_fts_query(q)
        grams = result.split(" OR ")
        assert len(grams) <= 12, f"片段数应 <= 12，实际: {len(grams)}"

    def test_tail_coverage(self):
        """均匀采样应覆盖句尾：最后 3 字应出现在某个 gram 中"""
        q = "之前压测用的那些脚本都放在哪了"
        result = build_fts_query(q)
        tail = q[-3:]
        assert tail in result, f"句尾 '{tail}' 应出现在结果中: {result}"

    def test_punctuation_splits(self):
        q = "你好啊，世界呀。测试哦；分词来"
        result = build_fts_query(q)
        grams = [g for g in result.split(" OR ") if g]
        # 逗号句号分号两侧各成段（每段 >=3 字符才保留）
        assert len(grams) >= 4, f"标点切分后应有 >=4 个 gram，实际: {result}"

    def test_short_string_returns_empty(self):
        assert build_fts_query("ab") == ""
        assert build_fts_query("a") == ""

    def test_empty_returns_empty(self):
        assert build_fts_query("") == ""

    def test_none_like_empty(self):
        assert build_fts_query(None) == ""

    def test_all_short_parts_returns_empty(self):
        assert build_fts_query("你 好 a") == ""


@pytest.fixture
def fts_env(monkeypatch, tmp_path):
    """指向临时 fts.db 并索引若干中文节点。

    Config.DB_PATH 在 import 时定型，monkeypatch 环境变量无效；
    直接 patch core.fts_index._connect 让 search_fts 走临时库。
    """
    import core.fts_index as fts_mod

    db_path = str(tmp_path / "fts.db")

    def make_conn():
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
            "content, node_id UNINDEXED, source_path UNINDEXED, "
            "tokenize='trigram')"
        )
        conn.commit()
        return conn

    monkeypatch.setattr(fts_mod, "_connect", make_conn)

    docs = [
        (1, "之前压测用的那些脚本都放在哪了"),
        (2, "Python 的单元测试框架 pytest 入门"),
        (3, "数据库索引优化策略与实践"),
        (4, "Q-041 索引碎片清理与重建的完整流程"),
    ]
    for nid, content in docs:
        index_node(nid, content, f"doc_{nid}")

    return fts_mod


class TestSearchFtsIntegration:
    """集成测试：临时 FTS 库上验证长/短查询行为"""

    def test_long_query_hits(self, fts_env):
        """长自然语言查询应命中（现状整句 MATCH 会 miss）"""
        results = search_fts("之前压测用的那些脚本都放在哪了")
        node_ids = [r["node_id"] for r in results]
        assert 1 in node_ids, f"应命中 node 1，实际: {node_ids}"

    def test_short_query_exact_match(self, fts_env):
        """短查询 Q-041 之类仍走整句精确匹配路径"""
        results = search_fts("Q-041")
        node_ids = [r["node_id"] for r in results]
        assert 4 in node_ids, f"应命中 node 4，实际: {node_ids}"

    def test_short_query_len3_exact(self, fts_env):
        results = search_fts("pytest")
        node_ids = [r["node_id"] for r in results]
        assert 2 in node_ids, f"应命中 node 2，实际: {node_ids}"

    def test_empty_query_returns_empty(self, fts_env):
        assert search_fts("") == []
        assert search_fts(None) == []