"""Offline evaluation metrics for retrieval quality."""

from __future__ import annotations

import math
from collections.abc import Sequence


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
        [1.0] * len(gold) + [0.5] * len(partial or set()),
        reverse=True,
    )[:k]
    dcg_val = _dcg(actual)
    idcg_val = _dcg(ideal)
    if idcg_val == 0.0:
        return 0.0
    return dcg_val / idcg_val


def auc_separability(
    pos: Sequence[float | None], neg: Sequence[float | None]
) -> float | None:
    """AUC 可分离度：P(随机正样本分数 > 随机负样本分数)，并列记 0.5。

    0.5 = 完全无法区分正负样本，1.0 = 完全可分（正样本分数全部高于负样本）。
    """
    pos_clean = [v for v in pos if v is not None]
    neg_clean = [v for v in neg if v is not None]
    if not pos_clean or not neg_clean:
        return None
    total = len(pos_clean) * len(neg_clean)
    wins = 0.0
    for p in pos_clean:
        for n in neg_clean:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / total
