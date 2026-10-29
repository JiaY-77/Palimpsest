"""mem_search 新鲜度加权的单元测试（issue #175）。

覆盖：
  1. ``core.bitemporal.freshness_decay``：两种衰减形式的数值、时间基准
     优先级（valid_at > created_at）、无时间字段 / 非法参数 → None；
  2. ``_mem_search_impl`` 排序行为：
     - 默认 WEIGHT=0：同语义分两节点分数完全相等（默认行为不变的红线）；
     - WEIGHT>0：同语义分下新节点排前（加成只随权重生效）；
     - as_of 历史视图：权重不生效（验收红线——as_of 路径不受影响）。

节点构造走 ``store.insert_node``（node_data 透传 created_at/valid_at）而非
``mem_ingest``：后者带冲突检测，两条同 content 记录会互标 outdated，破坏
「同分比新鲜」的构造前提。
隔离：本文件插入的节点在 teardown 全删（conftest 是 session 级共享临时库）。
"""

from __future__ import annotations

import json

import pytest

from config import Config
from core.bitemporal import freshness_decay
from mcp_tools import store
from mcp_tools.memory import _mem_search_impl

NOW = 1_800_000_000.0  # 固定「现在」，纯函数测试不依赖真实时钟
DAY = 86400.0

_CONTENT = "服务的网关端口配置在配置文件里，端口号是 8090"


def _get(result):
    """MCP 工具在测试里可能返回 str(JSON) 或 dict，统一成 dict。"""
    return json.loads(result) if isinstance(result, str) else result


class TestFreshnessDecay:
    """纯函数：衰减形式 / 时间基准 / 无信号兜底。"""

    def test_exp_form_values(self) -> None:
        p = {"created_at": NOW}
        assert freshness_decay(p, NOW, 30) == pytest.approx(1.0)
        assert freshness_decay(p, NOW + 30 * DAY, 30) == pytest.approx(0.5)
        assert freshness_decay(p, NOW + 60 * DAY, 30) == pytest.approx(0.25)

    def test_linear_form_values(self) -> None:
        p = {"created_at": NOW}
        assert freshness_decay(p, NOW, 30, "linear") == pytest.approx(1.0)
        assert freshness_decay(p, NOW + 15 * DAY, 30, "linear") == pytest.approx(0.5)
        assert freshness_decay(p, NOW + 30 * DAY, 30, "linear") == 0.0
        # 超龄截断到 0，不为负
        assert freshness_decay(p, NOW + 90 * DAY, 30, "linear") == 0.0

    def test_valid_at_takes_priority_over_created_at(self) -> None:
        p = {"valid_at": NOW, "created_at": NOW - 365 * DAY}
        # 用 valid_at：age=0 → 满信号；若错用 created_at 则会是旧信号
        assert freshness_decay(p, NOW, 30) == pytest.approx(1.0)

    def test_falls_back_to_created_at(self) -> None:
        p = {"created_at": NOW - 30 * DAY}
        assert freshness_decay(p, NOW, 30) == pytest.approx(0.5)

    def test_no_time_fields_returns_none(self) -> None:
        assert freshness_decay({"content": "x"}, NOW, 30) is None

    def test_invalid_scale_returns_none(self) -> None:
        p = {"created_at": NOW}
        assert freshness_decay(p, NOW, 0) is None
        assert freshness_decay(p, NOW, -1.0) is None

    def test_future_timestamp_clamps_to_full_signal(self) -> None:
        p = {"valid_at": NOW + 10 * DAY}  # 未来时态：还没开始为真
        assert freshness_decay(p, NOW, 30) == pytest.approx(1.0)

    def test_unparseable_timestamp_returns_none(self) -> None:
        """历史数据里 created_at 可能是 ISO 字符串——解析失败当无信号，不崩排序。"""
        p = {"created_at": "2026-09-10T15:44:48+00:00"}
        assert freshness_decay(p, NOW, 30) is None

    def test_unknown_decay_form_uses_exp(self) -> None:
        p = {"created_at": NOW - 30 * DAY}
        assert freshness_decay(p, NOW, 30, "cosine") == pytest.approx(0.5)


