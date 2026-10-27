"""core/confidence.py 与置信度重排的单元测试。"""

from __future__ import annotations

import pytest

from config import Config
from core import confidence
from core.trivium_store import _rerank_by_decay


class TestExtractEvidence:
    """证据提取：只认可观测信号，不认主观判断。"""

    def test_no_evidence(self) -> None:
        r, s, kinds = confidence.extract_evidence({"content": "今天天气不错"})
        assert (r, s, kinds) == (0.0, 0.0, [])

    def test_commit_hash(self) -> None:
        r, _, kinds = confidence.extract_evidence({"content": "修在 bb824d3 里"})
        assert r == pytest.approx(1.0)
        assert "commit" in kinds

    def test_pr_number(self) -> None:
        r, _, kinds = confidence.extract_evidence({"content": "见 PR #157"})
        assert r == pytest.approx(1.0)
        assert "commit" in kinds

    def test_source_path_from_payload(self) -> None:
        r, _, kinds = confidence.extract_evidence({"source_path": "a/b.md", "content": "x"})
        assert r == pytest.approx(0.9)
        assert kinds == ["source_path"]

    def test_git_commit_type(self) -> None:
        r, _, kinds = confidence.extract_evidence({"type": "git_commit", "commit_hash": "abc123", "content": "x"})
        assert r == pytest.approx(1.0)
        assert "git_commit" in kinds

    def test_testing_claim(self) -> None:
        r, _, kinds = confidence.extract_evidence({"content": "实测 10 并发全部 200"})
        assert r == pytest.approx(0.7)
        assert "tested" in kinds

    def test_superseded_penalty(self) -> None:
        _, s, kinds = confidence.extract_evidence({"content": "x", "status": "outdated"})
        assert s == pytest.approx(1.0)
        assert "superseded" in kinds

    def test_hedge_only_accepts_self_referential(self) -> None:
        """「可能」描述客观可能性，不是不确定信号（实测精确率仅 17%）。"""
        _, s1, k1 = confidence.extract_evidence({"content": "这个方案可能会慢"})
        assert s1 == 0.0
        assert "hedged" not in k1

        _, s2, k2 = confidence.extract_evidence({"content": "这一点待验证"})
        assert s2 == pytest.approx(0.3)
        assert "hedged" in k2


class TestConfidenceOf:
    """Subjective Logic 映射的边界行为。"""

    def test_no_evidence_means_unknown(self) -> None:
        c = confidence.confidence_of({"content": "x"})
        assert c["b"] == 0.0
        assert c["d"] == 0.0
        assert c["u"] == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "payload",
        [
            {"content": "x"},
            {"content": "实测 bb824d3"},
            {"content": "待验证", "status": "outdated"},
            {"source_path": "a.md", "content": "见 C:/x/y.txt"},
        ],
    )
    def test_components_sum_to_one(self, payload: dict) -> None:
        c = confidence.confidence_of(payload)
        assert c["b"] + c["d"] + c["u"] == pytest.approx(1.0)

    def test_formula_single_evidence(self) -> None:
        # r=1, s=0, W=2 → b=1/3, d=0, u=2/3
        c = confidence.confidence_of({"content": "bb824d3"})
        assert c["b"] == pytest.approx(1 / 3)
        assert c["d"] == pytest.approx(0.0)
        assert c["u"] == pytest.approx(2 / 3)

    def test_evidence_names_recorded(self) -> None:
        c = confidence.confidence_of({"content": "bb824d3 见 C:/a/b.txt"})
        assert set(c["evidence"]) == {"commit", "path"}


class TestNormalized:
    """固定线性映射到 [-1, 1]。"""

    def test_zero_b_maps_to_minus_one(self) -> None:
        assert confidence.normalized({"content": "x"}, 0.27, 0.15) == pytest.approx(-1.0)

    def test_strong_evidence_clips_to_one(self) -> None:
        payload = {
            "content": "bb824d3 C:/a/b.txt https://x.y node 12 实测",
            "source_path": "z.md",
        }
        assert confidence.normalized(payload, 0.27, 0.15) == pytest.approx(1.0)

    def test_zero_span_is_noop(self) -> None:
        assert confidence.normalized({"content": "bb824d3"}, 0.27, 0.0) == 0.0


class TestRerankIntegration:
    """置信度只做 ε 级 tie-break；默认关闭时行为不变。"""

    def test_disabled_by_default_keeps_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "MEMORY_CONFIDENCE_EPS", 0.0)
        scored = [
            (0.510, {"payload": {"content": "无证据", "created_at": 0}}),
            (0.505, {"payload": {"content": "实测 bb824d3", "created_at": 0}}),
        ]
        out = _rerank_by_decay(scored)
        assert max(out, key=lambda x: x[0])[1]["payload"]["content"] == "无证据"

    def test_enabled_can_flip_near_ties(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "MEMORY_CONFIDENCE_EPS", 0.05)
        monkeypatch.setattr(Config, "MEMORY_CONFIDENCE_CENTER", 0.27)
        monkeypatch.setattr(Config, "MEMORY_CONFIDENCE_SPAN", 0.15)
        monkeypatch.setattr(Config, "SOFT_RERANK_EPS", 0.0)
        monkeypatch.setattr(Config, "MEMORY_DECAY_FACTOR", 1.0)
        scored = [
            (0.500, {"payload": {"content": "无证据的一句话", "created_at": 0}}),
            (0.495, {"payload": {"content": "实测 bb824d3", "created_at": 0}}),
        ]
        out = _rerank_by_decay(scored)
        assert max(out, key=lambda x: x[0])[1]["payload"]["content"] == "实测 bb824d3"

    def test_enabled_does_not_override_clear_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """置信度不能压过明显的相关性差距——这是它只做 ε 项的原因。"""
        monkeypatch.setattr(Config, "MEMORY_CONFIDENCE_EPS", 0.05)
        monkeypatch.setattr(Config, "MEMORY_CONFIDENCE_CENTER", 0.27)
        monkeypatch.setattr(Config, "MEMORY_CONFIDENCE_SPAN", 0.15)
        monkeypatch.setattr(Config, "SOFT_RERANK_EPS", 0.0)
        monkeypatch.setattr(Config, "MEMORY_DECAY_FACTOR", 1.0)
        scored = [
            (0.900, {"payload": {"content": "高度相关但无证据", "created_at": 0}}),
            (0.100, {"payload": {"content": "实测 bb824d3", "created_at": 0}}),
        ]
        out = _rerank_by_decay(scored)
        assert max(out, key=lambda x: x[0])[1]["payload"]["content"] == "高度相关但无证据"
