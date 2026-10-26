"""回放对照：冲突判定阈值 0.75 → 0.85 对既有库判定的影响评估。

目的：在**生产库快照**上，对「同 type + 同 domain」的候选对，用真实 embedding
算出相似度，对比两套判据的判定差异，逐条列出供人工判读：

- 旧逻辑：0.4 < score <= 0.75 → related（不标）；score > 0.75 → 标 outdated
- 新逻辑：0.4 < score <= 0.85 → related（不标）；score > 0.85 → 标 outdated

即：**0.75 < score <= 0.85 的对，新逻辑从「标 outdated」变为「仅 related」**——
这份报告就是这批「被救回」的对的清单。

安全声明：本脚本**只读**库（read_only=True），不写、不改、不拷。
默认连生产库；要隔离请用 DB_PATH 环境变量指向副本。

用法：
    python scripts/replay_conflict_threshold.py [--old 0.75] [--new 0.85] [--limit N]
"""

import argparse
import contextlib
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根（本脚本位于 scripts/ 下）

from core.trivium_store import TriviumStore, node_domain


def _cosine(a, b):
    """两向量余弦相似度（f32/f64 混合安全）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# 参与冲突检测的类型（与 core/conflict.py 第 0 层白名单一致）
WHITELIST = ("memory", "task", "plan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=float, default=0.75, help="旧「同一事实」阈值")
    ap.add_argument("--new", type=float, default=0.85, help="新「同一事实」阈值")
    ap.add_argument("--limit", type=int, default=0, help="最多分析节点数（0=全部）")
    ap.add_argument("--max-pairs", type=int, default=200_000, help="候选对上限（防爆）")
    args = ap.parse_args()

    s = TriviumStore(read_only=True)
    try:
        db = s._acquire()
        try:
            ids = list(db.all_node_ids())
        finally:
            db.close()
        if args.limit:
            ids = ids[: args.limit]

        # 取白名单类型节点 + 向量
        nodes = []
        for nid in ids:
            node = s.get_node(nid)
            if not node:
                continue
            p = node.get("payload", {}) or {}
            if p.get("type") not in WHITELIST:
                continue
            vec = node.get("embedding") or node.get("vector")
            if not vec:
                continue
            nodes.append(
                {
                    "id": nid,
                    "type": p.get("type"),
                    "domain": node_domain(p),
                    "content": str(p.get("content", ""))[:70],
                    "vec": vec,
                }
            )
    finally:
        with contextlib.suppress(Exception):
            s._acquire().close()

    # 收集 REVISED_BY 边（无向归一化），用于标注「实际历史取代对」
    edge_set = set()
    for nid in ids:
        try:
            for e in s.get_edges(nid):
                if (getattr(e, "label", "") or "").upper() == "REVISED_BY":
                    edge_set.add(frozenset((nid, e.target_id)))
        except Exception:  # noqa: BLE001 —— 边读取失败不影响主报告
            continue

    print("# 回放对照：冲突阈值 0.75 → 0.85 影响评估")
    print(f"扫描节点：{len(ids)}，取到白名单+向量的节点：{len(nodes)}")
    print(f"库中 REVISED_BY 边：{len(edge_set)} 条")
    print(f"旧阈值 {args.old} → 新阈值 {args.new}\n")

    # 按 (type, domain) 分组，组内两两比较（与 conflict.py 第 2/3 层门一致）
    groups = {}
    for n in nodes:
        groups.setdefault((n["type"], n["domain"]), []).append(n)

    rescued = []  # 0.75<score<=0.85：旧判 outdated、新判 related（新逻辑救回）
    confirmed = []  # score>0.85：两套都判 outdated（真取代）
    adjacent = []  # 0.4<score<=0.75：两套都不判（无关本次改动）

    pairs_checked = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        for a, b in itertools.combinations(members, 2):
            pairs_checked += 1
            if pairs_checked > args.max_pairs:
                break
            score = _cosine(a["vec"], b["vec"])
            if score <= 0.4:
                continue
            row = {
                "score": round(score, 4),
                "a_id": a["id"],
                "b_id": b["id"],
                "type": key[0],
                "domain": key[1],
                "is_edge": frozenset((a["id"], b["id"])) in edge_set,
                "a_content": a["content"],
                "b_content": b["content"],
            }
            if args.old < score <= args.new:
                rescued.append(row)
            elif score > args.new:
                confirmed.append(row)
            else:
                adjacent.append(row)

    rescued.sort(key=lambda r: r["score"])
    confirmed.sort(key=lambda r: r["score"])
    rescued_edges = [r for r in rescued if r["is_edge"]]
    confirmed_edges = [r for r in confirmed if r["is_edge"]]

    print(f"## 比对概况（同 type+domain 两两组合，共检查 {pairs_checked} 对）")
    print(f"- **新逻辑救回**（{args.old} < score <= {args.new}，旧标 outdated → 新仅 related）：**{len(rescued)} 对**")
    print(f"  - 其中**实际存在 REVISED_BY 边**（历史真误伤，会被修正）：**{len(rescued_edges)} 对**")
    print(
        f"- 两套都判取代（score > {args.new}，真取代，不受影响）：{len(confirmed)} 对（内含实际边 {len(confirmed_edges)} 条）"
    )
    print(f"- 两套都判相关（{0.4} < score <= {args.old}，行为不变）：{len(adjacent)} 对\n")

    print(f"## ★ 被救回且**是真实历史边**的对（{len(rescued_edges)} 对）——历史真误伤，新逻辑会修正")
    for r in rescued_edges:
        print(json.dumps(r, ensure_ascii=False))
    print()

    print(f"## ★ 被救回的对（{len(rescued)} 对，含非边候选）——需人工逐条判读「该不该被标 outdated」")
    print("> 判读标准：两条是否在回答「同一个问题且答案互斥」→ 是=该取代（新逻辑漏判）；")
    print("> 否=互补/同主题不同方面 → 本就不该取代（新逻辑修对了）。\n")
    for r in rescued[:60]:
        print(json.dumps(r, ensure_ascii=False))
    if len(rescued) > 60:
        print(f"...（另有 {len(rescued) - 60} 对，可用 --limit/--max-pairs 细分）")

    print(f"\n## 参考：确认真取代的对（score > {args.new}，最高 20 对）——判读新阈值是否仍会误伤")
    for r in confirmed[-20:]:
        print(json.dumps(r, ensure_ascii=False))

    print("\n## 结论（自动生成，需人工复核）")
    print(f"- 受影响面 = 「被救回」的 {len(rescued)} 对（其中 {len(rescued_edges)} 对是真实历史边）。")
    print(f"- 真取代（{len(confirmed)} 对）不受影响；需复核这批复核是否含仍是误伤的（0.85 不够）。")
    print("- 逐条判读后：若被救回的对**大多**是互补/同主题 → 新阈值合理；若含真矛盾 → 需下调或加判别。")


if __name__ == "__main__":
    main()