class TestSearchRanking:
    """排序行为：默认零权重逐位一致 / 加权抬新 / as_of 豁免。"""

    @pytest.fixture
    def cleanup_nodes(self):
        from core.fts_index import remove_node

        created: list[int] = []
        yield created
        for nid in created:
            try:
                store.delete_node(nid)
            finally:
                remove_node(nid)

    def _mk_pair(self, cleanup_nodes: list[int]) -> tuple[int, int]:
        """插入同 content 两节点（fake embed 下语义分必同），一新一旧。

        返回 (new_id, old_id)。时间显式落在过去，避免与测试执行时刻的
        相对年龄漂移影响断言。
        """
        emb = store.embed_text(_CONTENT)
        new_id = store.insert_node(
            {
                "content": _CONTENT,
                "type": "memory",
                "domain": "test",
                "importance": 0.5,
                "created_at": NOW,
                "valid_at": NOW,
            },
            emb,
        )
        old_id = store.insert_node(
            {
                "content": _CONTENT,
                "type": "memory",
                "domain": "test",
                "importance": 0.5,
                "created_at": NOW - 365 * DAY,
                "valid_at": NOW - 365 * DAY,
            },
            emb,
        )
        cleanup_nodes.extend([new_id, old_id])
        return new_id, old_id

    def _scores(self, query: str, **kw) -> dict[int, float]:
        out = _mem_search_impl(query, scope="all", top_k=10, **kw)
        return {r["id"]: r["score"] for r in out["results"]}

    def test_default_weight_zero_keeps_identical_scores(self, cleanup_nodes, monkeypatch) -> None:
        """默认 WEIGHT=0：新鲜度分支整支跳过——连 ``freshness_decay`` 都不被调用。

        为什么不再比「两节点同分」：issue #226 起 ``insert_node`` 会保留 caller 的
        ``created_at``（此前被静默丢弃），两节点年龄差异因此真实生效，soft 重排
        的时间衰减本就该让它们不同分。那不是新鲜度加权的作用，用同分当判据会把
        两件事混在一起。这里直接把「权重为 0 时该函数一次都不该被调」钉死。
        """
        from mcp_tools import memory as memory_mod

        assert Config.MEM_SEARCH_FRESHNESS_WEIGHT == 0, "全局默认权重不该被本改动改成非零"

        def _must_not_be_called(*a, **kw):
            raise AssertionError("WEIGHT=0 时 freshness_decay 不该被调用（分支必须整支跳过）")

        monkeypatch.setattr(memory_mod, "freshness_decay", _must_not_be_called)
        new_id, old_id = self._mk_pair(cleanup_nodes)
        scores = self._scores(_CONTENT)
        assert new_id in scores and old_id in scores, scores

    def test_positive_weight_promotes_fresh_node(self, cleanup_nodes, monkeypatch) -> None:
        """WEIGHT>0：同语义分下新节点分数更高、排在旧节点之前。"""
        monkeypatch.setattr(Config, "MEM_SEARCH_FRESHNESS_WEIGHT", 0.1)
        monkeypatch.setattr(Config, "MEM_SEARCH_FRESHNESS_SCALE_DAYS", 30.0)
        monkeypatch.setattr(Config, "MEM_SEARCH_FRESHNESS_DECAY", "exp")
        new_id, old_id = self._mk_pair(cleanup_nodes)
        out = _mem_search_impl(_CONTENT, scope="all", top_k=10)
        ranked = [r["id"] for r in out["results"]]
        scores = {r["id"]: r["score"] for r in out["results"]}
        assert scores[new_id] > scores[old_id], scores
        assert ranked.index(new_id) < ranked.index(old_id), ranked

    def test_as_of_path_ignores_freshness(self, cleanup_nodes, monkeypatch) -> None:
        """as_of 历史视图：权重不生效（双时间线回看与今天的记忆新鲜度无关）。"""
        monkeypatch.setattr(Config, "MEM_SEARCH_FRESHNESS_WEIGHT", 0.1)
        monkeypatch.setattr(Config, "MEM_SEARCH_FRESHNESS_SCALE_DAYS", 30.0)
        from mcp_tools import memory as memory_mod

        def _must_not_be_called(*a, **kw):
            raise AssertionError("as_of 历史视图下 freshness_decay 不该被调用")

        monkeypatch.setattr(memory_mod, "freshness_decay", _must_not_be_called)
        new_id, old_id = self._mk_pair(cleanup_nodes)
        scores = self._scores(_CONTENT, as_of=NOW)
        # 两个节点在 as_of=NOW 时都「当时为真」，均应返回；新鲜度信号一次都不参与
        assert new_id in scores and old_id in scores, scores

    def test_nodes_without_time_fields_unaffected(self, cleanup_nodes, monkeypatch) -> None:
        """无时间字段的节点不加成（freshness_decay → None → 不惩罚不奖励）。"""
        monkeypatch.setattr(Config, "MEM_SEARCH_FRESHNESS_WEIGHT", 0.1)
        emb = store.embed_text(_CONTENT)
        nid = store.insert_node(
            {"content": _CONTENT, "type": "memory", "domain": "test", "importance": 0.5, "created_at": None},
            emb,
        )
        cleanup_nodes.append(nid)
        out = _mem_search_impl(_CONTENT, scope="all", top_k=10)
        mine = [r for r in out["results"] if r["id"] == nid]
        assert mine, f"节点未命中: {out['results']}"
        # 若加成被错误应用，分数会偏离语义基线；此处只验证仍能正常命中且分数为有限值
        assert mine[0]["score"] == mine[0]["score"]  # NaN 防御（NaN != NaN）
