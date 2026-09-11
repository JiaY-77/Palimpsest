"""Unit tests for eval/metrics.py — pure functions, no network/DB."""

from eval.metrics import mrr_at_k, ndcg_at_k, recall_at_k


class TestRecallAtK:
    def test_all_hit(self):
        assert recall_at_k([1, 2, 3], {1, 2}, 3) == 1.0

    def test_partial_hit(self):
        assert recall_at_k([1, 3, 5], {1, 2}, 3) == 0.5

    def test_no_hit(self):
        assert recall_at_k([10, 20], {1, 2}, 5) == 0.0

    def test_gold_empty(self):
        assert recall_at_k([1, 2], set(), 5) == 0.0

    def test_ranked_empty(self):
        assert recall_at_k([], {1, 2}, 5) == 0.0

    def test_k_zero(self):
        assert recall_at_k([1, 2], {1}, 0) == 0.0

    def test_k_smaller_than_ranked(self):
        assert recall_at_k([1, 2, 3, 4, 5], {4, 5}, 3) == 0.0

    def test_single_gold_hit(self):
        assert recall_at_k([9, 1, 8], {1}, 5) == 1.0

    def test_duplicate_ids_in_ranked(self):
        assert recall_at_k([1, 1, 2], {1, 2}, 3) == 1.0


class TestMRRAtK:
    def test_first_hit(self):
        assert mrr_at_k([1, 2, 3], {1}, 3) == 1.0

    def test_second_hit(self):
        assert mrr_at_k([5, 2, 3], {2}, 3) == 0.5

    def test_third_hit(self):
        assert mrr_at_k([5, 6, 1], {1}, 3) == 1 / 3

    def test_no_hit(self):
        assert mrr_at_k([10, 20], {1, 2}, 5) == 0.0

    def test_gold_empty(self):
        assert mrr_at_k([1, 2], set(), 5) == 0.0

    def test_ranked_empty(self):
        assert mrr_at_k([], {1}, 5) == 0.0

    def test_k_zero(self):
        assert mrr_at_k([1], {1}, 0) == 0.0

    def test_k_limits_search(self):
        # gold is at index 3 (position 4), but k=3 so not found
        assert mrr_at_k([10, 20, 30, 1], {1}, 3) == 0.0

    def test_multiple_gold_first_wins(self):
        assert mrr_at_k([5, 2, 3], {2, 3}, 3) == 0.5


class TestNDCGAtK:
    def test_perfect_ranking(self):
        assert ndcg_at_k([1, 2], {1, 2}, 2) == 1.0

    def test_reversed_ranking(self):
        # DCG = 0.5/log2(2) + 1.0/log2(3) = 0.5 + 0.631 = 1.131
        # IDCG = 1.0/log2(2) + 0.5/log2(3) = 1.0 + 0.315 = 1.315
        result = ndcg_at_k([2, 1], {1}, 2, partial={2})
        assert 0.8 < result < 1.0

    def test_no_hit(self):
        assert ndcg_at_k([10, 20], {1, 2}, 5) == 0.0

    def test_gold_empty(self):
        assert ndcg_at_k([1, 2], set(), 5) == 0.0

    def test_ranked_empty(self):
        assert ndcg_at_k([], {1}, 5) == 0.0

    def test_k_zero(self):
        assert ndcg_at_k([1], {1}, 0) == 0.0

    def test_partial_relevance(self):
        # gold={1}, partial={2}
        # Ideal: [1.0] (only gold in top-1)
        # Actual: rank 1 has id=2 (partial, rel=0.5), so DCG=0.5, IDCG=1.0 => 0.5
        result = ndcg_at_k([2, 1], {1}, 1, partial={2})
        assert abs(result - 0.5) < 1e-9

    def test_partial_in_top_k(self):
        # gold={1}, partial={2,3}
        # top-3: [2, 3, 1] -> rels = [0.5, 0.5, 1.0]
        # DCG = 0.5/1 + 0.5/1.585 + 1.0/2 = 0.5 + 0.315 + 0.5 = 1.315
        # IDCG (ideal = [1.0, 0.5, 0.5]): 1.0/1 + 0.5/1.585 + 0.5/2 = 1.0 + 0.315 + 0.25 = 1.565
        result = ndcg_at_k([2, 3, 1], {1}, 3, partial={2, 3})
        expected_dcg = 0.5 / 1 + 0.5 / 1.585 + 1.0 / 2
        expected_idcg = 1.0 / 1 + 0.5 / 1.585 + 0.5 / 2
        assert abs(result - expected_dcg / expected_idcg) < 1e-6

    def test_binary_no_partial(self):
        # Standard binary nDCG: gold={1}, no partial
        # top-1: [1] -> rel=1.0, DCG=1.0, IDCG=1.0 => 1.0
        assert ndcg_at_k([1], {1}, 1, partial=None) == 1.0

    def test_single_gold_not_in_results(self):
        assert ndcg_at_k([5, 6, 7], {1}, 5, partial=None) == 0.0
