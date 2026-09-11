"""检索期分块重打分的打分子对比（离线探针，15 个变体）。

与既有的检索期分块探针同口径（候选获取 / 题集 / 库副本 / embedding 模型完全一致），
只变「打分子」，严格单变量：

  base          整条节点内容余弦（基线）
  max           块最大余弦（复现已知负收益 R@5 ≈ -2.5pp）
  mean_top2     块余弦 top-2 均值（长度归一化候选 A）
  max_div_sqrt  块最大余弦 / sqrt(块数)（长度归一化候选 B）
  layered_top2  候选节点 domain=kb 用整条余弦，其余 domain 用 top-2 均值
  layered_max   候选节点 domain=kb 用整条余弦，其余 domain 用块最大余弦（归因）
  rrf_layered   RRF(k=60) 融合 layered_max 与 layered_top2 两路排序（跨域融合路线）
  calib_max     kb 用整条余弦；非 kb 按块 max 的组内秩映射到「非 kb 组 base 分分布」
                （跨域分数校准路线；见 eval/docs/10-chunk-calibration.md）
  calib_s80/s60/s40  软校准：α·base + (1−α)·calib_max 映射分（α 越小越接近 calib_max）
  calib_cond2/cond3  条件校准：只对块数 ≥2 / ≥3 的非 kb 节点校准，其余保持整条余弦
  calib_head         保头校准：非 kb 组内 base 分最高者不被组内他人反超
  calib_dom          分域校准：非 kb 内部按真实 domain 分组，映射到组内 base 分分布
                （以上四个细化路线的结论：见 eval/docs/11-score-calibration-round3.md
                 —— 全部增益 ≤1 题、与噪声同阶，本路线判定为已到边际）

分组口径：按候选节点的真实 domain 字段（gold 节点 payload.domain）分组，
          不用题目的 layer 字段（历史上 layer 与真实 domain 不对齐，会造成分层表失真）。
          同时打印 gold 题目的 layer 分布 + 候选节点 domain 分布用于核对分组键。

判定标准（出具到 stdout）：
  仅当「kb 层 R@5 相对 base 下降 ≤ 1pp，且 非 kb 层（尤其 hermes）R@5 上涨」的变体才算可用候选。

用法：venv/Scripts/python.exe scripts/chunk_norm_probe.py [--limit N] [--chunk 400] [--overlap 100] [--cand 30]
输出：eval/.tmp/chunk_norm_probe.json
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
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

VARIANTS = ("base", "max", "mean_top2", "max_div_sqrt", "layered_top2", "layered_max",
            "rrf_layered", "calib_max",
            "calib_s80", "calib_s60", "calib_s40", "calib_cond2", "calib_cond3",
            "calib_head", "calib_dom")

# 方向⑤新增变体说明：
#   rrf_layered  RRF(k=60) 融合 layered_max 与 layered_top2 两路排序
#                （kb 侧两路同分，非 kb 侧取两路折中，避免单一打分子的极端偏置）
#   calib_max    分数校准：kb 用整条余弦；非 kb 的块 max 分按秩分位映射到 base 分
#                的同一分布后再统一排序（治「跨域分数尺度不可比」）
K_RRF = 60
LAYERS = ("hermes", "kb", "novel", "other")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# ---- 库副本保护：先从真库复制副本，再 import Config（确保 Config.DB_PATH 指向副本）----
pre_sha = sha256(ORIG_DB)
for p in ORIG_DB.parent.iterdir():
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

MODEL = Config.OLLAMA_EMBEDDING_MODEL


def unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def node_domain(payload: dict) -> str:
    return (payload.get("domain", "")
            or payload.get("character_name", "") or "general").strip().lower()


def embed_batch(texts: list[str]) -> np.ndarray:
    """批量嵌入（ollama /api/embed）；失败则逐条回退（与 _t078_chunk_probe 同口径）。"""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    url = Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/")
    model = Config.OLLAMA_EMBEDDING_MODEL
    try:
        r = requests.post(f"{url}/api/embed",
                          json={"model": model, "input": texts}, timeout=300)
        r.raise_for_status()
        vecs = r.json().get("embeddings")
        if vecs:
            return np.asarray(vecs, dtype=np.float32)
    except Exception:  # noqa: S110, BLE001 —— 批量嵌入失败静默转逐条回退保证结果
        pass
    out = []
    for t in texts:
        rr = requests.post(f"{url}/api/embeddings",
                           json={"model": model, "prompt": t[:2500]}, timeout=120)
        rr.raise_for_status()
        out.append(rr.json()["embedding"])
    return np.asarray(out, dtype=np.float32)


def chunks(text: str, size: int, overlap: int) -> list[str]:
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    return [text[i:i + size] for i in range(0, len(text), step) if text[i:i + size].strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=400)
    ap.add_argument("--overlap", type=int, default=100)
    ap.add_argument("--cand", type=int, default=30)
    args = ap.parse_args()

    store = TriviumStore()
    items = json.loads((EVAL_DIR / "eval_set.json").read_text(encoding="utf-8"))["items"]
    pos = [it for it in items if it.get("kind") != "negative" and it.get("gold_ids")]
    if args.limit:
        pos = pos[: args.limit]

    # ---- 分组键：gold 节点真实 domain（读库 payload，与 eval_set.gold_domain 交叉核对）----
    db = store._acquire()
    gold_domain = {}
    missing = 0
    try:
        for it in pos:
            gid = it["gold_ids"][0]
            n = db.get(gid)
            if n is not None:
                gold_domain[it["qid"]] = node_domain(n.payload or {})
            else:
                missing += 1
                gold_domain[it["qid"]] = (it.get("gold_domain") or "other").strip().lower()
    finally:
        db.close()
    if missing:
        print(f"  [warn] {missing} 个 gold 节点未在库中读到，回退 eval_set.gold_domain")

    gold_layer = Counter(it.get("layer") for it in pos)
    gold_dom = Counter(gold_domain.values())
    print(f"\n分组键核对（n={len(pos)} 题）:")
    print("  gold 题目 layer 层分布（旧分组口径，勿再用）: "
          + "  ".join(f"{L}:{gold_layer.get(L, 0)}" for L in LAYERS))
    print("  gold 节点真实 domain 分布（本次分组键）: "
          + "  ".join(f"{d}:{gold_dom.get(d, 0)}" for d in sorted(gold_dom, key=lambda d: -gold_dom[d])))
    align = Counter((gold_domain[it["qid"]], it.get("layer")) for it in pos)
    print("  gold真实domain × 题目layer 交叉（应能看出旧 key 与真实分组错位）:")
    for (d, L), c in sorted(align.items(), key=lambda x: (-x[1], x[0])):
        print(f"    domain={d:<8} layer={L:<8} x{c}")

    n_chunks = 0
    t0 = time.time()
    rows = []
    cand_dom_c = Counter()
    for n, it in enumerate(pos, 1):
        qv = unit(np.asarray(store.embed_text(it["query"]), dtype=np.float32)[None, :])[0]
        cands = store.search_similar(qv, top_k=args.cand, expand_depth=0,
                                     apply_decay=False, block="")
        ids, texts, doms = [], [], []
        for c in cands:
            pl = c.get("payload", {}) or {}
            ids.append(c["id"])
            texts.append(pl.get("content") or "")
            doms.append(node_domain(pl))
        cand_dom_c.update(doms)

        # base：整条内容余弦（同口径重新嵌入，避免库内向量/衰减口径混淆）
        full = [t[:4000] for t in texts]
        full_vecs = unit(embed_batch(full)) if full else np.zeros((0, 1))
        base_scores = (full_vecs @ qv) if len(full_vecs) else np.zeros(len(ids))

        # 分块嵌入 + 每节点块相似度
        all_chunks, owner = [], []
        for i, t in enumerate(texts):
            cs = chunks(t, args.chunk, args.overlap)
            all_chunks.extend(cs)
            owner.extend([i] * len(cs))
        n_chunks += len(all_chunks)
        node_sims: list[list[float]] = [[] for _ in ids]
        if all_chunks:
            cvecs = unit(embed_batch(all_chunks))
            sims = cvecs @ qv
            for j, oi in enumerate(owner):
                node_sims[oi].append(float(sims[j]))

        # 六个打分子（同一批向量，纯分数变换）
        scores = {v: np.zeros(len(ids), dtype=np.float64) for v in VARIANTS}
        scores["base"] = base_scores
        for i, s in enumerate(node_sims):
            base_i = float(base_scores[i])
            if s:
                mx = max(s)
                top2 = heapq.nlargest(2, s)
                mean2 = (top2[0] + top2[1]) / 2.0 if len(top2) > 1 else top2[0]
                div_sqrt = mx / math.sqrt(len(s))
                scores["max"][i] = mx
                scores["mean_top2"][i] = mean2
                scores["max_div_sqrt"][i] = div_sqrt
            else:
                scores["max"][i] = base_i
                scores["mean_top2"][i] = base_i
                scores["max_div_sqrt"][i] = base_i
        for i, d in enumerate(doms):
            if d == "kb":
                scores["layered_top2"][i] = base_scores[i]
                scores["layered_max"][i] = base_scores[i]
            else:
                scores["layered_top2"][i] = scores["mean_top2"][i]
                scores["layered_max"][i] = scores["max"][i]

        # ---- 方向⑤ 路线① 排序融合：RRF 合并 layered_max 与 layered_top2 ----
        if len(ids):
            r_max = np.argsort(np.argsort(-scores["layered_max"])) + 1
            r_top2 = np.argsort(np.argsort(-scores["layered_top2"])) + 1
            scores["rrf_layered"] = 1.0 / (K_RRF + r_max) + 1.0 / (K_RRF + r_top2)

        # ---- 方向⑤ 路线② 分数校准：非 kb 的块分数校准到整条余弦的同一分布 ----
        kb_pos = [i for i, d in enumerate(doms) if d == "kb"]
        nonkb_pos = [i for i, d in enumerate(doms) if d != "kb"]
        for i in kb_pos:
            scores["calib_max"][i] = base_scores[i]
        if nonkb_pos:
            # 参考分布 = 非 kb 组自身的整条余弦分（与非 kb 的 base 同尺度，
            # 消除「块 max ≥ 整条均值」带来的系统性抬升）
            ref = np.sort(np.asarray([base_scores[i] for i in nonkb_pos], dtype=np.float64))
            grid = np.linspace(0.0, 1.0, len(ref))
            # 非 kb 组内按块 max 定顺序，再按秩分位取参考分布上的值（秩 0 = 最优 → 高分位）
            order = sorted(nonkb_pos, key=lambda i: -scores["max"][i])
            n_nk = len(order)
            for r, i in enumerate(order):
                p = 1.0 - (r + 0.5) / n_nk
                scores["calib_max"][i] = float(np.interp(p, grid, ref))

        # ---- 方向⑤ 第3轮：软校准 / 条件校准 / 保头校准 / 分域校准 ----
        # 四个新变体都以 calib_max 的「非 kb 映射分」为基础，kb 侧一律保持整条余弦（同 calib_max）。
        for key in ("calib_s80", "calib_s60", "calib_s40", "calib_cond2", "calib_cond3",
                    "calib_head", "calib_dom"):
            scores[key] = np.array(base_scores, dtype=np.float64)
        if nonkb_pos:
            mapped = {i: float(scores["calib_max"][i]) for i in nonkb_pos}
            # (a) 软校准：score = α·base + (1−α)·mapped，把「重排」降级为「微调」
            #     （α=1 → base，α=0 → calib_max；扫 0.8/0.6/0.4 找 R@1 与 R@5 的平衡点）
            for a, key in ((0.8, "calib_s80"), (0.6, "calib_s60"), (0.4, "calib_s40")):
                for i in nonkb_pos:
                    scores[key][i] = a * float(base_scores[i]) + (1.0 - a) * mapped[i]
            # (b) 条件校准：只对块数 ≥ 阈值的非 kb 节点校准
            #     （块数 1 时 max == 整条余弦，本不需要动；映射反而破坏其分数）
            for t, key in ((2, "calib_cond2"), (3, "calib_cond3")):
                for i in nonkb_pos:
                    scores[key][i] = (mapped[i] if len(node_sims[i]) >= t
                                      else float(base_scores[i]))
            # (c) 保头校准：非 kb 组内 base 分最高者不被组内他人反超（护住 R@1 头部）
            for i in nonkb_pos:
                scores["calib_head"][i] = mapped[i]
            top_base = max(nonkb_pos, key=lambda i: float(base_scores[i]))
            scores["calib_head"][top_base] = max(float(base_scores[top_base]),
                                                 max(mapped.values()))
            # (d) 分域校准：非 kb 内部按真实 domain 各自分组，映射到组内 base 分分布
            for d in sorted({doms[i] for i in nonkb_pos}):
                grp = [i for i in nonkb_pos if doms[i] == d]
                if len(grp) == 1:
                    scores["calib_dom"][grp[0]] = float(base_scores[grp[0]])
                    continue
                ref_d = np.sort(np.asarray([float(base_scores[i]) for i in grp],
                                           dtype=np.float64))
                grid_d = np.linspace(0.0, 1.0, len(ref_d))
                order_d = sorted(grp, key=lambda i: -scores["max"][i])
                for r, i in enumerate(order_d):
                    p = 1.0 - (r + 0.5) / len(order_d)
                    scores["calib_dom"][i] = float(np.interp(p, grid_d, ref_d))

        row = {"qid": it["qid"], "layer": it.get("layer"),
               "gold": it["gold_ids"], "gold_domain": gold_domain[it["qid"]],
               "ids": ids, "doms": doms}
        for v in VARIANTS:
            row[v] = [ids[i] for i in np.argsort(-scores[v])]
        rows.append(row)
        if n % 20 == 0:
            print(f"  {n}/{len(pos)}  {time.time() - t0:.0f}s  累计块 {n_chunks}", flush=True)

    print(f"\n候选节点真实 domain 分布（跨 {sum(cand_dom_c.values())} 个候选节点）:")
    tot_c = sum(cand_dom_c.values())
    for d, c in sorted(cand_dom_c.items(), key=lambda x: -x[1]):
        print(f"  {d:<10} {c:5d}  {100 * c / tot_c:5.1f}%")

    # ---- 评估 ----
    def evaluate(key: str) -> dict:
        agg = defaultdict(list)
        dom = defaultdict(lambda: defaultdict(list))
        for r in rows:
            gold = set(r["gold"])
            ranked = r[key]
            for k in (1, 3, 5, 10):
                agg[f"recall@{k}"].append(recall_at_k(ranked, gold, k))
            agg["mrr@10"].append(mrr_at_k(ranked, gold, 10))
            dom[r["gold_domain"]]["recall@5"].append(recall_at_k(ranked, gold, 5))

        def avg(v):
            return sum(v) / len(v) if v else 0.0

        out = {m: avg(v) for m, v in agg.items()}
        out["domain_r5"] = {d: avg(v["recall@5"]) for d, v in sorted(
            dom.items(), key=lambda x: -len(x[1]))}
        return out

    res = {v: evaluate(v) for v in VARIANTS}
    base = res["base"]
    dom_order = list(base["domain_r5"].keys())

    print("\n" + "=" * 78)
    print(f"变体对照表（题数 {len(rows)}/{len(pos)}, cand={args.cand}, chunk={args.chunk}, overlap={args.overlap}, model={MODEL}）")
    hdr = f"{'variant':<14} R@1    R@3    R@5    R@10   MRR    "
    for d in dom_order:
        hdr += f"{d[:5]:>7}"
    print(hdr)
    for v in VARIANTS:
        m = res[v]
        line = (f"{v:<14} {m['recall@1']:.4f} {m['recall@3']:.4f} {m['recall@5']:.4f} "
                f"{m['recall@10']:.4f} {m['mrr@10']:.4f}  ")
        for d in dom_order:
            line += f"{m['domain_r5'][d]:7.3f}"
        print(line)
    print("=" * 78)

    # ---- Δ R@5（pp）相对 base ----
    nonkb_idx = [r for r in rows if r["gold_domain"] != "kb"]

    def r5_pp(key: str, sub=None) -> float:
        sub = sub or rows
        gold = [set(r["gold"]) for r in sub]
        return sum(recall_at_k(r[key], gold[i], 5) for i, r in enumerate(sub)) / len(sub)

    base_r5_all, base_r5_kb, base_r5_hermes, base_r5_nonkb = (
        r5_pp("base"), r5_pp("base", [r for r in rows if r["gold_domain"] == "kb"]),
        r5_pp("base", [r for r in rows if r["gold_domain"] == "hermes"]), r5_pp("base", nonkb_idx))
    print("\nΔ R@5 相对 base（pp）:")
    print(f"  base R@5 参照值  全部 {base_r5_all:.4f} | kb {base_r5_kb:.4f} | "
          f"hermes {base_r5_hermes:.4f} | 非kb {base_r5_nonkb:.4f}")
    deltas = {}
    for v in VARIANTS:
        if v == "base":
            continue
        d_all = 100 * (r5_pp(v) - base_r5_all)
        d_kb = 100 * (r5_pp(v, [r for r in rows if r["gold_domain"] == "kb"]) - base_r5_kb)
        d_her = 100 * (r5_pp(v, [r for r in rows if r["gold_domain"] == "hermes"]) - base_r5_hermes)
        d_non = 100 * (r5_pp(v, nonkb_idx) - base_r5_nonkb)
        deltas[v] = {"all": d_all, "kb": d_kb, "hermes": d_her, "nonkb": d_non}
        print(f"  {v:<14} 全部 {d_all:+6.2f} | kb {d_kb:+6.2f} | "
              f"hermes {d_her:+6.2f} | 非kb {d_non:+6.2f}")
    print("\n判定标准：kb R@5 相对 base 下降 ≤ 1pp 且 非kb（尤其 hermes）R@5 上涨 → 可用候选")
    for v in VARIANTS:
        if v == "base":
            print("  base: 基线，不参与判定。")
            continue
        d = deltas[v]
        kb_ok = d["kb"] >= -1.0
        nonkb_up = d["nonkb"] > 0.0
        hermes_up = d["hermes"] > 0.0
        usable = kb_ok and nonkb_up and hermes_up
        verdict = "可用 ✔" if usable else "不可用 ✘"
        print(f"  {v:<14} kb:{d['kb']:+.2f}pp({'降≤1pp' if kb_ok else '降>1pp'}) "
              f"hermes:{d['hermes']:+.2f}pp({'涨' if hermes_up else '未涨'}) "
              f"非kb:{d['nonkb']:+.2f}pp({'涨' if nonkb_up else '未涨'})  → {verdict}")

    post_sha = sha256(ORIG_DB)
    consistent = pre_sha == post_sha
    print(f"\n库完整性: 原库前 {pre_sha[:16]} 后 {post_sha[:16]} → "
          f"{'一致' if consistent else '不一致'}")
    print(f"题数 {len(rows)}；块总数 {n_chunks}；参数 chunk={args.chunk} overlap={args.overlap} "
          f"cand={args.cand} limit={args.limit}；耗时 {time.time() - t0:.0f}s")

    out = {
        "meta": {"chunk": args.chunk, "overlap": args.overlap, "cand": args.cand,
                 "limit": args.limit, "model": MODEL, "n_questions": len(rows),
                 "n_chunks": n_chunks, "runtime_s": round(time.time() - t0, 1)},
        "sha256": {"pre": pre_sha, "post": post_sha, "consistent": consistent},
        "distributions": {"gold_layer": dict(gold_layer), "gold_domain": dict(gold_dom),
                          "candidate_domain": dict(cand_dom_c)},
        "variants": {v: {**{m: res[v][m] for m in ("recall@1", "recall@3", "recall@5",
                                                   "recall@10", "mrr@10")},
                          "domain_r5": res[v]["domain_r5"],
                          "delta_r5_pp": deltas.get(v)} for v in VARIANTS},
        "rows": rows,
    }
    (TMP / "chunk_norm_probe.json").write_text(json.dumps(out, ensure_ascii=False),
                                              encoding="utf-8")
    print(f"结果已写入 {TMP / 'chunk_norm_probe.json'}")


if __name__ == "__main__":
    main()
