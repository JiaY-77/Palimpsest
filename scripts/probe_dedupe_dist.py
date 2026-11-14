"""只读探针：统计现有 active 记忆两两相似度分布，为写入去重阈值（dedupe gate）选型供数。

动机（issue #280）：``high_similar_unreplaced_rate`` 的判据是 ``related_count>0``，
而 related 的阈值只有 0.4 —— 该数字是「话题相关」不是「重复」，不能直接当
dedupe 阈值用。本脚本在库副本上量出真实分布（p50/p90/p95/p99/max + 各档占比），
阈值据此实测选定，不拍脑袋。

口径与 dedupe 闸门的候选过滤一致（core/conflict 的门规则）：
  - 节点池：status != "outdated"、type ∈ {memory, task, plan}（自动冲突判定的
    类型白名单）、有向量；
  - 计入分布的对：同 type（type 隔离）且通过 domain 隔离（双方非 general 且
    不同 → 跳过）。跨 type / 被隔离的对不在闸门视野内，不计入。
另附一份「同 type 全对（不做 domain 隔离）」对照分布，便于看隔离的影响。

只读保证：TriviumStore(read_only=True)，除打开句柄外不写任何库文件；
跑前跑后对副本文件组各记一次 SHA256，由调用方比对（应完全一致）。

用法（在库副本上，绝不对生产库跑）::

    DB_PATH=<副本绝对路径> PYTHONPATH=. python scripts/probe_dedupe_dist.py
    # 可选: --top 20 输出最高相似对样本数；--floor 0.5 只统计高于该值的对

★ 副本要求（实测踩坑）：必须**冷复制整文件组**并带 ``.flush_ok``——
``.db / .vec / .gidx / .pidx / .wal / .flush_ok`` 缺一个，TriviumDB 会以
「generation 不完整」或 os error 2 拒绝打开。服务正在写时直接 cp 会拿到
generation 不一致的半套文件，同样打不开；先停服务（或等它空闲）再复制。

输出：JSON 到 stdout（分布表 / 分位 / 各档占比 / top 对样本）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from config import Config
from core.trivium_store import node_domain

# 与 core.conflict 自动冲突判定一致的类型白名单（dedupe 候选池同源）
WHITELIST_TYPES = ("memory", "task", "plan")
BUCKETS = (0.7, 0.85, 0.92, 0.95)


def _collect(store) -> list[dict[str, Any]]:
    """收集探针节点池：active + 白名单类型 + 有向量。只读遍历。"""
    nodes: list[dict[str, Any]] = []
    for nid, node in store.iter_nodes():
        payload = node.get("payload") or {}
        if payload.get("type") not in WHITELIST_TYPES:
            continue
        if payload.get("status") == "outdated":
            continue
        vec = node.get("vector")
        if not vec:
            continue
        nodes.append({"id": nid, "type": payload.get("type", ""), "domain": node_domain(payload), "vector": vec})
    return nodes


def _pair_eligible(a: dict, b: dict, *, apply_domain_gate: bool) -> bool:
    """同 type 隔离 + （可选）domain 隔离——与 core.conflict._passes_gates 同规则。"""
    if a["type"] != b["type"]:
        return False
    if not apply_domain_gate:
        return True
    da, db_ = a["domain"], b["domain"]
    return not (da != db_ and da != "general" and db_ != "general")


def _cosine_matrix(vecs) -> Any:
    """归一化后点积 = 余弦相似度。向量按 f32 存、算 f64，结果 round 4 位。"""
    import numpy as np

    mat = np.asarray(vecs, dtype="float64")
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    unit = mat / norms
    return unit @ unit.T


def _distribution(scores_flat, floor: float) -> dict[str, Any]:
    """对一组两两相似度算分位与各档占比（floor 以下的对不计入）。"""
    import numpy as np

    arr = np.asarray([s for s in scores_flat if s > floor], dtype="float64")
    n = int(arr.size)
    out: dict[str, Any] = {
        "pairs_counted": n,
        "floor": floor,
        "p50": 0.0,
        "p90": 0.0,
        "p95": 0.0,
        "p99": 0.0,
        "max": 0.0,
        "bucket_share": {},
    }
    if n == 0:
        return out
    out["p50"] = round(float(np.percentile(arr, 50)), 4)
    out["p90"] = round(float(np.percentile(arr, 90)), 4)
    out["p95"] = round(float(np.percentile(arr, 95)), 4)
    out["p99"] = round(float(np.percentile(arr, 99)), 4)
    out["max"] = round(float(arr.max()), 4)
    for b in BUCKETS:
        out["bucket_share"][f">{b}"] = round(float((arr > b).mean()), 6)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=20, help="输出最高相似对样本条数")
    parser.add_argument("--floor", type=float, default=0.5, help="只统计高于该值的对（低于此值的对与闸门无关）")
    args = parser.parse_args()

    from core.trivium_store import TriviumStore

    store = TriviumStore(read_only=True)
    nodes = _collect(store)
    ids = [n["id"] for n in nodes]
    sim = _cosine_matrix([n["vector"] for n in nodes])
    n = len(nodes)

    gate_pairs: list[float] = []
    raw_pairs: list[float] = []
    top: list[dict[str, Any]] = []
    for i in range(n):
        for j in range(i + 1, n):
            score = float(sim[i, j])
            raw_pairs.append(score)
            if _pair_eligible(nodes[i], nodes[j], apply_domain_gate=True):
                gate_pairs.append(score)
                top.append({"a": ids[i], "b": ids[j], "score": round(score, 4), "type": nodes[i]["type"]})

    top.sort(key=lambda x: x["score"], reverse=True)

    result = {
        "db_path": os.path.abspath(Config.DB_PATH),
        "read_only": True,
        "nodes_scanned": n,
        "by_type": {t: sum(1 for x in nodes if x["type"] == t) for t in WHITELIST_TYPES},
        "gate_eligible_pairs": _distribution(gate_pairs, args.floor),
        "same_type_all_pairs": _distribution(raw_pairs, args.floor),
        "top_pairs": [{"a": t["a"], "b": t["b"], "score": t["score"], "type": t["type"]} for t in top[: args.top]],
        "note": "top_pairs 仅给 id/分值，正文摘录见人工核对；本脚本只读，不写库文件。",
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
