"""为自然语言类题集抽样节点：输出可读内容供人工写 query。

抽样原则
--------
- 跨类型：kb_chunk / memory / correction / task / novel_chunk / record
- 排除短内容（<120 字，问不出有意义的自然语言题）
- 排除内容含明显标识符的节点（commit hash / #PR），避免与精确标识符类混淆
- 排除 outdated（评测口径只看可见节点）
- 固定随机种子，可复现
"""

import json
import os
import random
import re
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)  # 项目根（本脚本位于 eval/ 下）
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", ROOT + "/eval/.tmp_fusion/mh_memory.db")
from core.trivium_store import TriviumStore  # noqa: E402

# 每类要几条
WANT = {
    "kb_chunk": 5,
    "memory": 3,
    "correction": 2,
    "task": 3,
    "novel_chunk": 2,
    "record": 2,
    "plan": 1,
    "solution": 1,
}

_IDENT_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b|#\d{1,5}\b")
_SEED = 20261022

store = TriviumStore()
buckets: dict[str, list[tuple[int, dict]]] = {}
for nid, payload in store.iter_payloads():
    if not payload:
        continue
    if payload.get("status") == "outdated":
        continue
    ptype = payload.get("type", "")
    content = payload.get("content", "") or ""
    if len(content) < 120:
        continue
    if _IDENT_RE.search(content[:400]):
        continue
    buckets.setdefault(ptype, []).append((nid, payload))

rng = random.Random(_SEED)
picked = []
for ptype, want in WANT.items():
    pool = buckets.get(ptype, [])
    if not pool:
        print(f"⚠ {ptype}: 池为空")
        continue
    take = min(want, len(pool))
    picked.extend(rng.sample(pool, take))

print(f"=== 抽样 {len(picked)} 个节点（种子 {_SEED}）===")
out = []
for nid, p in picked:
    dom = p.get("domain", "")
    content = (p.get("content", "") or "").replace("\n", " ")
    print(f"\n--- id={nid} type={p.get('type')} domain={dom} len={len(content)}")
    print(content[:350])
    out.append(
        {
            "id": nid,
            "type": p.get("type"),
            "domain": dom,
            "importance": p.get("importance"),
            "content_head": content[:350],
        }
    )

with open(ROOT + "/eval/.tmp_fusion/sample_for_queries.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("\n已写 eval/.tmp_fusion/sample_for_queries.json")
