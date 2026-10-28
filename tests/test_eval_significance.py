"""Unit tests for eval/significance.py and eval/split_eval_set.py — pure functions, no DB."""

import pytest

from eval.significance import _percentile, metric_fn, paired_bootstrap, per_query_scores
from eval.split_eval_set import temporal_split


def _mk_items(n: int, kind: str = "semantic", gold: bool = True) -> list[dict]:
    return [
        {
            "qid": f"q{i + 1:03d}",
            "query": f"query {i}",
            "kind": kind,
            "gold_ids": [100 + i] if gold else [],
            "layer": "kb",
        }
        for i in range(n)
    ]


class TestPairedBootstrapIdentical:
    def test_identical_scores_give_p_one(self):
        # 全等分数 → 差恒 0 → 观测差与重采样差无法区分 → p 应为 1
        result = paired_bootstrap([0.5] * 30, [0.5] * 30, n_resamples=200, seed=7)
        assert result is not None
        assert result["p_value"] == 1.0
        assert result["mean_diff"] == 0.0
        assert result["ci_low"] == 0.0 and result["ci_high"] == 0.0
        assert result["n"] == 30


class TestPairedBootstrapSystematicWin:
    def test_constant_win_gives_tiny_p(self):
        # A 每题都高 0.3（差无方差且均值远离 0）→ p 应取最小值量级
        result = paired_bootstrap([0.6] * 30, [0.3] * 30, n_resamples=200, seed=7)
        assert result is not None
        assert result["p_value"] < 0.01
        assert result["mean_diff"] == pytest.approx(0.3)
        # 差恒定 → 重采样分布退化 → CI 两端都在 0.3
        assert result["ci_low"] == pytest.approx(0.3)
        assert result["ci_high"] == pytest.approx(0.3)

    def test_direction_is_a_minus_b(self):
        result = paired_bootstrap([0.3] * 10, [0.6] * 10, n_resamples=200, seed=7)
        assert result is not None
        assert result["mean_diff"] == pytest.approx(-0.3)


class TestPairedBootstrapMixed:
    def test_mixed_scores_give_nondegenerate_ci(self):
        # 有方差的差：偶数题 +0.5，奇数题 -0.1 → 均值 0.2，重采样有真实波动
        diffs = [0.5 if i % 2 == 0 else -0.1 for i in range(40)]
        scores_b = [0.3] * 40
        scores_a = [b + d for b, d in zip(scores_b, diffs, strict=True)]
        result = paired_bootstrap(scores_a, scores_b, n_resamples=500, seed=7)
        assert result is not None
        assert 0.0 < result["p_value"] < 1.0
        assert result["ci_low"] <= result["mean_diff"] <= result["ci_high"]
        assert result["ci_low"] < result["ci_high"]

    def test_deterministic_given_seed(self):
        diffs = [0.5 if i % 2 == 0 else -0.1 for i in range(20)]
        a = [0.3 + d for d in diffs]
        b = [0.3] * 20
        r1 = paired_bootstrap(a, b, n_resamples=100, seed=13)
        r2 = paired_bootstrap(a, b, n_resamples=100, seed=13)
        assert r1 == r2


class TestPairedBootstrapEdges:
    def test_single_pair_returns_none(self):
        assert paired_bootstrap([0.5], [0.3], n_resamples=10, seed=1) is None

    def test_empty_returns_none(self):
        assert paired_bootstrap([], [], n_resamples=10, seed=1) is None

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="length mismatch"):
            paired_bootstrap([0.5, 0.6], [0.3], n_resamples=10, seed=1)


class TestPercentile:
    def test_single_element(self):
        assert _percentile([2.0], 50) == 2.0

    def test_median_of_sorted(self):
        assert _percentile([0.0, 1.0, 2.0, 3.0], 50) == 1.5

    def test_endpoints(self):
        vals = [1.0, 2.0, 3.0]
        assert _percentile(vals, 0) == 1.0
        assert _percentile(vals, 100) == 3.0


class TestMetricFn:
    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="metric must look like"):
            metric_fn("recall")

    def test_unknown_prefix_raises(self):
        with pytest.raises(ValueError, match="unsupported metric prefix"):
            metric_fn("precision@5")

    def test_recall5_resolves(self):
        fn = metric_fn("recall@5")
        assert fn([42, 1, 2, 3, 4], {42}) == 1.0
        assert fn([1, 2, 3, 4, 5], {42}) == 0.0


