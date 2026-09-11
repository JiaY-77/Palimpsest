"""索引期 small-to-big 的离线可行性探针（块级召回 → 节点级返回）。

与「检索期近似分块」（把块分塞回节点空间再比，已系统否证，见 eval/docs/09-11）不同：
索引期方案让**块作为独立实体参与检索竞争**，命中后返回其父节点。块与块之间分数天然可比，
不需要跨尺度校准。本探针不动生产索引，在同一份库副本快照上测三件事，用数据判断是否值得上生产：

  1) 召回上限：块级检索 top-K 块 → 聚合到父节点（取最大块分）→ 与节点级整条向量对比
     （同时报告「gold 是否至少有一个块进入 top-K 块集」的块级召回上限）
  2) 成本：全库块数、块向量构建耗时、向量矩阵体积、按现库每节点开销外推的索引体积
  3) 延迟：节点级 vs 块级的检索耗时（numpy 暴力点积，同口径对比）

口径：与 chunk_norm_probe.py 一致——同库副本、同题集（eval/eval_set.json 正样本）、
      chunk=400 / overlap=100、同一 embedding 模型（Config.OLLAMA_EMBEDDING_MODEL）。

用法：
  venv/Scripts/python.exe scripts/chunk_index_probe.py [--limit N] [--topk 30,60,180] [--rebuild]
输出：eval/.tmp/chunk_index_probe.json
缓存：eval/.tmp/chunk_index_vectors.npz（块向量 + 父节点映射，--rebuild 强制重建）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

EVAL_DIR = ROOT / "eval"
TMP = EVAL_DIR / ".tmp"
TMP.mkdir(parents=True, exist_ok=True)

_env_db = os.getenv("DB_PATH", "")
ORIG_DB = (Path(_env_db) if os.path.isabs(_env_db) else ROOT / _env_db) if _env_db \
    else ROOT / "data" / "mh_memory.db"
ORIG_FTS = ORIG_DB.parent / "fts.db"

VECTOR_CACHE = TMP / "chunk_index_vectors.npz"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# ---- 库副本保护：复制真库副本，再 import Config（Config.DB_PATH 必须指向副本）----
pre_sha = sha256(ORIG_DB)
for p in sorted(ORIG_DB.parent.iterdir()):
    if p.name.startswith(ORIG_DB.name) and p.is_file():
        try:
            shutil.copy2(p, TMP / p.name)
        except Exception as e:  # noqa: BLE001 —— 库副本复制失败仅告警后续校验兜底
            print(f"  [warn] 复制 {p.name} 失败（忽略）: {e}")
if ORIG_FTS.exists():
    try:
        shutil.copy2(ORIG_FTS, TMP / ORIG_FTS.name)
    except Exception as e:  # noqa: BLE001 —— FTS 副本复制失败仅告警后续校验兜底
        print(f"  [warn] 复制 fts.db 失败（忽略）: {e}")
os.environ["DB_PATH"] = str(TMP / ORIG_DB.name)

from metrics import mrr_at_k, recall_at_k  # noqa: E402

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402

MODEL = Config.OLLAMA_EMBEDDING_MODEL


def unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def node_domain(payload: dict) -> str:
    return (payload.get("domain", "")
            or payload.get("character_name", "") or "general").strip().lower()


def embed_batch(texts: list[str], batch: int = 64) -> np.ndarray:
    """批量嵌入（ollama /api/embed，分批避免单请求过大）；失败则逐条回退。"""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/")
    model = Config.OLLAMA_EMBEDDING_MODEL
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        try:
            r = requests.post(f"{url}/api/embed", json={"model": model, "input": part},
                              timeout=600)
            r.raise_for_status()
            vecs = r.json().get("embeddings")
            if not vecs or len(vecs) != len(part):
                raise RuntimeError("embed 返回数量不符")
            out.extend(vecs)
        except Exception:  # noqa: BLE001 —— 批量嵌入失败逐条回退保证部分结果
            for t in part:
                rr = requests.post(f"{url}/api/embeddings",
                                   json={"model": model, "prompt": t[:2500]}, timeout=180)
                rr.raise_for_status()
                out.append(rr.json()["embedding"])
        if (i // batch) % 10 == 0:
            print(f"    嵌入 {min(i + batch, len(texts))}/{len(texts)}", flush=True)
    return np.asarray(out, dtype=np.float32)


def chunks(text: str, size: int, overlap: int) -> list[str]:
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    return [text[i:i + size] for i in range(0, len(text), step) if text[i:i + size].strip()]


def dir_size_mb(paths: list[Path]) -> float:
    return sum(p.stat().st_size for p in paths if p.exists()) / (1024 * 1024)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=400)
    ap.add_argument("--overlap", type=int, default=100)
    ap.add_argument("--topk", type=str, default="30,60,180")
    ap.add_argument("--rebuild", action="store_true", help="强制重建块向量缓存")
    ap.add_argument("--node-limit", type=int, default=0,
                    help="只取前 N 个节点建块索引（仅用于快速验证脚本逻辑）")
    args = ap.parse_args()
    topks = [int(x) for x in args.topk.split(",") if x.strip()]

    store = TriviumStore()

    # ---- 全库节点（物化，避免遍历期间占用连接）----
    t_load = time.time()
    all_nodes = list(store.iter_nodes())
    active = [(nid, n) for nid, n in all_nodes
              if (n["payload"].get("status") or "active") != "outdated"
              and (n["payload"].get("content") or "").strip()]
    if args.node_limit:
        active = active[: args.node_limit]
    nids = [nid for nid, _ in active]
    texts = [(n["payload"].get("content") or "") for _, n in active]
    doms = [node_domain(n["payload"]) for _, n in active]
    print(f"库内节点 {len(all_nodes)} 个；active 且有正文 {len(active)} 个；"
          f"正文总字数 {sum(len(t) for t in texts)}（载入 {time.time() - t_load:.1f}s）")
    print(f"domain 分布: {dict(Counter(doms).most_common())}")

    # ---- 节点级整条向量（base 对照，同 chunk_norm_probe 口径：content[:4000] 重嵌入）----
    t0 = time.time()
    node_vecs = unit(embed_batch([t[:4000] for t in texts]))
    node_embed_s = time.time() - t0
    print(f"节点级嵌入完成：{node_vecs.shape}，耗时 {node_embed_s:.1f}s")

    # ---- 块级向量（缓存）----
    if VECTOR_CACHE.exists() and not args.rebuild:
        z = np.load(VECTOR_CACHE)
        bvecs, owner = z["vecs"], z["owner"]
        chunk_s = float(z["build_s"][0])
        print(f"块向量缓存命中：{bvecs.shape}（构建耗时记录 {chunk_s:.1f}s）")
    else:
        t0 = time.time()
        all_chunks, owner_list = [], []
        for i, t in enumerate(texts):
            cs = chunks(t, args.chunk, args.overlap)
            all_chunks.extend(cs)
            owner_list.extend([i] * len(cs))
        print(f"切块完成：{len(all_chunks)} 块（chunk={args.chunk} overlap={args.overlap}）")
        bvecs = unit(embed_batch(all_chunks))
        owner = np.asarray(owner_list, dtype=np.int32)
        chunk_s = time.time() - t0
        np.savez_compressed(VECTOR_CACHE, vecs=bvecs, owner=owner,
                            build_s=np.asarray([chunk_s]))
        print(f"块向量构建完成：{bvecs.shape}，耗时 {chunk_s:.1f}s")

    # ---- 成本面 ----
    raw_mb = bvecs.nbytes / (1024 * 1024)
    cache_mb = VECTOR_CACHE.stat().st_size / (1024 * 1024)
    db_files = [p for p in ORIG_DB.parent.iterdir()
                if p.name.startswith(ORIG_DB.name) and p.is_file()]
    db_mb = dir_size_mb(db_files)
    per_node_kb = db_mb * 1024 / max(len(all_nodes), 1)
    proj_index_mb = per_node_kb * bvecs.shape[0] / 1024  # 按现库每节点开销外推
    print(f"\n成本：块向量矩阵 {raw_mb:.1f} MB（float32 原始）/ npz 缓存 {cache_mb:.1f} MB；"
          f"块数 {bvecs.shape[0]} = 节点数 ×{bvecs.shape[0] / max(len(active), 1):.2f}")
    print(f"      现库 {len(all_nodes)} 节点占 {db_mb:.1f} MB（{per_node_kb:.1f} KB/节点）→ "
          f"同口径块索引外推 ≈ {proj_index_mb:.1f} MB")
    print(f"      嵌入构建：节点级 {node_embed_s:.1f}s、块级 {chunk_s:.1f}s")

    # ---- 检索延迟 ----
    qv = unit(np.asarray(store.embed_text("测试查询 延迟测量"), dtype=np.float32)[None, :])[0]

    def timed(fn, n=20):
        fn()  # 预热
        t = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t) / n * 1000

    def node_search():
        s = node_vecs @ qv
        return np.argsort(-s)

    def chunk_search(k=180):
        s = bvecs @ qv
        top = np.argpartition(-s, min(k, len(s) - 1))[:k]
        order = top[np.argsort(-s[top])]
        best = {}
        for bi in order:
            o = int(owner[bi])
            if o not in best:
                best[o] = float(s[bi])
        return best

    lat_node = timed(node_search)
    lat_chunk180 = timed(lambda: chunk_search(180))
    print(f"\n延迟（numpy 暴力点积，20 次均值）：节点级 {lat_node:.2f} ms；"
          f"块级 top180 检索+聚合 {lat_chunk180:.2f} ms")

    # ---- 题集评估 ----
    items = json.loads((EVAL_DIR / "eval_set.json").read_text(encoding="utf-8"))["items"]
    pos = [it for it in items if it.get("kind") != "negative" and it.get("gold_ids")]
    if args.limit:
        pos = pos[: args.limit]
    id2idx = {nid: i for i, nid in enumerate(nids)}
    gold_domain = {}
    for it in pos:
        pid = id2idx.get(it["gold_ids"][0])
        gold_domain[it["qid"]] = doms[pid] if pid is not None else \
            (it.get("gold_domain") or "other").strip().lower()

    rows = []
    t0 = time.time()
    for k, it in enumerate(pos, 1):
        q = unit(np.asarray(store.embed_text(it["query"]), dtype=np.float32)[None, :])[0]
        ns = node_vecs @ q
        node_rank = [nids[i] for i in np.argsort(-ns)]
        cs = bvecs @ q
        kmax = max(topks)
        top = np.argpartition(-cs, min(kmax, len(cs) - 1))[:kmax]
        top = top[np.argsort(-cs[top])]
        chunk_ranks = {}
        ceil_hit = dict.fromkeys(topks, False)
        gold = set(it["gold_ids"])
        for rank, bi in enumerate(top, 1):
            o = int(owner[bi])
            if o not in chunk_ranks:
                chunk_ranks[o] = rank
            if nids[o] in gold:
                for K in topks:
                    if rank <= K:
                        ceil_hit[K] = True
        best = {}
        for K in topks:
            agg = {}
            for bi in top[:K]:
                o = int(owner[bi])
                agg.setdefault(o, float(cs[bi]))
            best[K] = [nids[o] for o in sorted(agg, key=lambda x: -agg[x])]
        rows.append({"qid": it["qid"], "gold": it["gold_ids"],
                     "gold_domain": gold_domain[it["qid"]],
                     "node": node_rank, "chunk_ceil": ceil_hit,
                     "chunk": best})
        if k % 20 == 0:
            print(f"  {k}/{len(pos)}  {time.time() - t0:.0f}s", flush=True)

    def metrics(ranked_fn):
        agg = defaultdict(list)
        dom = defaultdict(list)
        for r in rows:
            gold = set(r["gold"])
            ranked = ranked_fn(r)
            for k in (1, 3, 5, 10):
                agg[f"recall@{k}"].append(recall_at_k(ranked, gold, k))
            agg["mrr@10"].append(mrr_at_k(ranked, gold, 10))
            dom[r["gold_domain"]].append(recall_at_k(ranked, gold, 5))
        out = {m: sum(v) / len(v) for m, v in agg.items()}
        out["domain_r5"] = {d: sum(v) / len(v) for d, v in sorted(dom.items(),
                                                                 key=lambda x: -len(x[1]))}
        return out

    res = {"node": metrics(lambda r: r["node"])}
    for K in topks:
        res[f"chunk_top{K}"] = metrics(lambda r, K=K: r["chunk"][K])
    # 补充对照：
    #   node_candN  = 节点级只取前 N 个候选后排序（判断「候选预算」是否本身是瓶颈）
    #   chunkN_rerank = 块召回父节点集合、最终用整条余弦重排（块只当候选扩展器）
    res["node_cand30"] = metrics(lambda r: r["node"][:30])
    res["node_cand180"] = metrics(lambda r: r["node"][:180])
    for K in topks:
        res[f"chunk{K}_rerank"] = metrics(
            lambda r, K=K: [i for i in r["node"] if i in set(r["chunk"][K])])

    print("\n" + "=" * 84)
    print(f"索引期 small-to-big 可行性（{len(rows)} 题；块数 {bvecs.shape[0]}；chunk={args.chunk}"
          f"/{args.overlap}；model={MODEL}）")
    dom_order = list(res["node"]["domain_r5"].keys())
    hdr = f"{'method':<16} R@1    R@3    R@5    R@10   MRR   "
    for d in dom_order:
        hdr += f"{d[:5]:>7}"
    print(hdr)
    for key, m in res.items():
        line = (f"{key:<16} {m['recall@1']:.4f} {m['recall@3']:.4f} {m['recall@5']:.4f} "
                f"{m['recall@10']:.4f} {m['mrr@10']:.4f} ")
        for d in dom_order:
            line += f"{m['domain_r5'].get(d, 0):7.3f}"
        print(line)
    print("=" * 84)

    base_r5 = res["node"]["recall@5"]
    print("\nΔR@5 vs 节点级整条（pp）与块级召回上限：")
    for K in topks:
        m = res[f"chunk_top{K}"]
        ceil = sum(1 for r in rows if r["chunk_ceil"][K]) / len(rows)
        lost5 = sum(1 for r in rows
                    if recall_at_k(r["node"], set(r["gold"]), 5) == 1
                    and recall_at_k(r["chunk"][K], set(r["gold"]), 5) == 0)
        gain5 = sum(1 for r in rows
                    if recall_at_k(r["node"], set(r["gold"]), 5) == 0
                    and recall_at_k(r["chunk"][K], set(r["gold"]), 5) == 1)
        print(f"  chunk_top{K:<4} ΔR@5 全部 {100 * (m['recall@5'] - base_r5):+6.2f}pp"
              f" | kb {100 * (m['domain_r5'].get('kb', 0) - res['node']['domain_r5'].get('kb', 0)):+6.2f}pp"
              f" | hermes {100 * (m['domain_r5'].get('hermes', 0) - res['node']['domain_r5'].get('hermes', 0)):+6.2f}pp"
              f" | 逐题 +{gain5}/−{lost5}"
              f" | 块级召回上限(有块进 top{K}) {100 * ceil:.1f}%")
    print("\n说明：块级召回上限 = gold 节点至少有一个块进入 top-K 块集的比例；"
          "上限不高于节点级 R@5 时，方案在机制上就不成立。")

    post_sha = sha256(ORIG_DB)
    print(f"\n库完整性: 原库前 {pre_sha[:16]} 后 {post_sha[:16]} → "
          f"{'一致' if pre_sha == post_sha else '不一致'}")

    out = {
        "meta": {"chunk": args.chunk, "overlap": args.overlap, "topk": topks,
                 "model": MODEL, "n_questions": len(rows), "limit": args.limit,
                 "n_nodes_total": len(all_nodes), "n_nodes_active": len(active),
                 "n_chunks": int(bvecs.shape[0])},
        "cost": {"node_embed_s": round(node_embed_s, 1), "chunk_build_s": round(chunk_s, 1),
                 "matrix_mb": round(raw_mb, 1), "cache_mb": round(cache_mb, 1),
                 "db_mb": round(db_mb, 2), "per_node_kb": round(per_node_kb, 2),
                 "proj_index_mb": round(proj_index_mb, 1)},
        "latency_ms": {"node": round(lat_node, 2), "chunk_top180": round(lat_chunk180, 2)},
        "sha256": {"pre": pre_sha, "post": post_sha, "consistent": pre_sha == post_sha},
        "results": res,
        "rows": rows,
    }
    (TMP / "chunk_index_probe.json").write_text(json.dumps(out, ensure_ascii=False),
                                                encoding="utf-8")
    print(f"结果已写入 {TMP / 'chunk_index_probe.json'}")


if __name__ == "__main__":
    main()
