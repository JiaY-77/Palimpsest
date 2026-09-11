"""单变量 A/B 评测：在库副本上跑题集，走检索的真实入口（不碰真库）。

环境变量：
  DB_PATH                 评测用的库副本路径（必填）
  OLLAMA_EMBEDDING_MODEL  该副本使用的 embedding 模型
  OUT_JSON                结果输出路径

评测口径与 eval/run_eval.py 一致（正样本 = kind != "negative" 且 gold 非空），
但检索走的是 `mem_search` 的真实实现 `_mem_search_impl`（top_k=10），
即用户日常入口，而不是模拟排序。

用法（跑两次，分别指向两份副本）：
  DB_PATH=eval/.tmp/ab2/qwen3/mh_memory.db OLLAMA_EMBEDDING_MODEL=qwen3-embedding:0.6b \
    venv/Scripts/python.exe scripts/ab_snapshot_eval.py

配套脚本：ab_snapshot_build.py（造副本）、ab_snapshot_compare.py（逐题归因）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

DB = os.environ.get("DB_PATH")
if not DB:
    raise SystemExit("需要 DB_PATH 环境变量（指向库副本）")
DBP = Path(DB)
if not DBP.is_file():
    raise SystemExit(f"库不存在: {DBP}")

OUT = os.environ.get("OUT_JSON") or str(ROOT / "eval" / ".tmp" / "ab2" / "eval_result.json")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


PRE = sha256(DBP)

from config import Config  # noqa: E402
from metrics import mrr_at_k, ndcg_at_k, recall_at_k  # noqa: E402
from mcp_tools.memory import _mem_search_impl  # noqa: E402

LAYERS = ("hermes", "kb", "novel", "other")
items = json.loads((ROOT / "eval" / "eval_set.json").read_text(encoding="utf-8"))["items"]
pos = [it for it in items if it.get("kind") != "negative" and it.get("gold_ids")]

agg = defaultdict(list)
lay = {L: defaultdict(list) for L in LAYERS}
per_q: dict[str, list[int]] = {}
for it in pos:
    out = _mem_search_impl(it["query"], scope="all", domain="", domain_bias="",
                           top_k=10, include_neighbors=False, block="",
                           include_outdated=False)
    ranked = [r["id"] for r in out.get("results", [])]
    per_q[it["qid"]] = ranked
    gold = set(it["gold_ids"])
    for k in (1, 3, 5, 10):
        agg[f"recall@{k}"].append(recall_at_k(ranked, gold, k))
    agg["mrr@10"].append(mrr_at_k(ranked, gold, 10))
    agg["ndcg@5"].append(ndcg_at_k(ranked, gold, 5))
    L = it.get("layer") if it.get("layer") in LAYERS else "other"
    lay[L]["recall@5"].append(recall_at_k(ranked, gold, 5))


def avg(v):
    return sum(v) / len(v) if v else 0.0


row = {m: avg(v) for m, v in agg.items()}
row["layer_r5"] = {L: avg(lay[L]["recall@5"]) for L in LAYERS}
row["model"] = Config.OLLAMA_EMBEDDING_MODEL
row["db"] = str(DBP)
row["expand_depth"] = getattr(Config, "RETRIEVAL_EXPAND_DEPTH", None)
row["db_unchanged"] = PRE == sha256(DBP)
row["per_q"] = per_q

print(f"[{row['model']}] depth={row['expand_depth']}  "
      f"R@1 {row['recall@1']:.4f} R@3 {row['recall@3']:.4f} R@5 {row['recall@5']:.4f} "
      f"R@10 {row['recall@10']:.4f} MRR {row['mrr@10']:.4f} nDCG5 {row['ndcg@5']:.4f} | "
      + " ".join(f"{L[:3]}:{row['layer_r5'][L]:.3f}" for L in LAYERS))

Path(OUT).parent.mkdir(parents=True, exist_ok=True)
Path(OUT).write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"写入 {OUT}")
