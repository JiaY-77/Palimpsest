# -*- coding: utf-8 -*-
"""
A1 一致性对比测试（阶段 2 第一步）
=====================================
目标：对比「现状 Python BFS 图谱扩散检索」与「目标 triviumdb 0.8.3 原生
search_advanced（SA-PPR 认知管线）」两条路径在同一数据 + 同一查询下的
召回/排序差异，确认差异可解释（或 diff=0），为改造选参数。

对比对象：
  路径 A（现状）：db.search(候选) + 时间衰减 + Python BFS 扩散
                  —— 忠实复刻 core/trivium_store.py 的 search_similar(279-357)
                     与 _expand_neighbors(359-420) 完整逻辑。
  路径 B（目标）：db.search_advanced(query, top_k, expand_depth=N,
                 min_score=0.0, enable_advanced_pipeline=True,
                 max_edges_per_node=20, min_edge_weight=0.0, teleport_alpha=…)

硬约束：
  - 不碰正式库：本脚本只连接 %TEMP%/tdb_ftest/a1_consistency/ 下的临时库。
    注意：core.TriviumStore.__init__ 会调 _init_indexes 打开 Config.DB_PATH
    （正式库），因此本脚本不实例化 TriviumStore，而是用裸 triviumdb 连接
    + 忠实复刻其 search 管线逻辑，彻底隔离正式库。
  - 不改生产代码。
  - 测完清理临时目录。

运行：./venv/Scripts/python.exe scripts/tdb_stress/_a1_consistency_test.py
"""

import hashlib
import math
import os
import random
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import triviumdb  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（与生产 Config 对齐）
# ---------------------------------------------------------------------------
DIM = 8
MEMORY_DECAY_FACTOR = 0.95
EXPAND_MAX_EDGES_PER_NODE = 20
EXPAND_MIN_EDGE_WEIGHT = 0.0
TOP_K = 20  # 对比规模：取 top_k=20 以便观察扩散差异

BASE_DIR = os.path.join(tempfile.gettempdir(), "tdb_ftest", "a1_consistency")
DB_PATH = os.path.join(BASE_DIR, "a1.db")

DOMAINS = ["task", "kb", "rule", "hermes", "general"]


