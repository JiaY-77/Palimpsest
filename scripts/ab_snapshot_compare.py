"""A/B 逐题归因：读两份评测结果，比较两次运行谁赢谁输以及赢在哪一层。

读 eval/.tmp/ab2/{qwen3,bgem3}_r{1,2}.json（由 ab_snapshot_eval.py 产出，含 per_q 逐题排名），
输出：
  - 两次运行的稳定性自检（聚合数字与逐题排名是否一致）
  - R@5 / R@10 的逐题 gain/loss（各自独有命中）+ 分层分布
  - gold 掉出 top-10 的题在两种配置下的排名差
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AB = ROOT / "eval" / ".tmp" / "ab2"
items = json.loads((ROOT / "eval" / "eval_set.json").read_text(encoding="utf-8"))["items"]
by_qid = {i["qid"]: i for i in items}

q1 = json.loads((AB / "qwen3_r1.json").read_text(encoding="utf-8"))
q2 = json.loads((AB / "qwen3_r2.json").read_text(encoding="utf-8"))
b1 = json.loads((AB / "bgem3_r1.json").read_text(encoding="utf-8"))
b2 = json.loads((AB / "bgem3_r2.json").read_text(encoding="utf-8"))

KEYS = ["recall@1", "recall@3", "recall@5", "recall@10", "mrr@10", "ndcg@5"]
stable_q = all(abs(q1[k] - q2[k]) < 1e-9 for k in KEYS)
stable_b = all(abs(b1[k] - b2[k]) < 1e-9 for k in KEYS)
print(f"稳定性自检：qwen3 两次一致={stable_q}  bgem3 两次一致={stable_b}")
print(f"per_q 两次一致: qwen3={q1['per_q'] == q2['per_q']}  bgem3={b1['per_q'] == b2['per_q']}")

pq, pb = q1["per_q"], b1["per_q"]


def hit(ranked, gold, k):
    return bool(set(ranked[:k]) & gold)


for k in (5, 10):
    only_b, only_q = [], []
    for qid, gold in ((i["qid"], set(i["gold_ids"])) for i in items
                      if i.get("kind") != "negative" and i.get("gold_ids")):
        hb, hq = hit(pb[qid], gold, k), hit(pq[qid], gold, k)
        if hb and not hq:
            only_b.append(qid)
        elif hq and not hb:
            only_q.append(qid)
    print(f"\n=== R@{k} 逐题 ===")
    print(f"  bge-m3 独有命中: {len(only_b)} 题 {only_b}")
    print(f"  qwen3  独有命中: {len(only_q)} 题 {only_q}")
    for label, qids in (("bge独有", only_b), ("qwen独有", only_q)):
        if qids:
            from collections import Counter
            print(f"  {label} 分层: {dict(Counter(by_qid[q].get('layer') for q in qids))}")

# gold 排名对比（对 bge 更差的题）
print("\n=== bge-m3 在 R@5 丢掉的题（qwen3 命中）明细 ===")
lost = []
for i in items:
    if i.get("kind") == "negative" or not i.get("gold_ids"):
        continue
    qid, gold = i["qid"], set(i["gold_ids"])
    hq = hit(pq[qid], gold, 5)
    hb = hit(pb[qid], gold, 5)
    if hq and not hb:
        rq = next((n + 1 for n, x in enumerate(pq[qid]) if x in gold), None)
        rb = next((n + 1 for n, x in enumerate(pb[qid]) if x in gold), None)
        lost.append((qid, i.get("layer"), rq, rb, i["query"][:34]))
for row in lost:
    print(f"  {row[0]} [{row[1]}] qwen rank {row[2]} → bge rank {row[3]}  | {row[4]}")

print("\n=== bge-m3 在 R@5 赢回的题（qwen3 未命中）明细 ===")
gained = []
for i in items:
    if i.get("kind") == "negative" or not i.get("gold_ids"):
        continue
    qid, gold = i["qid"], set(i["gold_ids"])
    if hit(pb[qid], gold, 5) and not hit(pq[qid], gold, 5):
        rq = next((n + 1 for n, x in enumerate(pq[qid]) if x in gold), None)
        rb = next((n + 1 for n, x in enumerate(pb[qid]) if x in gold), None)
        gained.append((qid, i.get("layer"), rq, rb, i["query"][:34]))
for row in gained:
    print(f"  {row[0]} [{row[1]}] qwen rank {row[2]} → bge rank {row[3]}  | {row[4]}")

print("\n=== 汇总 ===")
print(f"  R@5  : qwen3 {q1['recall@5']:.4f} → bge-m3 {b1['recall@5']:.4f}  "
      f"(赢 {len(gained)} 题 / 输 {len(lost)} 题)")
print(f"  R@10 : qwen3 {q1['recall@10']:.4f} → bge-m3 {b1['recall@10']:.4f}")
print(f"  分层 R@5: qwen3 {q1['layer_r5']} → bge-m3 {b1['layer_r5']}")
