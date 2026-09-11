"""查询改写 / 扩展的离线评测探针（本地生成模型，不改生产链路）。

动机：语义检索对「省略式口语查询」不友好（如「上次那个压测的事」），
用本地生成模型把查询改写为更完整的检索式，看是否提升 recall@k。

已知风险（必须在设计里防）：短实体查询会被改坏——
「凌无咎」可能被改写成「凌无咎的作品」，反而偏离原意。故提供条件触发变体（len > 阈值才改写）。

变体（同一库副本 / 同题集 / 同 embedding 模型，严格单变量）：
  base      原查询（节点级整条余弦，与其它检索探针同口径）
  exp_all   全部题都改写后检索
  exp_cond  仅 len(query) > --min-len 的题改写（默认 8）
  fuse_all  RRF(k=60) 融合 base 与 exp_all 两路排序
  fuse_cond RRF(k=60) 融合 base 与 exp_cond

改写调用：本地 ollama 生成模型（Config 无此项，用 --model 指定），temperature=0 保证确定性；
结果缓存到 eval/.tmp/query_expansion_cache.json（--rebuild 强制重写）。

用法：
  venv/Scripts/python.exe scripts/query_expansion_probe.py [--limit N] [--model qwen2.5:7b-instruct]
                                                          [--min-len 8] [--rebuild]
输出：eval/.tmp/query_expansion_probe.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import defaultdict
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

CACHE = TMP / "query_expansion_cache.json"
K_RRF = 60

PROMPT = (
    "你是检索查询改写助手。把用户的问题改写成一句更适合向量检索的查询。规则：\n"
    "1) 原查询中的人名、术语、专有名词必须一字不改地保留；\n"
    "2) 只补全省略掉的语义（把口语化的指代说清楚），不要猜测具体事实；\n"
    "3) 不要引入原查询没有的实体或数字；\n"
    "4) 只输出改写后的查询本身，不要任何解释、前缀或引号。\n"
    "用户问题：{q}\n改写："
)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# ---- 库副本保护 ----
pre_sha = sha256(ORIG_DB)
for p in sorted(ORIG_DB.parent.iterdir()):
    if p.name.startswith(ORIG_DB.name) and p.is_file():
        try:
            shutil.copy2(p, TMP / p.name)
        except Exception as e:  # noqa: BLE001 —— 库副本复制失败仅告警 SHA 校验兜底
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

EMB_MODEL = Config.OLLAMA_EMBEDDING_MODEL


def unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def node_domain(payload: dict) -> str:
    return (payload.get("domain", "")
            or payload.get("character_name", "") or "general").strip().lower()


def embed_batch(texts: list[str], batch: int = 64) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/")
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        try:
            r = requests.post(f"{url}/api/embed",
                              json={"model": EMB_MODEL, "input": part}, timeout=600)
            r.raise_for_status()
            vecs = r.json().get("embeddings")
            if not vecs or len(vecs) != len(part):
                raise RuntimeError("embed 返回数量不符")
            out.extend(vecs)
        except Exception:  # noqa: BLE001 —— 批量嵌入失败逐条回退保证部分结果
            for t in part:
                rr = requests.post(f"{url}/api/embeddings",
                                   json={"model": EMB_MODEL, "prompt": t[:2500]}, timeout=180)
                rr.raise_for_status()
                out.append(rr.json()["embedding"])
    return np.asarray(out, dtype=np.float32)


def rewrite(gen_model: str, query: str) -> str:
    """调本地生成模型改写查询；temperature=0，输出清洗成单行。"""
    url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/")
    r = requests.post(f"{url}/api/generate", json={
        "model": gen_model,
        "prompt": PROMPT.format(q=query),
        "stream": False,
        "options": {"temperature": 0, "num_predict": 96, "top_p": 1.0},
    }, timeout=180)
    r.raise_for_status()
    txt = (r.json().get("response") or "").strip()
    txt = txt.splitlines()[0].strip() if txt else ""
    for pref in ("改写：", "改写:", "查询：", "查询:"):
        if txt.startswith(pref):
            txt = txt[len(pref):].strip()
    return txt.strip('"“”\'') or query


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", type=str, default="qwen2.5:7b-instruct")
    ap.add_argument("--min-len", type=int, default=8, help="条件触发的长度阈值")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    store = TriviumStore()
    all_nodes = list(store.iter_nodes())
    active = [(nid, n) for nid, n in all_nodes
              if (n["payload"].get("status") or "active") != "outdated"
              and (n["payload"].get("content") or "").strip()]
    nids = [nid for nid, _ in active]
    texts = [(n["payload"].get("content") or "") for _, n in active]
    doms = [node_domain(n["payload"]) for _, n in active]
    print(f"active 节点 {len(active)}；正文 {sum(len(t) for t in texts)} 字")

    t0 = time.time()
    node_vecs = unit(embed_batch([t[:4000] for t in texts], batch=32))
    print(f"节点向量 {node_vecs.shape}，{time.time() - t0:.1f}s")

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

    # ---- 改写（缓存）----
    cache = {}
    if CACHE.exists() and not args.rebuild:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    todo = [it for it in pos if it["qid"] not in cache.get(args.model, {})]
    if todo:
        print(f"改写 {len(todo)} 题（模型 {args.model}，temperature=0）…")
        t0 = time.time()
        bucket = cache.setdefault(args.model, {})
        for n, it in enumerate(todo, 1):
            t1 = time.time()
            try:
                bucket[it["qid"]] = {"q": it["query"], "rw": rewrite(args.model, it["query"]),
                                     "s": round(time.time() - t1, 3)}
            except Exception as e:  # noqa: BLE001 —— 改写失败回退原查询并记录错误继续评估
                bucket[it["qid"]] = {"q": it["query"], "rw": it["query"], "s": 0.0,
                                     "err": str(e)[:120]}
            if n % 20 == 0:
                print(f"  {n}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        print(f"改写完成 {time.time() - t0:.1f}s")
    rw = cache[args.model]
    lat = [v.get("s", 0) for v in rw.values() if v.get("s")]
    print(f"改写延迟：均值 {sum(lat) / max(len(lat), 1):.3f}s（n={len(lat)}）")

    # ---- 评估 ----
    rows = []
    t0 = time.time()
    for k, it in enumerate(pos, 1):
        q = it["query"]
        qr = rw[it["qid"]]["rw"] if it["qid"] in rw else q
        def rank(text: str):
            v = unit(np.asarray(store.embed_text(text), dtype=np.float32)[None, :])[0]
            return [nids[i] for i in np.argsort(-(node_vecs @ v))]

        rk_base = rank(q)
        rk_rw = rank(qr) if qr != q else list(rk_base)
        cond_rw = qr if (len(q) > args.min_len and qr != q) else q
        rk_cond = rank(cond_rw) if cond_rw != q else list(rk_base)

        def rrf(a, b):
            ra = {x: i + 1 for i, x in enumerate(a)}
            rb = {x: i + 1 for i, x in enumerate(b)}
            keys = set(ra) | set(rb)
            sc = {x: 1.0 / (K_RRF + ra.get(x, 10 ** 6)) + 1.0 / (K_RRF + rb.get(x, 10 ** 6))
                  for x in keys}
            return sorted(keys, key=lambda x: -sc[x])

        rows.append({"qid": it["qid"], "query": q, "rewritten": qr, "gold": it["gold_ids"],
                     "gold_domain": gold_domain[it["qid"]],
                     "base": rk_base, "exp_all": rk_rw, "exp_cond": rk_cond,
                     "fuse_all": rrf(rk_base, rk_rw), "fuse_cond": rrf(rk_base, rk_cond)})
        if k % 20 == 0:
            print(f"  {k}/{len(pos)}  {time.time() - t0:.0f}s", flush=True)

    keys = ("base", "exp_all", "exp_cond", "fuse_all", "fuse_cond")

    def met(key):
        agg = defaultdict(list)
        dom = defaultdict(list)
        for r in rows:
            gold = set(r["gold"])
            for k in (1, 3, 5, 10):
                agg[f"R@{k}"].append(recall_at_k(r[key], gold, k))
            agg["MRR"].append(mrr_at_k(r[key], gold, 10))
            dom[r["gold_domain"]].append(recall_at_k(r[key], gold, 5))
        out = {m: sum(v) / len(v) for m, v in agg.items()}
        out["dom"] = {d: sum(v) / len(v) for d, v in sorted(dom.items(), key=lambda x: -len(x[1]))}
        return out

    res = {k: met(k) for k in keys}
    print("\n" + "=" * 80)
    print(f"查询改写评测（{len(rows)} 题；模型 {args.model}；min-len={args.min_len}）")
    print(f"{'variant':<12} {'R@1':>7} {'R@3':>7} {'R@5':>7} {'R@10':>7} {'MRR':>7}   kb / hermes R@5")
    for k in keys:
        m = res[k]
        print(f"{k:<12} {m['R@1']:7.4f} {m['R@3']:7.4f} {m['R@5']:7.4f} {m['R@10']:7.4f} "
              f"{m['MRR']:7.4f}   {m['dom'].get('kb', 0):.3f} / {m['dom'].get('hermes', 0):.3f}")
    print("=" * 80)

    b = res["base"]
    print("\nΔ 相对 base（pp）与逐题 gain/loss（R@5）：")
    for k in keys[1:]:
        m = res[k]
        g = loss = 0
        for r in rows:
            gold = set(r["gold"])
            a, b2 = recall_at_k(r["base"], gold, 5), recall_at_k(r[k], gold, 5)
            if b2 > a:
                g += 1
            elif b2 < a:
                loss += 1
        print(f"  {k:<12} ΔR@1 {100 * (m['R@1'] - b['R@1']):+6.2f} | ΔR@5 {100 * (m['R@5'] - b['R@5']):+6.2f} "
              f"| ΔR@10 {100 * (m['R@10'] - b['R@10']):+6.2f} | ΔMRR {100 * (m['MRR'] - b['MRR']):+6.2f} "
              f"| 逐题 +{g}/−{loss}")

    print("\n改写抽样（前 8 题）：")
    for r in rows[:8]:
        print(f"  [{r['qid']}] {r['query']}\n        → {r['rewritten']}")

    post_sha = sha256(ORIG_DB)
    print(f"\n库完整性: 前 {pre_sha[:16]} 后 {post_sha[:16]} → "
          f"{'一致' if pre_sha == post_sha else '不一致'}")

    out = {"meta": {"model": args.model, "min_len": args.min_len, "n_questions": len(rows),
                    "limit": args.limit, "embed_model": EMB_MODEL,
                    "rewrite_latency_s": round(sum(lat) / max(len(lat), 1), 3)},
           "sha256": {"pre": pre_sha, "post": post_sha, "consistent": pre_sha == post_sha},
           "results": res, "rows": rows}
    (TMP / "query_expansion_probe.json").write_text(json.dumps(out, ensure_ascii=False),
                                                    encoding="utf-8")
    print(f"结果已写入 {TMP / 'query_expansion_probe.json'}")


if __name__ == "__main__":
    main()
