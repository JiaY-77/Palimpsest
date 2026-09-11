# -*- coding: utf-8 -*-
"""分块重打分探针的逐题归因工具（配合 scripts/chunk_norm_probe.py 使用）。

读 `eval/.tmp/chunk_norm_probe.json`（探针结果，含逐题排名），对指定变体做：
  - 聚合指标（R@1/R@3/R@5/R@10/MRR）+ 相对 base 的差值
  - 逐题 gain/loss 计数、掉出 top5 / top10 / top1 的题数
  - 变化题明细（gold 位次变化 + 真实 domain 分布）

用途：聚合指标在小题集上极易被单题支配（1 题 = 0.85pp），
下结论前必须知道增益来自几题、损失落在哪个 domain。

用法：
    venv/Scripts/python.exe scripts/chunk_probe_attrib.py [变体名 ...]
    不带参数 = 归因所有 calib_* 变体（默认在 base / 新变体间对比）。
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "eval" / ".tmp"
sys.path.insert(0, str(ROOT / "eval"))
from metrics import recall_at_k  # noqa: E402

RESULT = TMP / "chunk_norm_probe.json"
if not RESULT.exists():
    sys.exit(f"未找到探针结果 {RESULT}，请先跑 scripts/chunk_norm_probe.py")

data = json.loads(RESULT.read_text(encoding="utf-8"))
rows = data["rows"]
variants = list(data["variants"].keys())
keys = sys.argv[1:] or [v for v in variants if v.startswith("calib_") and v != "calib_max"]
keys = ["base", "calib_max"] + [k for k in keys if k not in ("base", "calib_max")]


def r_at(r, key, k):
    return recall_at_k(r[key], set(r["gold"]), k)


def avg(fn):
    return sum(fn(r) for r in rows) / len(rows)


def position(r, key):
    gold = r["gold"][0]
    return r[key].index(gold) + 1 if gold in r[key] else None


base_r1, base_r5, base_r10 = (avg(lambda r: r_at(r, "base", k)) for k in (1, 5, 10))
print(f"题数 {len(rows)}  base: R@1 {base_r1:.4f}  R@5 {base_r5:.4f}  R@10 {base_r10:.4f}\n")

print(f"{'variant':<14} {'R@1':>7} {'ΔR@1':>7} {'R@5':>7} {'ΔR@5':>7} {'R@10':>7} {'ΔR@10':>7} "
      f"| R@5 gain/loss | R@1 gain/loss | 掉top5 | 掉top10")
for v in keys:
    r1v, r5v, r10v = (avg(lambda r: r_at(r, v, k)) for k in (1, 5, 10))
    g5 = sum(1 for r in rows if r_at(r, v, 5) > r_at(r, "base", 5))
    l5 = sum(1 for r in rows if r_at(r, v, 5) < r_at(r, "base", 5))
    g1 = sum(1 for r in rows if r_at(r, v, 1) > r_at(r, "base", 1))
    l1 = sum(1 for r in rows if r_at(r, v, 1) < r_at(r, "base", 1))
    out5 = sum(1 for r in rows if r_at(r, "base", 5) == 1 and r_at(r, v, 5) == 0)
    out10 = sum(1 for r in rows if r_at(r, "base", 10) == 1 and r_at(r, v, 10) == 0)
    print(f"{v:<14} {r1v:7.4f} {100*(r1v-base_r1):+7.2f} {r5v:7.4f} {100*(r5v-base_r5):+7.2f} "
          f"{r10v:7.4f} {100*(r10v-base_r10):+7.2f} | {g5:2d}/{l5:2d}       | {g1:2d}/{l1:2d}       "
          f"| {out5:6d} | {out10:7d}")

print("\n逐题明细（相对 base 有 R@1 / R@5 / R@10 变化的题）：")
for v in keys:
    ch = [r for r in rows if any(r_at(r, v, k) != r_at(r, "base", k) for k in (1, 5, 10))]
    if not ch:
        print(f"  {v}: 无变化")
        continue
    print(f"  {v}: {len(ch)} 题")
    for r in ch:
        pb, pv = position(r, "base"), position(r, v)
        print(f"    {r['qid']:<6} [{r['gold_domain']:<7}] base#{pb} → {v}#{pv}")
    print(f"    domain 分布: {dict(Counter(r['gold_domain'] for r in ch))}")

print("\ngold 命中位次概览：")
for v in keys:
    pos = [position(r, v) for r in rows]
    hit = [p for p in pos if p]
    top1 = sum(1 for p in pos if p == 1)
    print(f"  {v:<14} gold@top1 {top1}/{len(rows)}  平均位次(命中者) "
          f"{sum(hit)/len(hit):.2f}")
