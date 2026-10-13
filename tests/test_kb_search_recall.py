"""kb_search 的候选召回测试。

背景：kb_search 先取全局 top_k 条相似结果、再过滤出 kb_chunk。知识库块在库里
是少数派——当普通记忆节点在语义上比知识库块更接近查询时，kb_chunk 会被挤出
候选窗口，过滤后返回空，即使知识库索引里确实有匹配内容。修复方案是把过滤条件
下推到检索层（payload_filter），让检索只在 kb_chunk 子集内排序。

测试使用临时 TriviumDB 和确定性 fake embedding，不连接 Ollama，也不触碰正式库。
"""

import contextlib
import json
import shutil

import pytest

import mcp_tools.kb as kb_tool
from config import Config
from core.trivium_store import TriviumStore


@pytest.fixture
def iso_store(tmp_path, monkeypatch):
    """为本测试创建独立数据库和 store（与技能召回测试同款隔离级别）。"""
    db_path = tmp_path / "kb_recall_test.db"
    monkeypatch.setattr(Config, "DB_PATH", str(db_path))
    store = TriviumStore()
    try:
        yield store
    finally:
        with contextlib.suppress(Exception):
            store._acquire().close()
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def fake_embedder(monkeypatch):
    """用可解释的 token 向量替代真实 embedding。"""

    def _embed(text: str) -> list[float]:
        vector = [0.0] * 1024
        for token in str(text).lower().split():
            index = sum(ord(char) for char in token) % len(vector)
            vector[index] += 1.0
        return vector

    monkeypatch.setattr(TriviumStore, "embed_text", staticmethod(_embed))
    return _embed


def _kb_node(content: str) -> dict:
    return {
        "type": "kb_chunk",
        "domain": "kb",
        "content": content,
        "title": "release workflow",
        "source_path": "03_技术学习/release-workflow.md",
        "status": "active",
    }


def test_kb_search_survives_dense_memory_competition(iso_store, fake_embedder):
    """知识库块被更相似的记忆节点挤出 top_k 窗口时，仍应被检索到。

    噪声数量需超过检索层内部的候选放大窗口（top_k*6，至少 10），
    否则放大机制本身就能兜住，测不出「后置过滤饥饿」。
    """
    query = "release workflow"
    emb = fake_embedder(query)

    # 记忆节点：向量与查询完全一致，必然排在知识库块之前；
    # 数量超过检索层候选窗口，确保知识库块被挤出。
    for index in range(12):
        iso_store.insert_node(
            {
                "type": "memory",
                "domain": "hermes",
                "content": f"noise memory {index}",
                "status": "active",
            },
            list(emb),
        )

    # 知识库块：内容相关但相似度低于噪声（向量减半）。
    iso_store.insert_node(_kb_node("release workflow\nShip a release safely.\n"), [value * 0.5 for value in emb])

    original = kb_tool.store
    try:
        kb_tool.store = iso_store
        result = json.loads(kb_tool.kb_search(query, top_k=1))
    finally:
        kb_tool.store = original

    assert result["results"], "知识库块被记忆候选挤出窗口后返回了空结果"
    assert result["results"][0]["path"] == "03_技术学习/release-workflow.md"
    assert len(result["results"]) == 1


def test_kb_search_survives_competition_beyond_candidate_window(iso_store, fake_embedder):
    """噪声节点多到填满候选放大窗口时，知识库块仍应被检索到。

    仅放大候选窗口（如 top_k*3）只能缓解挤压：噪声足够多时窗口会被填满，
    知识库块依旧被过滤掉。真正的修法是让检索在 kb_chunk 子集内进行
    （payload_filter），候选池隔离后噪声再多也不影响。
    """
    query = "release workflow"
    emb = fake_embedder(query)

    # 30 个「完全等同查询」的记忆节点——超过任意候选放大窗口。
    for index in range(30):
        iso_store.insert_node(
            {
                "type": "memory",
                "domain": "hermes",
                "content": f"noise memory {index}",
                "status": "active",
            },
            list(emb),
        )

    # 知识库块内容相关，但向量弱于噪声（向量减半）。
    iso_store.insert_node(_kb_node("release workflow\nShip a release safely.\n"), [value * 0.5 for value in emb])

    original = kb_tool.store
    try:
        kb_tool.store = iso_store
        result = json.loads(kb_tool.kb_search(query, top_k=1))
    finally:
        kb_tool.store = original

    assert result["results"], "噪声填满候选窗口后知识库检索返回了空结果"
    assert result["results"][0]["path"] == "03_技术学习/release-workflow.md"
