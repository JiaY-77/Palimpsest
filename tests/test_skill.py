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
from scripts.build_skill_index import (
    SkillsDirNotFoundError,
    resolve_skills_dir,
    skills_dir_candidates,
)
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


def _write_skill(root, relative_dir: str, name: str, description: str, body: str) -> str:
    """写一个带 frontmatter 的 SKILL.md，并返回其绝对路径。"""
    path = root / relative_dir / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return str(path.resolve())


def _skill_payloads(store):
    return [payload for _node_id, payload in store.iter_payloads() if payload.get("type") == "skill_chunk"]


def test_build_skill_index_is_idempotent(iso_store, fake_embedder, tmp_path):
    """同一批技能连续构建两次时，节点 ID、数量和内容均保持不变。"""
    root = tmp_path / "skills"
    first = _write_skill(
        root,
        "software-development",
        "git-flow",
        "Manage branches and pull requests",
        "Create a feature branch, commit changes, and open a pull request.",
    )
    second = _write_skill(
        root,
        "",
        "release-notes",
        "Write release notes",
        "Summarize user-visible changes for a release.",
    )

    first_result = build_skill_index(str(root), store=iso_store, full=True)
    first_payloads = _skill_payloads(iso_store)
    first_by_path = {payload["source_path"]: payload for payload in first_payloads}
    first_ids = {node_id for node_id, payload in iso_store.iter_payloads() if payload.get("type") == "skill_chunk"}

    second_result = build_skill_index(str(root), store=iso_store)

    second_payloads = _skill_payloads(iso_store)
    second_by_path = {payload["source_path"]: payload for payload in second_payloads}
    second_ids = {node_id for node_id, payload in iso_store.iter_payloads() if payload.get("type") == "skill_chunk"}
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
        root,
        "software-development",
        "git-flow",
        "Manage branches and pull requests",
        "Create a feature branch, commit changes, and open a pull request.",
    )
    _write_skill(
        root,
        "operations",
        "deployment",
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
        root,
        "research",
        "literature-review",
        "Review academic literature",
        "Search papers, compare findings, and summarize evidence.",
    )
    kept = _write_skill(
        root,
        "",
        "meeting-notes",
        "Capture meeting notes",
        "Record decisions, actions, and owners after a meeting.",
    )
    build_skill_index(str(root), store=iso_store, full=True)
    node_ids = {
        node_id: payload for node_id, payload in iso_store.iter_payloads() if payload.get("type") == "skill_chunk"
    }
    removed_id = next(node_id for node_id, payload in node_ids.items() if payload["source_path"] == removed)
    kept_id = next(node_id for node_id, payload in node_ids.items() if payload["source_path"] == kept)

    os.remove(removed)
    result = build_skill_index(str(root), store=iso_store)

    remaining = dict(iso_store.iter_payloads())
    assert result["cleaned"] == 1
    assert removed_id not in remaining
    assert kept_id in remaining
    assert remaining[kept_id]["source_path"] == kept


# ---------------------------------------------------------------------------
# 技能目录解析（profile 感知）
# ---------------------------------------------------------------------------


def _make_hermes_home(root, *, profile=None):
    """搭一个 Hermes 目录骨架：<root>/skills（base）+ 可选 profiles/<name>/skills。

    返回 (env, base_skills_dir, profile_skills_dir|None)。
    """
    env = {"LOCALAPPDATA": str(root / "LocalAppData"), "HERMES_HOME": str(root / "hermes")}
    base_skills = root / "hermes" / "skills"
    base_skills.mkdir(parents=True, exist_ok=True)
    profile_skills = None
    if profile:
        profile_skills = root / "hermes" / "profiles" / profile / "skills"
        profile_skills.mkdir(parents=True, exist_ok=True)
        env["HERMES_HOME"] = str(root / "hermes" / "profiles" / profile)
        env["HERMES_PROFILE"] = profile
    return env, str(base_skills.resolve()), (str(profile_skills.resolve()) if profile_skills else None)


def test_resolve_skills_dir_profile_scoped_home(tmp_path):
    """HERMES_HOME 指向档案目录时直接解析到该档案的技能目录。"""
    env, _base, profile_skills = _make_hermes_home(tmp_path, profile="xiaojiu")
    assert resolve_skills_dir(env=env) == profile_skills


def test_resolve_skills_dir_uses_profile_when_home_is_root(tmp_path):
    """HERMES_HOME 停在根、但 HERMES_PROFILE 指定的档案目录存在时，优先档案目录。"""
    env, base, profile_skills = _make_hermes_home(tmp_path, profile="xiaojiu")
    # 把 HERMES_HOME 退回根：模拟只钉了 HERMES_PROFILE 的启动器
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    assert resolve_skills_dir(env=env) == profile_skills
    # 根下 base 技能目录仍作为后备出现在候选里
    assert base in skills_dir_candidates(env=env)


