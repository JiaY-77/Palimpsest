"""安全边界探测：`is_exact_identifier_query` 会不会把普通查询误判成标识符查询。

背景：分档方案（B/E）在判定为精确标识符时把 FTS 权重从 0.1 抬到 1.0（甚至更高）。
误判 = 自然语言查询被强抬 FTS 权重 → 可能把语义检索打乱。这是上线方案的真实风险面，
必须有量化结论。
"""

import os
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", ROOT + "/eval/.tmp_fusion/mh_memory.db")
from core.search import is_exact_identifier_query  # noqa: E402

# 应当判 False（自然语言 / 含日期等非标识符数字）
SHOULD_BE_FALSE = [
    "20261022 那天做了哪些事",
    "版本 20260929 的库有什么变化",
    "20260828 到 20261017 之间的记录",
    "README 里怎么写的",
    "一共 1234567 条数据怎么办",
    "deadbeef 这个词是什么意思",  # 纯字母 8 位：反例对照（应 False）
    "defaced 这词什么意思",
    "iPhone15 的配置",
    "3 月 5 号那天的会",
    "第 12345678 号文件",
]
# 应当判 True（真标识符）
SHOULD_BE_TRUE = [
    "bb824d3",
    "d60240b 这个 commit",
    "PR #131",
    "#15 那个 issue",
    "748606a 那批六个 PR",
]

print("=== 应当判 False（误判会抬高 FTS 权重）===")
bad = 0
for q in SHOULD_BE_FALSE:
    got = is_exact_identifier_query(q)
    flag = "❌ 误判" if got else "✅"
    if got:
        bad += 1
    print(f"  {flag} {got!s:5} | {q}")
print(f"\n误判 {bad}/{len(SHOULD_BE_FALSE)}")

print("\n=== 应当判 True（漏判会救不回标识符查询）===")
miss = 0
for q in SHOULD_BE_TRUE:
    got = is_exact_identifier_query(q)
    flag = "✅" if got else "❌ 漏判"
    if not got:
        miss += 1
    print(f"  {flag} {got!s:5} | {q}")
print(f"\n漏判 {miss}/{len(SHOULD_BE_TRUE)}")
