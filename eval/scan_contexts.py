"""抽验候选标识符的真实语义：打印每个 token 在节点内容里的上下文片段。

用途：区分「真 commit hash / 真 PR 号」与「章节编号、日期、路径数字」等误判。
"""

import os
import re
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)  # 项目根（本脚本位于 eval/ 下）
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", ROOT + "/eval/.tmp_fusion/mh_memory.db")
from core.trivium_store import TriviumStore  # noqa: E402

CANDIDATES = [
    "748606a",
    "abc0329",
    "c6760db",
    "9e2f540",
    "204d0b4",
    "68e1831",
    "fd84dd2",
    "#15",
    "#40",
    "#41",
    "#21",
    "#31",
    "#32",
    "#54",
    "#24",
]

store = TriviumStore()
nodes = []
for nid, payload in store.iter_payloads():
    if not payload or payload.get("status") == "outdated":
        continue
    content = payload.get("content", "") or ""
    if content:
        nodes.append((nid, content))

for tok in CANDIDATES:
    pat = re.compile(re.escape(tok) + r"(?![\w#])")
    shown = 0
    print(f"\n===== {tok} =====")
    for nid, content in nodes:
        if shown >= 3:
            break
        m = pat.search(content)
        if not m:
            continue
        s, e = max(0, m.start() - 45), min(len(content), m.end() + 45)
        snippet = content[s:e].replace("\n", " ")
        print(f"  [{nid}] …{snippet}…")
        shown += 1
