"""量化「日期被误判成 hash」的实际影响。

做法：从库里找真实含 8 位日期（如 20261022）的节点，人工写「用户会怎么问」的题，
然后在 A（基线）与 B（分档，会被误判触发）之间对比。若 B 明显变差，
说明误判是真伤，`is_exact_identifier_query` 需要收紧。
"""

import collections
import os
import re
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)  # 项目根（本脚本位于 eval/ 下）
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", ROOT + "/eval/.tmp_fusion/mh_memory.db")
from core.trivium_store import TriviumStore  # noqa: E402

DATE_RE = re.compile(r"\b20\d{6}\b")

store = TriviumStore()
counter: collections.Counter = collections.Counter()
samples: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)

for nid, payload in store.iter_payloads():
    if not payload or payload.get("status") == "outdated":
        continue
    content = payload.get("content", "") or ""
    for m in DATE_RE.findall(content):
        counter[m] += 1
        if len(samples[m]) < 2:
            s = max(0, content.find(m) - 60)
            samples[m].append((nid, content[s : content.find(m) + 80].replace("\n", " ")))

print("=== 库里 8 位日期 token（可见节点）top 15 ===")
for tok, n in counter.most_common(15):
    print(f"\n{tok}  出现 {n} 次")
    for nid, snip in samples[tok]:
        print(f"   [{nid}] …{snip}…")
