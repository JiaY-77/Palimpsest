"""
冲突取代语义测试（显式取代 + 自动取代开关）
================================================
产品决策（2026-10）：**取代默认不自动发生**。

  - 默认（``CONFLICT_AUTO_SUPERSEDE`` 关闭）：写入相似内容只记入 ``related_ids``
    （话题相关提示），**不标 outdated**——「相似」不等于「同一事实被取代」，
    避免同主题互补内容被误吞。
  - 显式取代：``mem_ingest(replaces="<id>")`` 无条件标这些旧节点 outdated +
    建 ``新 --REVISED_BY--> 旧`` 修订链（语义判断由调用方给出，不依赖阈值）。
  - 旧「相似度 >0.75 自动取代」仅在 ``CONFLICT_AUTO_SUPERSEDE=true`` 时启用
    （过渡兼容开关）。

隔离级别：**自建独立库**（tempfile + 临时 DB_PATH + 替换 memory 模块的 store）。
本模块会真实写入节点；若写进 conftest 的共享 session 库，会污染
``test_mem_communities`` / ``test_mem_recent_behavior`` / ``test_smoke`` 这类数
节点/数簇的用例（单跑绿、合跑红）。照 tests/test_skill_search_recall.py 的既有模式。
"""

import contextlib
import json
import shutil

import pytest

import mcp_tools.memory as memory_tool
from config import Config
from core.trivium_store import TriviumStore


@pytest.fixture(autouse=True)
def _default_no_auto_supersede(monkeypatch):
    """把自动取代显式置为默认关闭（真实默认值），使用例不依赖 .env 状态。

    需要验证旧自动路径的用例可在测试体内再次 monkeypatch 覆盖。
    """
    monkeypatch.setattr(Config, "CONFLICT_AUTO_SUPERSEDE", False, raising=False)


@pytest.fixture
def iso_store(tmp_path, monkeypatch):
    """为本模块创建独立数据库与 store，绝不触碰 conftest 的共享 session 库。"""
    db_path = tmp_path / "conflict_supersede_test.db"
    monkeypatch.setattr(Config, "DB_PATH", str(db_path))
    store = TriviumStore()
    original = memory_tool.store
    memory_tool.store = store
    try:
        yield store
    finally:
        memory_tool.store = original
        with contextlib.suppress(Exception):
            store._acquire().close()
        shutil.rmtree(tmp_path, ignore_errors=True)


def _get(result: str) -> dict:
    return json.loads(result)


def test_default_write_does_not_supersede(iso_store):
    """默认：写两条相似记忆 → 都不标 outdated，旧节点只出现在 related_ids。"""
    text = "小帕冲突语义测试片段：同一主题的互补内容"
    r1 = _get(memory_tool.mem_ingest(content=text, type="memory", domain="cs"))
    r2 = _get(memory_tool.mem_ingest(content=text, type="memory", domain="cs"))

    assert r1["stored"] is True and r2["stored"] is True, (r1, r2)
    # 不再自动取代
    assert r2["conflict_found"] is False, r2
    assert r2["outdated_ids"] == [], r2
    # 但相似旧节点被记为「相关」
    assert r1["node_id"] in r2["related_ids"], r2
    # 两条节点都仍为 active
    assert iso_store.get_node(r1["node_id"])["payload"].get("status") != "outdated"
    assert iso_store.get_node(r2["node_id"])["payload"].get("status") != "outdated"


def test_explicit_replaces_supersedes(iso_store):
    """显式 replaces：旧节点标 outdated + 建 REVISED_BY 边（不论相似度）。"""
    r1 = _get(memory_tool.mem_ingest(content="旧事实：版本一", type="memory", domain="cs"))
    old_id = r1["node_id"]

    r2 = _get(
        memory_tool.mem_ingest(
            content="新事实：版本二（取代上一版）",
            type="memory",
            domain="cs",
            replaces=str(old_id),
        )
    )
    assert r2["stored"] is True, r2
    assert r2["conflict_found"] is True, r2
    assert old_id in r2["outdated_ids"], r2
    assert iso_store.get_node(old_id)["payload"].get("status") == "outdated"

    # 修订链：新节点 --REVISED_BY--> 旧节点
    edges = [(e.target_id, (getattr(e, "label", "") or "").upper()) for e in iso_store.get_edges(r2["node_id"])]
    assert (old_id, "REVISED_BY") in edges, edges


def test_explicit_replaces_accepts_chinese_comma_and_ignores_garbage(iso_store):
    """replaces 解析：兼容中文逗号 / 空白，非法片段忽略，不存在 id 静默跳过。"""
    r1 = _get(memory_tool.mem_ingest(content="旧事实 A", type="memory", domain="cs"))
    old_id = r1["node_id"]

    r2 = _get(
        memory_tool.mem_ingest(
            content="新事实 B",
            type="memory",
            domain="cs",
            replaces=f"{old_id}， 999999，not-an-id",
        )
    )
    assert r2["stored"] is True, r2
    # 存在的旧 id 被取代；不存在的 999999 静默跳过（不报错、不入 outdated）
    assert old_id in r2["outdated_ids"], r2
    assert 999999 not in r2["outdated_ids"], r2
    assert iso_store.get_node(old_id)["payload"].get("status") == "outdated"


def test_replaces_respects_gates_kb_chunk_not_superseded(iso_store):
    """replaces 仍受 type 门约束：不能把 kb_chunk 标 outdated。"""
    kb = _get(memory_tool.mem_ingest(content="知识块", type="kb_chunk", domain="kb"))
    kb_id = kb["node_id"]

    r2 = _get(memory_tool.mem_ingest(content="记忆", type="memory", domain="cs", replaces=str(kb_id)))
    assert r2["stored"] is True, r2
    # type 不同（kb_chunk vs memory）→ 过不了门 → 不标 outdated
    assert kb_id not in r2["outdated_ids"], r2
    assert iso_store.get_node(kb_id)["payload"].get("status") != "outdated"


def test_legacy_auto_supersede_opt_in(iso_store, monkeypatch):
    """CONFLICT_AUTO_SUPERSEDE=true 时回退到旧行为：相似度 >0.75 自动标 outdated。"""
    monkeypatch.setattr(Config, "CONFLICT_AUTO_SUPERSEDE", True, raising=False)
    text = "自动取代回退测试"
    r1 = _get(memory_tool.mem_ingest(content=text, type="memory", domain="cs"))
    r2 = _get(memory_tool.mem_ingest(content=text, type="memory", domain="cs"))

    assert r2["conflict_found"] is True, r2
    assert r1["node_id"] in r2["outdated_ids"], r2
    assert iso_store.get_node(r1["node_id"])["payload"].get("status") == "outdated"
