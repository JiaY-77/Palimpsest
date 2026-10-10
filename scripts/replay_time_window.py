"""回放对照：时间窗冲突判定（PR-④3）对既有库判定的影响评估。

目的：在**生产库的只读视图**上，模拟「新写入一条事实」时，旧逻辑 vs 新逻辑
对同一批候选的判定差异，逐条列出供人工判读。

安全声明：本脚本**只读**生产库（不写、不改、不拷）。只取已存在的 payload，
在内存里跑两套判据对比。

判据：
- 旧逻辑：score > 0.75 且过 type/domain 门 → 标 outdated
- 新逻辑：再加一层「时间窗重叠」——不重叠则不标

由于无法真实计算 embedding 相似度（需要模型），本脚本**不按相似度筛**，
而是对**同 type + 同 domain 的已存事实对**做时间窗重叠分析——给出一份
「如果这两条相似，会不会被时间窗判定放过」的分布，据此评估影响面。

用法：python scripts/replay_time_window.py [--limit N]
"""

import argparse
import itertools
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from core.bitemporal import FACT_TYPES, INVALID_AT, VALID_AT, windows_overlap
from core.trivium_store import node_domain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400, help="最多分析的节点数")
    args = ap.parse_args()

    from core.trivium_store import TriviumStore

    s = TriviumStore(read_only=True)
    try:
        db = s._acquire()
        try:
            ids = list(db.all_node_ids())[: args.limit]
        finally:
            db.close()

        # 取事实类节点（memory/task/plan）
        facts = []
        for nid in ids:
            node = s.get_node(nid)
            if not node:
                continue
            p = node.get("payload", {}) or {}
            if p.get("type") not in FACT_TYPES:
                continue
            facts.append({"id": nid, "type": p.get("type"), "domain": node_domain(p), "p": p})
    finally:
        with __import__("contextlib").suppress(Exception):
            s._acquire().close()

    print("# 回放对照：时间窗冲突判定影响评估")
    print(f"扫描事实类节点：{len(facts)} 条（上限 {args.limit}）\n")

    # 统计时间字段覆盖率
    has_valid = sum(1 for f in facts if f["p"].get(VALID_AT) is not None)
    has_invalid = sum(1 for f in facts if f["p"].get(INVALID_AT) is not None)
    print("## 时间字段覆盖率")
    print(f"- 有 valid_at：{has_valid}/{len(facts)} ({100 * has_valid / max(1, len(facts)):.0f}%)")
    print(f"- 有 invalid_at：{has_invalid}/{len(facts)} ({100 * has_invalid / max(1, len(facts)):.0f}%)")
    print(f"- **缺 valid_at 的节点**（新逻辑下恒判重叠，行为不变）：{len(facts) - has_valid}\n")

    # 按 (type, domain) 分组，组内两两比较时间窗
    groups = {}
    for f in facts:
        groups.setdefault((f["type"], f["domain"]), []).append(f)

    verdicts = Counter()
    changed_examples = []  # 新逻辑会放过（不标 outdated）的对
    for key, members in groups.items():
        if len(members) < 2:
            continue
        for a, b in itertools.combinations(members, 2):
            ov = windows_overlap(a["p"], b["p"])
            # 注意：这里无法知道真实相似度；本分析只评「时间窗」这一个维度
            if ov:
                verdicts["重叠（新逻辑照常标 outdated）"] += 1
            else:
                verdicts["不重叠（新逻辑会放过，不标）"] += 1
                if len(changed_examples) < 15:
                    changed_examples.append(
                        {
                            "type": key[0],
                            "domain": key[1],
                            "a_id": a["id"],
                            "a_valid": a["p"].get(VALID_AT),
                            "a_invalid": a["p"].get(INVALID_AT),
                            "b_id": b["id"],
                            "b_valid": b["p"].get(VALID_AT),
                            "b_invalid": b["p"].get(INVALID_AT),
                        }
                    )

    print("## 同 (type, domain) 组内两两时间窗判定分布")
    for k, v in verdicts.most_common():
        print(f"- {k}: {v}")
    total = sum(verdicts.values())
    print(f"\n共比较 {total} 对（同 type+domain 的已存事实两两组合）\n")

    print("## 「不重叠」样例（新逻辑下若二者相似，会被放过——需人工判读是否合理）")
    print("> 注意：这些对是否真的相似**未知**（无法在离线算 embedding），")
    print("> 只是时间窗维度的候选。真实受影响量 ≤ 此处数量。\n")
    for ex in changed_examples:
        print(json.dumps(ex, ensure_ascii=False))

    print("\n## 结论（自动生成，需人工复核）")
    print("- 缺 valid_at 的历史节点占多数时，新逻辑行为几乎不变（保守兜底生效）。")
    print("- 仅「有明确 valid_at 且已 invalid_at、且与新事实不重叠」的对会被放过。")
    print("- 由于默认关闭，**开启前此评估不影响任何现有行为**。")


if __name__ == "__main__":
    main()
