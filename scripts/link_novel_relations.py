# -*- coding: utf-8 -*-
"""小说人物关系批量建边脚本。

读取 _novel_relations.json（人物关系清单，schema：
    {"source": 角色A, "target": 角色B, "type": 关系类型,
     "note": 一句话依据, "confidence": 可选}，
把每条确定关系写成图谱边。

建边协议（无向语义，双向各建一条）：
    source → target 与 target → source 各一条，label=type（大写），weight=1.0。
    双向建边参考 mcp_tools/graph.py 的 mem_link 行为（无向关系补反向边）。

幂等：建边前先用 store.get_edges 检查该 (source_id, target_id, label) 组合的出边
    是否已存在，存在则跳过（existing_skipped），防重复建边；--force 可强制重建。

匹配规则：
    遍历 domain=novel 节点，取 payload.kind == "character" 且 payload.title
    精确等于 source/target 者作为匹配节点（角色名以角色卡 # 标题为准）。
    未匹配到任何节点的角色收集进 unmatched_roles 输出 warning。

参数：
    --relations 关系清单 JSON 路径（默认项目根 _novel_relations.json）
    --dry-run   只预览统计，不写库（默认开启）
    --apply     真正建边（关闭 dry-run）

用法：
    venv/Scripts/python.exe scripts/link_novel_relations.py --dry-run
    venv/Scripts/python.exe scripts/link_novel_relations.py --apply
"""
import argparse
import json
import os
import sys

try:
    from _common import PROJECT_ROOT, SCRIPT_DIR
except ImportError:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)

from core.trivium_store import TriviumStore  # noqa: E402

# 默认关系清单路径（项目根）
DEFAULT_RELATIONS = os.path.join(PROJECT_ROOT, "_novel_relations.json")

# 双向建边约定：所有语义关系（本脚本 type）均视为无向，建一对反向边。
# 在此集合外的 label 需特别处理，但本脚本 type 全部归属此集合。
BIDIRECTIONAL_TYPES = {
    "TEACHER_STUDENT", "FAMILY", "RIVAL", "ALLY", "SAME_SECT",
}


def load_relations(path: str) -> list:
    """读取关系清单 JSON，返回条目列表。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("关系清单应为 JSON 数组")
    return data


def build_character_index(store):
    """扫描库中 domain=novel 的角色节点，建立 {title: node_id} 映射。

    只收录 kind=character（角色卡）节点，title 为角色名（# 标题）。
    同一 title 可能出现多个节点时取第一个（正常应为唯一）。
    """
    index = {}
    for nid, payload in store.iter_payloads():
        if payload.get("type") != "novel_chunk":
            continue
        if payload.get("domain") != "novel":
            continue
        if payload.get("kind") != "character":
            continue
        title = (payload.get("title") or "").strip()
        if not title:
            continue
        if title not in index:
            index[title] = nid
    return index


def edge_exists(store, src_id: int, dst_id: int, label: str) -> bool:
    """检查 src → dst 且 label 匹配的出边是否已存在（防重复建边）。"""
    label_upper = label.upper()
    for edge in store.get_edges(src_id):
        if edge.target_id == dst_id:
            if (getattr(edge, "label", "") or "").upper() == label_upper:
                return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="小说人物关系批量建边",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--relations", default=DEFAULT_RELATIONS,
                        help="关系清单 JSON 路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览统计，不写库")
    parser.add_argument("--force", action="store_true",
                        help="已存在的同名边也强制重建（默认跳过）")
    parser.add_argument("--apply", action="store_true",
                        help="真正写库建边（默认 dry-run 只预览统计）")
    args = parser.parse_args()

    # 默认 dry-run：未给 --apply 时按 dry-run
    dry_run = not args.apply

    relations = load_relations(args.relations)
    if not relations:
        print(json.dumps({"total_relations": 0}), ensure_ascii=False)
        return

    store = TriviumStore()
    char_index = build_character_index(store)

    # 第一步：预解析所有角色名，判断匹配/未匹配/去重
    total = len(relations)
    linked = 0
    skipped_relations = 0      # 任一端匹配不到角色而跳过的关系
    existing_skipped = 0       # 已存在边而跳过的关系
    unmatched_roles = set()

    # （去重集合：同一 (source, target, type) 只建一次）
    seen_pairs = set()

    for rel in relations:
        src = (rel.get("source") or "").strip()
        dst = (rel.get("target") or "").strip()
        rtype = (rel.get("type") or "").strip().upper()

        pair_key = (src, dst, rtype)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        src_id = char_index.get(src)
        dst_id = char_index.get(dst)

        if src_id is None:
            unmatched_roles.add(src)
        if dst_id is None:
            unmatched_roles.add(dst)

        if src_id is None or dst_id is None:
            skipped_relations += 1
            continue

        if rtype not in BIDIRECTIONAL_TYPES:
            # 未知类型：仍双向建（保守），此处仅提示
            print(f"警告：未约定的关系类型 {rtype}（{src}→{dst}），仍按双向建边")

        # 幂等检查：正反向任一条已存在即视为已建（除非 --force）
        exists = edge_exists(store, src_id, dst_id, rtype) \
            or edge_exists(store, dst_id, src_id, rtype)
        if exists and not args.force:
            existing_skipped += 1
            continue

        # 双向各建一条
        if not dry_run:
            store.create_edge(src_id, dst_id, rtype, weight=1.0)
            store.create_edge(dst_id, src_id, rtype, weight=1.0)
        linked += 1

    # 统计
    stats = {
        "mode": "dry-run（未写库）" if dry_run else "apply（已写库）",
        "total_relations": total,
        "linked": linked,
        "unmatched_roles": sorted(unmatched_roles),
        "skipped_relations": skipped_relations,
        "existing_skipped": existing_skipped,
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
