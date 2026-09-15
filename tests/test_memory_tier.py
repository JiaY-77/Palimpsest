"""
记忆分层（tier）回归测试
========================
验证 T081 的核心契约：

  1. tier="" 不过滤 —— 逐条等价于改动前行为（回归红线）
  2. tier="facts"（默认）只回事实层；日志层（record/event/git_commit）被摘出
  3. tier="logs" 只回日志层
  4. 未登记的 type 保守归 facts（不静默丢结果）
  5. kb_chunk / novel_chunk 不受 tier 约束（走已有 scope 隔离）
  6. FTS-only 侧与语义侧同受约束（否则日志层会从 FTS 路漏回）
  7. MCP 工具层 / REST 模型 / CLI 的参数存在性（全链路透传）

隔离说明：本模块用【自建独立临时库】（iso_store 模式，同 test_mem_stats），
不经由 conftest 的 session 级共享库。原因：共享库另有多组测试断言全库一致性
（如 check_fts_consistency），直写节点不更新 FTS 会污染那些断言。
不依赖 Ollama：fake embedding 由 conftest 的 session 级 fake_embedder 统一提供。
"""

import contextlib
import os
import shutil
import tempfile

import pytest

from core.trivium_store import TriviumStore
from mcp_tools.memory import (
    DEFAULT_TIER,
    TIER_FACTS,
    TIER_LOGS,
    _hybrid_search_impl,
    _mem_search_impl,
    _tier_matches,
)


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（与共享库完全隔离）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_tier_iso_")
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
    """把 mcp_tools.memory 用到的全局 store 换成隔离库，返回该 store。

    memory.py 内部 `from mcp_tools._common import store` 拿到的是模块级单例，
    这里同时 patch memory 模块里的引用与 _common 单例，确保检索走隔离库。
    """
    from mcp_tools import _common, memory

    monkeypatch.setattr(memory, "store", iso_store)
    monkeypatch.setattr(_common, "store", iso_store)
    return iso_store


def _insert(store, payload: dict, content: str) -> int:
    """直写节点（绕过 mem_ingest 冲突检测），返回 node_id。"""
    node_payload = dict(payload)
    node_payload["content"] = content
    return store.insert_node(node_payload, store.embed_text(content))


def _ids(out: dict) -> set:
    return {r["id"] for r in out.get("results", [])}


# ---------------------------------------------------------------------------
# 1. _tier_matches 纯函数契约（不碰库）
# ---------------------------------------------------------------------------
class TestTierMatches:
    def test_empty_tier_disables_filtering(self):
        for t in ("record", "event", "git_commit", "memory", "kb_chunk", "whatever"):
            assert _tier_matches(t, "") is True

    def test_unknown_tier_value_falls_back_to_no_filter(self):
        # 真非法值（拼错/未定义）不应静默把结果清空——退回不过滤，行为可见。
        # 注意：大小写与首尾空白是合法变体（见 test_tier_is_case_insensitive），
        # 它们会被规整成 facts/logs，不属于「非法」。
        for bad in ("fact", "log", "both", "none", "FACTS2"):
            assert _tier_matches("record", bad) is True, bad
            assert _tier_matches("memory", bad) is True, bad

    def test_facts_layer_definition(self):
        for t in TIER_FACTS:
            assert _tier_matches(t, "facts") is True
        for t in TIER_LOGS:
            assert _tier_matches(t, "facts") is False

    def test_logs_layer_definition(self):
        for t in TIER_LOGS:
            assert _tier_matches(t, "logs") is True
        for t in TIER_FACTS:
            assert _tier_matches(t, "logs") is False

    def test_unregistered_type_defaults_to_facts(self):
        # 保守兜底：plot_plan / rule 等未登记 type 归 facts，不被默默丢掉
        for t in ("plot_plan", "rule", "unknown_type", ""):
            assert _tier_matches(t, "facts") is True
            assert _tier_matches(t, "logs") is False

    def test_kb_and_novel_chunk_not_governed(self):
        # kb_chunk / novel_chunk 不入 tier 体系，facts 下保留（由 scope 隔离）
        assert _tier_matches("kb_chunk", "facts") is True
        assert _tier_matches("novel_chunk", "facts") is True

    def test_tier_is_case_insensitive(self):
        assert _tier_matches("record", "LOGS") is True
        assert _tier_matches("memory", "Facts") is True

    def test_default_tier_constant_is_facts(self):
        assert DEFAULT_TIER == "facts"


# ---------------------------------------------------------------------------
# 2. 端到端：默认 facts 过滤日志层，tier="" 全回
# ---------------------------------------------------------------------------
def test_default_tier_excludes_logs(iso):
    q = "分层回归甲：北极航道破冰船的柴油机维护周期记录"
    fact_id = _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, q)
    log_id = _insert(iso, {"type": "record", "domain": "hermes", "importance": 0.5},
                     q + "（日志）")

    default_out = _mem_search_impl(q, scope="all", top_k=20)
    assert fact_id in _ids(default_out)
    assert log_id not in _ids(default_out), "默认 facts 层不应回日志节点"


