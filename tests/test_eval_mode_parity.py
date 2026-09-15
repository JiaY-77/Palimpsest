"""
eval 四模式口径一致性回归测试
==============================
锁定 issue #15 Issue1 的修复契约：fts / vec / rrf / cascade 必须共享同一套
过滤口径（tier + outdated），不允许某个模式绕过过滤链。

改动前的问题：
  - _run_fts / _run_vec 不过 tier（日志层会从这两条路漏回结果集）
  - _run_vec 直调 store.search_similar，绕过了 _mem_search_impl 的过滤链

隔离说明：模块只在「构造查询」层面校验口径，不实际连库跑嵌入；
涉及库的部分复用 test_memory_tier 的 iso 思路，用 _insert 直写节点。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile

import pytest

from core.trivium_store import TriviumStore


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（与共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_eval_iso_")
    from config import Config

    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def iso(iso_store, monkeypatch):
    """把 memory 模块全局 store 换成隔离库。"""
    from mcp_tools import _common, memory

    monkeypatch.setattr(memory, "store", iso_store)
    monkeypatch.setattr(_common, "store", iso_store)
    return iso_store


def _insert(store, payload: dict, content: str, sync_fts: bool = False) -> int:
    node_payload = dict(payload)
    node_payload["content"] = content
    nid = store.insert_node(node_payload, store.embed_text(content))
    if sync_fts:
        # 直写节点不会自动进 FTS 索引；fts 模式要能命中必须先同步
        from core.fts_index import sync_node

        sync_node(nid, content)
    return nid


def test_all_modes_accept_tier_parameter():
    """契约：四个模式函数都必须显式接受 tier（不能只有 rrf/cascade 支持）。"""
    import inspect

    import eval.run_eval as re_mod

    for mode in ("fts", "vec", "rrf", "cascade"):
        fn = re_mod._MODE_FNS[mode]
        params = inspect.signature(fn).parameters
        assert "tier" in params, f"{mode} 缺 tier 参数（会绕过分层过滤）"
        assert params["tier"].default == "facts", f"{mode} 的 tier 默认应为 facts"


def test_vec_mode_uses_shared_filter_chain():
    """契约：vec 模式必须走 _mem_search_impl，而不是直调 store.search_similar。"""
    import ast
    import inspect
    import textwrap

    import eval.run_eval as re_mod

    fn = re_mod._run_vec
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    # 只统计真实调用/属性访问，忽略 docstring 与注释里的叙述
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            names.add(ast.unparse(node.func))
    assert any("_mem_search_impl" in n for n in names), "vec 模式应复用共享过滤链"
    assert not any("search_similar" in n for n in names), \
        "vec 模式不应再直调 search_similar 绕过过滤"


def test_fts_mode_respects_tier_in_source():
    """契约：fts 模式的源码里必须有 tier 过滤逻辑。"""
    import inspect

    import eval.run_eval as re_mod

    src = inspect.getsource(re_mod._run_fts)
    assert "_tier_matches" in src, "fts 模式应过 tier 过滤"
    assert "outdated" in src, "fts 模式应过 outdated 过滤"


def test_four_modes_agree_on_tier_filtering(iso):
    """端到端：默认 facts 层下，四个模式都不该回日志节点。

    这是 issue #15 Issue1 的核心症状 —— 同一查询在不同模式间口径不一致。
    """
    import eval.run_eval as re_mod
    from mcp_tools.memory import _tier_matches

    q = "口径一致性回归：南太平洋深海热液喷口的硫化物烟囱采样"
    _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, q)
    log_id = _insert(iso, {"type": "record", "domain": "hermes", "importance": 0.5},
                     q + "（日志）")

    # 前提断言：测试数据本身确实分层正确
    assert _tier_matches("memory", "facts") and not _tier_matches("record", "facts")

    for mode in ("fts", "vec", "rrf", "cascade"):
        ids, _scores = re_mod._MODE_FNS[mode](q, 20, iso, "facts")
        assert log_id not in ids, f"mode={mode} 在 facts 层下漏回了日志节点"


def test_four_modes_agree_on_tier_empty(iso):
    """tier=\"\" 显式历史通道：语义侧模式都应能回日志节点（口径同样一致）。

    注：fts 模式是纯全文检索，走 trigram 分词，中文短句在临时库里不保证命中，
    这里只断言三个语义侧模式（vec/rrf/cascade）——它们共享过滤链，是本 issue
    口径不一致的主体。
    """
    import eval.run_eval as re_mod

    q = "口径一致性回归乙：北极圈永冻土甲烷通量的多年连续观测"
    _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, q)
    log_id = _insert(iso, {"type": "record", "domain": "hermes", "importance": 0.5},
                     q + "（记录）")

    for mode in ("vec", "rrf", "cascade"):
        ids, _scores = re_mod._MODE_FNS[mode](q, 20, iso, "")
        assert log_id in ids, f"mode={mode} 在 tier='' 时应回日志节点"