class TestPerQueryScores:
    def _items(self) -> dict[str, dict]:
        items = {
            "q1": {"qid": "q1", "kind": "semantic", "gold_ids": [7]},
            "q2": {"qid": "q2", "kind": "negative", "gold_ids": []},
            "q3": {"qid": "q3", "kind": "semantic", "gold_ids": []},
        }
        return items

    def test_negative_and_no_gold_skipped(self):
        results = {
            "q1": {"vec": {"ids": [7, 8], "scores": [0.9, 0.1]}},
            "q2": {"vec": {"ids": [1], "scores": [0.5]}},
            "q3": {"vec": {"ids": [1], "scores": [0.5]}},
        }
        scores = per_query_scores(results, self._items(), "vec", metric_fn("recall@1"))
        assert scores == {"q1": 1.0}

    def test_missing_mode_counts_as_empty_ranking(self):
        results = {"q1": {"other_mode": {"ids": [7]}}}
        scores = per_query_scores(results, self._items(), "vec", metric_fn("recall@1"))
        assert scores == {"q1": 0.0}


class TestTemporalSplit:
    def test_older_to_tune_newer_to_held_out(self):
        items = _mk_items(10)
        created = {100 + i: 1_000_000.0 + i for i in range(10)}  # 递增时间
        tune, held_out, manifest = temporal_split(items, created, tune_fraction=0.8)
        assert len(tune) == 8 and len(held_out) == 2
        # tune 里的时间全部 <= held-out 里的时间
        tune_ts = [created[g] for it in tune for g in it["gold_ids"]]
        held_ts = [created[g] for it in held_out for g in it["gold_ids"]]
        assert max(tune_ts) <= min(held_ts)
        assert manifest["boundary_created_at"] == max(tune_ts)

    def test_manifest_qids_partition_placed(self):
        items = _mk_items(10)
        created = {100 + i: 1_000_000.0 + (i % 3) * 100.0 for i in range(10)}
        tune, held_out, manifest = temporal_split(items, created, tune_fraction=0.8)
        tune_q = set(manifest["tune_qids"])
        held_q = set(manifest["held_out_qids"])
        assert not (tune_q & held_q)
        placed_q = {it["qid"] for it in items}
        assert tune_q | held_q == placed_q
        assert manifest["n_tune"] == len(tune)
        assert manifest["n_held_out"] == len(held_out)

    def test_negative_and_missing_timestamp_unplaced(self):
        items = _mk_items(4)
        items.append({"qid": "qN1", "kind": "negative", "gold_ids": [], "layer": "kb"})
        items.append({"qid": "q5", "kind": "semantic", "gold_ids": [999]})  # 无时间戳
        created = {100 + i: 1_000_000.0 + i for i in range(4)}
        tune, held_out, manifest = temporal_split(items, created, tune_fraction=0.5)
        qids = {it["qid"] for it in tune} | {it["qid"] for it in held_out}
        assert "qN1" not in qids and "q5" not in qids
        assert manifest["unplaced"] == {"negative_or_no_gold": 1, "missing_timestamp": 1}

    def test_multi_gold_uses_earliest(self):
        items = [{"qid": "q1", "kind": "semantic", "gold_ids": [1, 2]}]
        created = {1: 500.0, 2: 900.0}
        _, _, manifest = temporal_split(items, created, tune_fraction=1.0)
        # 1.0 fraction → 全部进 tune，boundary 应取该题的最早 gold 时间
        assert manifest["boundary_created_at"] == 500.0

    def test_ties_broken_by_qid(self):
        items = [{"qid": "qB", "kind": "semantic", "gold_ids": [2]}, {"qid": "qA", "kind": "semantic", "gold_ids": [1]}]
        created = {1: 1.0, 2: 1.0}  # 同一时间 → 按 qid 排序
        _, _, manifest = temporal_split(items, created, tune_fraction=0.5)
        assert manifest["tune_qids"] == ["qA"]

    def test_empty_items(self):
        tune, held_out, manifest = temporal_split([], {})
        assert tune == [] and held_out == []
        assert manifest["boundary_created_at"] is None
        assert manifest["n_placed"] == 0

    def test_fraction_floor(self):
        # 11 * 0.8 = 8.8 → floor 8
        items = _mk_items(11)
        created = {100 + i: 1_000_000.0 + i for i in range(11)}
        tune, held_out, manifest = temporal_split(items, created, tune_fraction=0.8)
        assert len(tune) == 8 and len(held_out) == 3
        assert manifest["n_tune"] == 8
