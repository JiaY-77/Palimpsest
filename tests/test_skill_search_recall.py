"""skill_search 的候选召回测试。

背景：skill_search 先取全局 top_k 条相似结果、再过滤出 skill_chunk。
当普通记忆节点在语义上比技能节点更接近查询时，技能会被挤出候选窗口，
过滤后返回空——即使技能确实存在于索引中。修复方案是先多取候选、
过滤之后再截断到 top_k。

测试使用临时 TriviumDB 和确定性 fake embedding，不连接 Ollama，也不触碰正式库。
"""

import contextlib
import json
import shutil

import pytest

import mcp_tools.skill as skill_tool
from config import Config
from core.trivium_store import TriviumStore


@pytest.fixture
def iso_store(tmp_path, monkeypatch):
    """为本测试创建独立数据库和 store（与其他技能测试同款隔离级别）。"""
    db_path = tmp_path / "skill_recall_test.db"
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


def test_skill_search_survives_dense_non_skill_competition(iso_store, fake_embedder):
    """技能节点被更相似的普通记忆节点挤出 top_k 窗口时，仍应被检索到。"""
    # 普通记忆节点，其向量与查询完全一致（必然排在技能之前）。
    query = "release workflow"
    emb = fake_embedder(query)
    for index in range(3):
        iso_store.insert_node(
            {
                "type": "memory",
                "domain": "hermes",
                "content": f"noise memory {index}",
                "name": f"noise-{index}",
                "status": "active",
            },
            list(emb),
        )

    # 技能节点，内容相关但相似度低于上面的噪声节点（向量减半）。
    skill_emb = [value * 0.5 for value in fake_embedder(query)]
    iso_store.insert_node(
        {
            "type": "skill_chunk",
            "domain": "skill",
            "content": "release workflow\nShip a release safely.\n",
            "name": "release-flow",
            "description": "Ship a release safely.",
            "category": "software-development",
            "source_path": "C:/skills/software-development/release-flow/SKILL.md",
            "status": "active",
        },
        skill_emb,
    )

    original = skill_tool.store
    try:
        skill_tool.store = iso_store
        result = json.loads(skill_tool.skill_search(query, top_k=1))
    finally:
        skill_tool.store = original

    assert result["results"], "技能被非技能候选挤出窗口后返回了空结果"
    assert result["results"][0]["name"] == "release-flow"
    assert len(result["results"]) == 1
