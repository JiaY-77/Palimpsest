"""Offline evaluation metrics for retrieval quality."""

from __future__ import annotations

import math


def recall_at_k(ranked_ids: list[int], gold: set[int], k: int) -> float:
    if not gold or k <= 0:
        return 0.0
    top = ranked_ids[:k]
    hits = sum(1 for gid in gold if gid in top)
    return hits / len(gold)


def mrr_at_k(ranked_ids: list[int], gold: set[int], k: int) -> float:
    if not gold or k <= 0:
        return 0.0
    for i, rid in enumerate(ranked_ids[:k], start=1):
        if rid in gold:
            return 1.0 / i
    return 0.0


def _dcg(relevances: list[float]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


def ndcg_at_k(
    ranked_ids: list[int],
    gold: set[int],
    k: int,
    partial: set[int] | None = None,
) -> float:
    if not gold or k <= 0:
        return 0.0
    top = ranked_ids[:k]
    rel_map: dict[int, float] = {}
    for rid in top:
        if rid in gold:
            rel_map[rid] = 1.0
        elif partial and rid in partial:
            rel_map[rid] = 0.5
        else:
            rel_map[rid] = 0.0

    actual = [rel_map[rid] for rid in top]
    ideal = sorted(
        [1.0] * len(gold) + [0.5] * len(partial if partial else set()),
        reverse=True,
    )[:k]
    dcg_val = _dcg(actual)
    idcg_val = _dcg(ideal)
    if idcg_val == 0.0:
        return 0.0
    return dcg_val / idcg_val