def test_empty_tier_returns_both(iso):
    q = "分层回归乙：南极冰盖钻探取样的同位素比值表"
    fact_id = _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, q)
    log_id = _insert(iso, {"type": "record", "domain": "hermes", "importance": 0.5},
                     q + "（日志）")

    out = _mem_search_impl(q, scope="all", top_k=20, tier="")
    ids = _ids(out)
    assert fact_id in ids and log_id in ids


def test_logs_tier_returns_only_logs(iso):
    q = "分层回归丙：马达加斯加狐猴种群夜间声学监测"
    fact_id = _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, q)
    log_id = _insert(iso, {"type": "event", "domain": "hermes", "importance": 0.5},
                     q + "（事件）")

    out = _mem_search_impl(q, scope="all", top_k=20, tier="logs")
    ids = _ids(out)
    assert log_id in ids
    assert fact_id not in ids


def test_all_three_log_types_filtered(iso):
    q = "分层回归丁：格陵兰冰芯气泡的古大气成分"
    log_ids = set()
    for i, t in enumerate(("record", "event", "git_commit")):
        log_ids.add(_insert(iso, {"type": t, "domain": "hermes", "importance": 0.5},
                            f"{q} 第{i}号"))

    out = _mem_search_impl(q, scope="all", top_k=20)
    assert log_ids & _ids(out) == set(), "三个日志类型都不该被 facts 层返回"

    out_all = _mem_search_impl(q, scope="all", top_k=20, tier="")
    assert log_ids <= _ids(out_all), "tier='' 时应全回"


def test_unregistered_type_survives_facts(iso):
    q = "分层回归戊：安第斯山脉高原鼠兔的洞穴温度梯度"
    pid = _insert(iso, {"type": "plot_plan", "domain": "hermes", "importance": 0.5}, q)
    out = _mem_search_impl(q, scope="all", top_k=20)
    assert pid in _ids(out), "未登记 type 应保守归 facts"


def test_tier_empty_equals_pre_change_behaviour(iso):
    """回归红线：tier="" 时逐条等价于改动前行为（不过滤）。"""
    q = "分层回归己：鄂霍次克海海冰边缘区浮游植物水华观测"
    ids = set()
    for t in ("memory", "record", "event", "correction", "task"):
        ids.add(_insert(iso, {"type": t, "domain": "hermes", "importance": 0.5},
                        f"{q} 类型{t}"))

    out = _mem_search_impl(q, scope="all", top_k=20, tier="")
    got = _ids(out)
    assert ids <= got, f"tier='' 应回全部类型，缺 {ids - got}"
    scores = [r["score"] for r in out["results"]]
    assert scores == sorted(scores, reverse=True), "结果必须按 score 降序"


# ---------------------------------------------------------------------------
# 3. 混合检索：FTS-only 侧同受约束
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["rrf", "cascade"])
def test_hybrid_respects_tier(iso, mode):
    q = "分层混合回归：塞伦盖蒂角马迁徙的河道水文监测"
    fact_id = _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, q)
    log_id = _insert(iso, {"type": "record", "domain": "hermes", "importance": 0.5},
                     q + "（记录）")

    default_ids = _ids(_hybrid_search_impl(q, scope="all", top_k=20, mode=mode))
    assert fact_id in default_ids
    assert log_id not in default_ids, f"mode={mode} 时日志层应从 FTS 侧也被挡住"

    all_ids = _ids(_hybrid_search_impl(q, scope="all", top_k=20, mode=mode, tier=""))
    assert fact_id in all_ids and log_id in all_ids


# ---------------------------------------------------------------------------
# 4. 全链路透传（MCP 工具 / REST 模型 / CLI）
# ---------------------------------------------------------------------------
def test_mcp_tools_accept_tier():
    import inspect

    from mcp_tools.memory import mem_hybrid_search, mem_search

    for fn in (mem_search, mem_hybrid_search):
        assert "tier" in inspect.signature(fn).parameters
        assert inspect.signature(fn).parameters["tier"].default == "facts"


def test_rest_models_accept_tier():
    from main import MemHybridSearchRequest, MemSearchRequest

    assert MemSearchRequest.model_fields["tier"].default == "facts"
    assert MemHybridSearchRequest.model_fields["tier"].default == "facts"
    # 默认值必须真实生效（不只是字段存在）
    assert MemSearchRequest(query="x").tier == "facts"
    assert MemHybridSearchRequest(query="x").tier == "facts"
    assert MemSearchRequest(query="x", tier="").tier == ""
    assert MemSearchRequest(query="x", tier="logs").tier == "logs"


def test_cli_parsers_accept_tier():
    from scripts.palimpsest_cli import build_parser

    for cmd in ("search", "hybrid-search"):
        assert build_parser().parse_args([cmd, "词"]).tier == "facts", \
            f"{cmd} 默认应为 facts"
        assert build_parser().parse_args([cmd, "词", "--tier", ""]).tier == ""
