"""技能语义索引与检索测试。

测试使用临时 TriviumDB 和确定性 fake embedding，不连接 Ollama，也不触碰正式库。
"""

import contextlib
import json
import os
import shutil

import pytest

import mcp_tools.skill as skill_tool
from config import Config
from core.trivium_store import TriviumStore
from scripts.build_skill_index import build as build_skill_index


@pytest.fixture
def iso_store(tmp_path, monkeypatch):
    """为每个测试创建独立数据库和 store。"""
    db_path = tmp_path / "skill_test.db"
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


def _write_skill(root, relative_dir: str, name: str, description: str,
                 body: str) -> str:
    """写一个带 frontmatter 的 SKILL.md，并返回其绝对路径。"""
    path = root / relative_dir / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return str(path.resolve())


def _skill_payloads(store):
    return [
        payload
        for _node_id, payload in store.iter_payloads()
        if payload.get("type") == "skill_chunk"
    ]


def test_build_skill_index_is_idempotent(iso_store, fake_embedder, tmp_path):
    """同一批技能连续构建两次时，节点 ID、数量和内容均保持不变。"""
    root = tmp_path / "skills"
    first = _write_skill(
        root, "software-development", "git-flow",
        "Manage branches and pull requests",
        "Create a feature branch, commit changes, and open a pull request.",
    )
    second = _write_skill(
        root, "", "release-notes",
        "Write release notes",
        "Summarize user-visible changes for a release.",
    )

    first_result = build_skill_index(str(root), store=iso_store, full=True)
    first_payloads = _skill_payloads(iso_store)
    first_by_path = {payload["source_path"]: payload for payload in first_payloads}
    first_ids = {
        node_id
        for node_id, payload in iso_store.iter_payloads()
        if payload.get("type") == "skill_chunk"
    }

    second_result = build_skill_index(str(root), store=iso_store)

    second_payloads = _skill_payloads(iso_store)
    second_by_path = {payload["source_path"]: payload for payload in second_payloads}
    second_ids = {
        node_id
        for node_id, payload in iso_store.iter_payloads()
        if payload.get("type") == "skill_chunk"
    }
    assert first_result["indexed"] == 2
    assert second_result["indexed"] == 0
    assert second_result["skipped"] == 2
    assert len(first_payloads) == len(second_payloads) == 2
    assert first_ids == second_ids
    assert first_by_path[first]["content"] == second_by_path[first]["content"]
    assert first_by_path[second]["content"] == second_by_path[second]["content"]


def test_skill_search_finds_matching_skill(iso_store, fake_embedder, tmp_path, monkeypatch):
    """技能检索只返回 skill_chunk，且能命中目标技能。"""
    root = tmp_path / "skills"
    _write_skill(
        root, "software-development", "git-flow",
        "Manage branches and pull requests",
        "Create a feature branch, commit changes, and open a pull request.",
    )
    _write_skill(
        root, "operations", "deployment",
        "Deploy services safely",
        "Run a staged deployment and verify health checks.",
    )
    build_skill_index(str(root), store=iso_store, full=True)

    # skill_search 使用 mcp_tools 全局 store；测试中切换到隔离 store。
    monkeypatch.setattr(skill_tool, "store", iso_store)
    result = json.loads(skill_tool.skill_search("pull request branch"))

    assert result["results"]
    assert result["results"][0]["name"] == "git-flow"
    assert result["results"][0]["description"] == "Manage branches and pull requests"
    assert result["results"][0]["category"] == "software-development"
    assert result["results"][0]["source_path"].endswith("SKILL.md")
    assert isinstance(result["results"][0]["score"], (int, float))


def test_build_skill_index_removes_orphan(iso_store, fake_embedder, tmp_path):
    """源 SKILL.md 删除后，增量重建会清理对应 skill_chunk 节点。"""
    root = tmp_path / "skills"
    removed = _write_skill(
        root, "research", "literature-review",
        "Review academic literature",
        "Search papers, compare findings, and summarize evidence.",
    )
    kept = _write_skill(
        root, "", "meeting-notes",
        "Capture meeting notes",
        "Record decisions, actions, and owners after a meeting.",
    )
    build_skill_index(str(root), store=iso_store, full=True)
    node_ids = {
        node_id: payload
        for node_id, payload in iso_store.iter_payloads()
        if payload.get("type") == "skill_chunk"
    }
    removed_id = next(node_id for node_id, payload in node_ids.items()
                      if payload["source_path"] == removed)
    kept_id = next(node_id for node_id, payload in node_ids.items()
                   if payload["source_path"] == kept)

    os.remove(removed)
    result = build_skill_index(str(root), store=iso_store)

    remaining = dict(iso_store.iter_payloads())
    assert result["cleaned"] == 1
    assert removed_id not in remaining
    assert kept_id in remaining
    assert remaining[kept_id]["source_path"] == kept
