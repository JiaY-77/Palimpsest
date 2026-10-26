"""扫描库里真实存在的精确标识符（commit hash / PR 号），为扩充 exact 题集。

只读副本库，不改任何东西。输出按「可见命中数」降序，便于挑样本。
"""

import collections
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)  # 项目根（本脚本位于 eval/ 下）
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", ROOT + "/eval/.tmp_fusion/mh_memory.db")
from core.trivium_store import TriviumStore  # noqa: E402

HASH_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,12}\b")
PR_RE = re.compile(r"#(\d{1,5})\b")

store = TriviumStore()
hash_counter: collections.Counter = collections.Counter()
pr_counter: collections.Counter = collections.Counter()

for _nid, payload in store.iter_payloads():
    if not payload or payload.get("status") == "outdated":
        continue
    content = payload.get("content", "") or ""
    for m in HASH_RE.findall(content):
        hash_counter[m] += 1
    for m in PR_RE.findall(content):
        pr_counter[f"#{m}"] += 1

print("=== hash 候选（可见命中数 >=2，前 25）===")
for tok, n in hash_counter.most_common(25):
    if n >= 2:
        print(f"  {tok}\t{n}")
print("\n=== PR 号候选（可见命中数 >=2，前 30）===")
for tok, n in pr_counter.most_common(30):
    if n >= 2:
        print(f"  {tok}\t{n}")

# 同时验证：这些 token 在 FTS 里能不能被字面命中（fts_enum）
fts_path = os.path.join(os.path.dirname(os.environ["DB_PATH"]), "fts.db")
conn = sqlite3.connect("file:" + fts_path.replace("\\", "/") + "?mode=ro", uri=True)


def fts_hits(tok: str) -> int:
    try:
        rows = conn.execute("SELECT count(*) FROM mem_fts WHERE mem_fts MATCH ?", (f'"{tok}"',)).fetchone()
        return int(rows[0]) if rows else 0
    except sqlite3.Error:
        return -1


print("\n=== 交叉验证：hash 候选的 FTS 命中数 ===")
for tok, n in hash_counter.most_common(12):
    print(f"  {tok}: 可见{tok and n} / FTS {fts_hits(tok)}")
print("\n=== 交叉验证：PR 候选的 FTS 命中数 ===")
for tok, n in pr_counter.most_common(15):
    print(f"  {tok}: 可见{n} / FTS {fts_hits(tok)}")
conn.close()