# ---------------------------------------------------------------------------
# 确定性 fake embedder（思路同 tests/conftest.py 的 n-gram 向量，降维到 8）
# ---------------------------------------------------------------------------
def _fake_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    if not text:
        return vec
    padded = " " + text + " "
    for i in range(len(padded) - 1):
        gram = padded[i:i + 2]
        h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:4], 16)
        vec[h % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _days_since_created(created_at, now):
    try:
        ts = float(created_at)
    except (TypeError, ValueError):
        return 0.0
    if ts <= 0:
        return 0.0
    return max(0.0, (now - ts) / 86400.0)


def _to_float(v, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # NaN -> default


# ---------------------------------------------------------------------------
# 建库：300-500 节点 + 随机边 + 枢纽节点
# ---------------------------------------------------------------------------
def build_graph():
    if os.path.isdir(BASE_DIR):
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    os.makedirs(BASE_DIR, exist_ok=True)

    rng = random.Random(20260901)
    now = time.time()
    db = triviumdb.TriviumDB(DB_PATH, dim=DIM)

    n_nodes = 420
    # 节点文本主题词库（含语义簇：制造易被检索命中的聚类）
    clusters = {
        "memory": ["memory recall", "retention", "forgetting", "hippocampus"],
        "graph": ["graph diffusion", "bfs walk", "graph traversal", "hub topology"],
        "search": ["similarity search", "vector search", "ranking", "semantic search"],
        "social": ["friend network", "social graph", "influence", "community"],
        "tech": ["python code", "api design", "embedding model", "database index"],
    }
    cluster_weights = list(clusters.keys())
    vectors = []
    payloads = []
    hub_ids = set()

    # 先插入节点
    for i in range(1, n_nodes + 1):
        domain = DOMAINS[rng.randrange(len(DOMAINS))]
        cw = cluster_weights[rng.randrange(len(cluster_weights))]
        w = cluster_weights[rng.randrange(len(cluster_weights))]
        # 文本 = 主词 + 随机词，相似主题→相似 n-gram 向量
        text = " ".join(
            [clusters[cw][rng.randrange(len(clusters[cw]))],
             clusters[w][rng.randrange(len(clusters[w]))],
             f"node {i}"]
        )
        vec = _fake_embed(text)
        imp = round(rng.uniform(0.1, 1.0), 3)
        created = now - rng.uniform(0, 90) * 86400.0  # 0~90 天前
        payload = {
            "type": "kb_chunk" if domain == "kb" and rng.random() < 0.4 else "memory",
            "domain": domain,
            "importance": imp,
            "created_at": round(created, 3),
            "num": i,
            "label": text[:40],
        }
        vectors.append([v + rng.uniform(-0.01, 0.01) for v in vec])
        payloads.append(payload)

    db.batch_insert_with_ids(list(range(1, n_nodes + 1)), vectors, payloads)

    # 挑 12 个枢纽节点（高连接度，模拟真实记忆网的记忆锚点）
    hub_ids = set(rng.sample(list(range(1, n_nodes + 1)), 12))

    # 随机边：普通节点 2~6 条，枢纽节点 25~70 条
    linked = 0
    for src in range(1, n_nodes + 1):
        if src in hub_ids:
            tgt_count = rng.randrange(25, 70)
        else:
            tgt_count = rng.randrange(2, 6)
        tgts = set()
        attempts = 0
        while len(tgts) < tgt_count and attempts < 200:
            attempts += 1
            dst = rng.randrange(1, n_nodes + 1)
            if dst == src or dst in tgts:
                continue
            tgts.add(dst)
            db.link(src, dst, label="REL", weight=round(rng.uniform(0.1, 1.0), 3))
            linked += 1

    info = {
        "nodes": db.node_count(),
        "hub_ids": sorted(hub_ids),
        "edges": linked,
        "domains": {},
    }
    for dom in DOMAINS:
        info["domains"][dom] = len(
            [p for p in payloads if p["domain"] == dom])

    # 抽取若干代表性查询向量（覆盖各语义簇，直接从节点向量去噪）
    queries = {}
    for name, words in clusters.items():
        q = _fake_embed(" ".join(words) + f" {name} query")
        queries[name] = q
    db.close()
    return info, queries


# ---------------------------------------------------------------------------
# 路径 A：忠实复刻 TriviumStore.search_similar + _expand_neighbors
# ---------------------------------------------------------------------------
def path_a_search(db, query_vec, top_k, expand_depth, apply_decay=True,
                  block=""):
    cand_k = max(top_k * 3, 10)
    hits = db.search(query_vec, top_k=cand_k, min_score=0.0, expand_depth=0)
    scored = [(float(h.score), {"id": h.id, "payload": h.payload})
              for h in (hits or [])]

    if apply_decay:
        now = time.time()
        for i, (score, node) in enumerate(scored):
            payload = node.get("payload", {}) or {}
            if payload.get("type") == "kb_chunk":
                continue
            days = _days_since_created(payload.get("created_at"), now)
            importance = _to_float(payload.get("importance"), 0.5)
            scored[i] = (score * importance * (MEMORY_DECAY_FACTOR ** (days / 30.0)),
                         node)

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    if expand_depth > 0:
        top = expand_neighbors(db, top, depth=expand_depth,
                               max_edges_per_node=EXPAND_MAX_EDGES_PER_NODE,
                               min_edge_weight=EXPAND_MIN_EDGE_WEIGHT,
                               block=block)

    return [
        {"id": node.get("id"), "score": score, "payload": node.get("payload", {})}
        for score, node in top[:top_k]
    ]


def expand_neighbors(db, top, depth, max_edges_per_node, min_edge_weight, block=""):
    max_edges = max_edges_per_node
    min_w = min_edge_weight
    merged = {node.get("id"): (score, node) for score, node in top}
    seen = set(merged.keys())
    frontier = [(score, node.get("id"), 0) for score, node in top]
    while frontier:
        score, nid, hop = frontier.pop(0)
        if hop >= depth:
            continue
        edges = list(db.get_edges(nid))
        edges = [e for e in edges
                 if float(getattr(e, "weight", 1.0) or 1.0) >= min_w]
        edges.sort(key=lambda e: float(getattr(e, "weight", 1.0) or 1.0),
                   reverse=True)
        edges = edges[:max_edges]
        for edge in edges:
            w = float(getattr(edge, "weight", 1.0) or 1.0)
            nb = edge.target_id
            if nb in seen:
                continue
            if block:
                nb_node = db.get(nb)
                if not nb_node:
                    continue
                nb_payload = nb_node.payload or {}
                nb_domain = (nb_payload.get("domain")
                             or nb_payload.get("character_name")
                             or "general").strip().lower()
                b = block.strip().lower()
                if b:
                    if nb_domain != b and not (b == "kb" and nb_domain == "rule"):
                        continue
            else:
                nb_node = db.get(nb)
                if not nb_node:
                    continue
            seen.add(nb)
            nb_score = score * w
            merged[nb] = (nb_score, {
                "id": nb_node.id,
                "payload": nb_node.payload,
                "num_edges": nb_node.num_edges,
                "vector": nb_node.vector,
            })
            frontier.append((nb_score, nb, hop + 1))
    return sorted(merged.values(), key=lambda x: x[0], reverse=True)


# ---------------------------------------------------------------------------
# 路径 B：db.search_advanced
# ---------------------------------------------------------------------------
def path_b_search(db, query_vec, top_k, expand_depth, teleport_alpha):
    hits = db.search_advanced(
        query_vec,
        top_k=top_k,
        recall_k=0,
        rerank_k=0,
        expand_depth=expand_depth,
        min_score=0.0,
        teleport_alpha=teleport_alpha,
        enable_advanced_pipeline=True,
        max_edges_per_node=EXPAND_MAX_EDGES_PER_NODE,
        min_edge_weight=EXPAND_MIN_EDGE_WEIGHT,
        edge_direction="out",
    )
    return [
        {"id": h.id, "score": float(h.score), "payload": h.payload}
        for h in (hits or [])
    ]


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def jaccard(ids_a, ids_b):
    a, b = set(ids_a), set(ids_b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def spearman(a, b):
    """公共子序列的排序相关性（1=同序，-1=逆序）。"""
    a_list, b_list = list(a), list(b)
    a_pos = {x: i for i, x in enumerate(a_list)}
    b_pos = {x: i for i, x in enumerate(b_list)}
    common = [x for x in a_list if x in b_pos]
    if len(common) < 2:
        return 0.0
    a_ranks = [a_pos[x] for x in common]
    b_ranks = [b_pos[x] for x in common]
    n = len(common)
    ma, mb = sum(a_ranks) / n, sum(b_ranks) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a_ranks, b_ranks))
    va = sum((x - ma) ** 2 for x in a_ranks)
    vb = sum((y - mb) ** 2 for y in b_ranks)
    if va == 0 or vb == 0:
        return 0.0
    return cov / (math.sqrt(va) * math.sqrt(vb))


def pos_drift(a_ids, b_ids):
    """b 相对 a 的位置漂移均值（公共 id 在两侧位置差绝对值均值）。"""
    a_pos = {x: i for i, x in enumerate(a_ids)}
    b_pos = {x: i for i, x in enumerate(b_ids)}
    common = [x for x in a_ids if x in b_pos]
    if not common:
        return 0.0, 0
    drift = sum(abs(a_pos[x] - b_pos[x]) for x in common) / len(common)
    return drift, len(common)


def payload_summary(p):
    p = p or {}
    return p.get("label") or p.get("text") or f"#{p.get('num')}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    print("=" * 72)
    print("A1 一致性对比测试：现状 Python BFS vs 目标 search_advanced (SA-PPR)")
    print("=" * 72)

    info, queries = build_graph()
    print(f"\n临时库: {DB_PATH}")
    print(f"节点: {info['nodes']}  边: {info['edges']}  枢纽: {len(info['hub_ids'])}")
    print(f"区块分布: {info['domains']}")

    db = triviumdb.TriviumDB(DB_PATH, dim=DIM)

    depths = [1, 2, 3]
    alphas = [0.0, 0.1, 0.15]
    query_names = list(queries.keys())

    results = {}  # (depth, alpha) -> list of per-query metric dicts

    for depth in depths:
        for alpha in alphas:
            per_q = []
            for qname in query_names:
                q = queries[qname]
                res_a = path_a_search(db, q, TOP_K, depth)
                res_b = path_b_search(db, q, TOP_K, depth, alpha)
                ids_a = [r["id"] for r in res_a]
                ids_b = [r["id"] for r in res_b]
                jac = jaccard(ids_a, ids_b)
                rho = spearman(ids_a, ids_b)
                drift, ncommon = pos_drift(ids_a, ids_b)
                per_q.append({
                    "q": qname,
                    "jac": jac, "rho": rho, "drift": drift, "ncommon": ncommon,
                    "ids_a": ids_a, "ids_b": ids_b,
                    "res_a": res_a, "res_b": res_b,
                })
            avg_j = sum(x["jac"] for x in per_q) / len(per_q)
            avg_rho = sum(x["rho"] for x in per_q) / len(per_q)
            avg_drift = sum(x["drift"] for x in per_q) / len(per_q)
            results[(depth, alpha)] = {
                "per_q": per_q,
                "avg_jac": avg_j, "avg_rho": avg_rho, "avg_drift": avg_drift,
            }
            print(f"\n---- expand_depth={depth} teleport_alpha={alpha} ----")
            print(f"  Jaccard 召回重合率: {avg_j:.3f}   Spearman 排序: {avg_rho:+.3f}"
                  f"  位置漂移均值: {avg_drift:.2f}")
            for x in per_q:
                print(f"    [{x['q']:8}] J={x['jac']:.3f} rho={x['rho']:+.3f} "
                      f"drift={x['drift']:.2f} 公共={x['ncommon']}/{TOP_K}")

    # ------------------------------------------------------------------
    # 汇总对比表
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("汇总对比表：expand_depth × teleport_alpha")
    print("=" * 72)
    hdr = f"{'depth':>6} | {'alpha':>6} | {'Jaccard':>8} | {'Spearman':>9} | {'漂移':>6}"
    print(hdr)
    print("-" * len(hdr))
    best_key = None
    best_j = -1
    for depth in depths:
        for alpha in alphas:
            r = results[(depth, alpha)]
            print(f"{depth:>6} | {alpha:>6.2f} | {r['avg_jac']:>8.3f} | "
                  f"{r['avg_rho']:>+9.3f} | {r['avg_drift']:>6.2f}")
            if r["avg_jac"] > best_j:
                best_j = r["avg_jac"]
                best_key = (depth, alpha)
    print(f"\n与现状最接近（Jaccard 最高）: depth={best_key[0]}, alpha={best_key[1]} "
          f"(J={best_j:.3f})")

    # ------------------------------------------------------------------
    # 差异节点举例 + 结论（选 Jaccard 非 1.0 的一档，且 max drift 的 query）
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("差异节点示例（选取差异最大的一档/一条查询）")
    print("=" * 72)
    shown = 0
    for depth in depths:
        for alpha in alphas:
            r = results[(depth, alpha)]
            for x in sorted(r["per_q"], key=lambda z: z["jac"]):
                ids_a = set(x["ids_a"])
                ids_b = set(x["ids_b"])
                only_a = [i for i in x["ids_a"] if i not in ids_b]
                only_b = [i for i in x["ids_b"] if i not in ids_a]
                if not only_a and not only_b:
                    continue
                print(f"\n[d={depth} alpha={alpha} q={x['q']} J={x['jac']:.2f}]")
                print(f"  仅出现在 A(BFS): {len(only_a)}  仅出现在 B(SA-PPR): {len(only_b)}")
                for i in only_a[:4]:
                    node = next((z for z in x["res_a"] if z["id"] == i), None)
                    if node:
                        print(f"    A-only #{i}  score={node['score']:.4f}  "
                              f"hub={i in info['hub_ids']}  {payload_summary(node['payload'])[:40]}")
                for i in only_b[:4]:
                    node = next((z for z in x["res_b"] if z["id"] == i), None)
                    if node:
                        print(f"    B-only #{i}  score={node['score']:.4f}  "
                              f"hub={i in info['hub_ids']}  {payload_summary(node['payload'])[:40]}")
                shown += 1
                if shown >= 3:
                    break
            if shown >= 3:
                break
        if shown >= 3:
            break

    # ------------------------------------------------------------------
    # 结论
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    hub_bias = {}
    for depth in ([best_key[0]] if best_key else depths):
        for alpha in ([best_key[1]] if best_key else alphas):
            r = results[(depth, alpha)]
            for x in r["per_q"]:
                only_b = [i for i in x["ids_b"] if i not in set(x["ids_a"])]
                only_a = [i for i in x["ids_a"] if i not in set(x["ids_b"])]
                hub_b = sum(1 for i in only_b if i in info["hub_ids"])
                hub_a = sum(1 for i in only_a if i in info["hub_ids"])
                if hub_b or hub_a:
                    print(f"  [d={depth} a={alpha} q={x['q']}] "
                          f"B-only 中枢纽节点 {hub_b}/{len(only_b)}  "
                          f"A-only 中枢纽节点 {hub_a}/{len(only_a)}")
    print("\n  结论要点：")
    print(f"  - 与现状最接近档位: expand_depth={best_key[0]}, teleport_alpha={best_key[1]} "
          f"(Jaccard={best_j:.3f})")
    r_best = results[best_key]
    hub_bias_total = sum(
        1 for x in r_best["per_q"]
        for i in x["ids_b"] if i not in set(x["ids_a"]) and i in info["hub_ids"]
    )
    only_b_total = sum(
        1 for x in r_best["per_q"]
        for i in x["ids_b"] if i not in set(x["ids_a"])
    )
    if only_b_total:
        print(f"  - SA-PPR 在最佳档位下新引入节点中枢纽占比: "
              f"{hub_bias_total}/{only_b_total} = "
              f"{hub_bias_total / only_b_total * 100:.1f}%")
    else:
        print("  - SA-PPR 未引入新节点（与现状 top_k 完全重合）")

    db.close()
    # 清理临时目录
    try:
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    except Exception:
        pass
    print("\n临时目录已清理:", BASE_DIR)


if __name__ == "__main__":
    main()