def test_resolve_skills_dir_falls_back_to_home_skills(tmp_path):
    """未设 HERMES_PROFILE 时解析到 <HERMES_HOME>/skills。"""
    env = {"HERMES_HOME": str(tmp_path / "hermes")}
    (tmp_path / "hermes" / "skills").mkdir(parents=True)
    expected = str((tmp_path / "hermes" / "skills").resolve())
    assert resolve_skills_dir(env=env) == expected


def test_resolve_skills_dir_explicit_wins(tmp_path):
    """显式 --skills-dir 覆盖一切（即便档案目录存在也走显式值）。"""
    env, _base, _profile = _make_hermes_home(tmp_path, profile="xiaojiu")
    explicit = tmp_path / "custom-skills"
    explicit.mkdir()
    assert resolve_skills_dir(str(explicit), env=env) == str(explicit.resolve())
    assert skills_dir_candidates(str(explicit), env=env) == [str(explicit.resolve())]


def test_resolve_skills_dir_missing_raises(tmp_path):
    """所有候选目录都不存在时抛 SkillsDirNotFoundError（不返回不存在的路径）。"""
    env = {
        "HERMES_HOME": str(tmp_path / "nope" / "hermes"),
        "HERMES_PROFILE": "ghost",
        "LOCALAPPDATA": str(tmp_path / "LocalAppData"),
    }
    with pytest.raises(SkillsDirNotFoundError):
        resolve_skills_dir(env=env)


# ---------------------------------------------------------------------------
# 失败路径：目录缺失绝不能清空全库（回归防护）
# ---------------------------------------------------------------------------


def test_build_rejects_missing_dir_without_deleting(iso_store, fake_embedder, tmp_path):
    """目标技能目录不存在时，build 必须 fail-fast，且**不删除任何**已索引节点。

    回归锁：历史上 _skill_files 对缺失目录返回 []，known_paths 变空，
    孤儿清理会把库中全部 skill_chunk 删光。
    """
    root = tmp_path / "skills"
    kept = _write_skill(
        root,
        "research",
        "literature-review",
        "Review academic literature",
        "Search papers, compare findings, and summarize evidence.",
    )
    build_skill_index(str(root), store=iso_store, full=True)
    before = {nid: p for nid, p in iso_store.iter_payloads() if p.get("type") == "skill_chunk"}
    assert len(before) == 1

    missing = str(tmp_path / "does-not-exist" / "skills")
    with pytest.raises(SkillsDirNotFoundError):
        build_skill_index(missing, store=iso_store)

    after = {nid: p for nid, p in iso_store.iter_payloads() if p.get("type") == "skill_chunk"}
    assert after == before  # 节点与其 payload 原样保留
    assert any(p["source_path"] == kept for p in after.values())


def test_build_allows_empty_existing_dir(iso_store, fake_embedder, tmp_path):
    """目录真实存在但没有任何 SKILL.md 时是合法的：正常返回，不抛异常。

    与「目录缺失」区分：前者允许孤儿清理（确实无技能了），后者 fatal。
    """
    root = tmp_path / "skills"
    stale = _write_skill(root, "", "stale", "stale skill", "body")
    build_skill_index(str(root), store=iso_store, full=True)
    assert len([1 for _n, p in iso_store.iter_payloads() if p.get("type") == "skill_chunk"]) == 1

    # 删掉唯一的技能文件：目录仍在，清理应把孤儿移除
    os.remove(stale)
    result = build_skill_index(str(root), store=iso_store)
    assert result["cleaned"] == 1
    assert [p for _n, p in iso_store.iter_payloads() if p.get("type") == "skill_chunk"] == []


def test_build_auto_resolves_when_dir_omitted(iso_store, fake_embedder, tmp_path, monkeypatch):
    """不传 skills_dir 时走自动解析；解析失败则 fail-fast、不动库。"""
    root = tmp_path / "skills"
    _write_skill(root, "", "auto", "auto skill", "body")
    monkeypatch.setattr(
        "scripts.build_skill_index.resolve_skills_dir",
        lambda *a, **k: str(root),
    )
    result = build_skill_index(store=iso_store, full=True)
    assert result["indexed"] == 1
    assert result["skills_dir"] == str(root.resolve())

    monkeypatch.setattr(
        "scripts.build_skill_index.resolve_skills_dir",
        lambda *a, **k: (_ for _ in ()).throw(SkillsDirNotFoundError("无目录")),
    )
    with pytest.raises(SkillsDirNotFoundError):
        build_skill_index(store=iso_store)
