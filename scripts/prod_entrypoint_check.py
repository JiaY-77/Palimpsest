"""生产入口复测：直接调检索的真实实现，在两种配置下各跑一遍题集。

不是模拟排序，而是直接调 `mcp_tools.memory._mem_search_impl`（mem_search 的真实实现），
证明配置改动真的在生产入口生效（对照两种 `RETRIEVAL_EXPAND_DEPTH` 取值）。

全程在库副本上跑，真库用 SHA256 前后校验，保证只读。

用法：venv/Scripts/python.exe scripts/prod_entrypoint_check.py
输出：eval/.tmp/prod_entrypoint_check.json
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

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


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


PRE = sha256(ORIG_DB)
for p in ORIG_DB.parent.iterdir():
    if p.name.startswith(ORIG_DB.name) and p.is_file():
        shutil.copy2(p, TMP / p.name)
if ORIG_FTS.exists():
    shutil.copy2(ORIG_FTS, TMP / ORIG_FTS.name)
os.environ["DB_PATH"] = str(TMP / ORIG_DB.name)

from metrics import mrr_at_k, recall_at_k  # noqa: E402

from config import Config  # noqa: E402
from mcp_tools.memory import _mem_search_impl  # noqa: E402

LAYERS = ("hermes", "kb", "novel", "other")
items = json.loads((EVAL_DIR / "eval_set.json").read_text(encoding="utf-8"))["items"]
pos = [it for it in items if it.get("kind") != "negative" and it.get("gold_ids")]

results: dict[str, dict] = {}
for depth in (0, 1):
    Config.RETRIEVAL_EXPAND_DEPTH = depth
    os.environ["RETRIEVAL_EXPAND_DEPTH"] = str(depth)
    agg = defaultdict(list)
    lay = {L: defaultdict(list) for L in LAYERS}
    t0 = time.time()
    for it in pos:
        out = _mem_search_impl(it["query"], scope="all", domain="", domain_bias="",
                               top_k=10, include_neighbors=False,
                               block="", include_outdated=False)
        ranked = [r["id"] for r in out.get("results", [])]
        gold = set(it["gold_ids"])
        for k in (1, 3, 5, 10):
            agg[f"recall@{k}"].append(recall_at_k(ranked, gold, k))
        agg["mrr@10"].append(mrr_at_k(ranked, gold, 10))
        L = it.get("layer") if it.get("layer") in LAYERS else "other"
        lay[L]["recall@5"].append(recall_at_k(ranked, gold, 5))

    def avg(v):
        return sum(v) / len(v) if v else 0.0

    row = {m: avg(v) for m, v in agg.items()}
    row["layer_r5"] = {L: avg(lay[L]["recall@5"]) for L in LAYERS}
    row["seconds"] = round(time.time() - t0, 1)
    results[f"RETRIEVAL_EXPAND_DEPTH={depth}"] = row
    print(f"[expand_depth={depth}]  R@1 {row['recall@1']:.4f} R@3 {row['recall@3']:.4f} "
          f"R@5 {row['recall@5']:.4f} R@10 {row['recall@10']:.4f} MRR {row['mrr@10']:.4f} | "
          + " ".join(f"{L[:3]}:{row['layer_r5'][L]:.3f}" for L in LAYERS)
          + f"  ({row['seconds']}s)", flush=True)

d0 = results["RETRIEVAL_EXPAND_DEPTH=0"]["recall@5"]
d1 = results["RETRIEVAL_EXPAND_DEPTH=1"]["recall@5"]
print(f"\nmem_search 真实接口 R@5：depth=1 {d1:.4f} → depth=0 {d0:.4f}  (Δ {100 * (d0 - d1):+.1f}pp)")

POST = sha256(ORIG_DB)
print(f"库完整性: {'一致 ✅' if PRE == POST else '不一致 ❌'}")
(TMP / "prod_entrypoint_check.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"结果写入 {TMP / 'prod_entrypoint_check.json'}")
