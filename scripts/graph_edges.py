# -*- coding: utf-8 -*-
"""
图谱边持久化脚本
================
以 source_path（知识库相对路径）定义文档间关联边，幂等写入 TriviumDB
（RELATED_TO 双向建边），供 graph_neighbors 图谱查询使用。

背景：build_kb_index.py 重建索引 = 删旧 kb_chunk + 插新，节点 id 全变，
按 id 建的边全部丢失。本脚本用 source_path 定位节点（不依赖 id），
重建索引后重跑即可恢复图谱。

用法：
    python scripts/graph_edges.py            # 幂等建边（已存在的边跳过）
    python scripts/graph_edges.py --dry-run  # 只报告会建哪些边，不实际建

边定义见同目录 graph_edges.json：
    {"source": "03_技术学习/xx.md", "target": "03_技术学习/yy.md",
     "relation": "RELATED_TO", "weight": 0.9}
"""

import argparse
import json
import os
import sys

# 确保能 import 项目 core 模块（以项目根为基准），与 build_kb_index.py 一致
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# 切换到项目根目录，保证 config 里的相对路径（data/mh_memory.db）解析正确
os.chdir(_PROJECT_ROOT)

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402

EDGES_FILE = os.path.join(_SCRIPT_DIR, "graph_edges.json")
CHUNK_TYPE = "kb_chunk"

# 无向语义关系：与 mcp_server.mem_link 双向建边协议保持一致
BIDIRECTIONAL_RELATIONS = {"RELATED_TO", "CAUSES", "REFERS_TO"}


def _build_chunk_index(store) -> dict:
    """遍历全部节点，建立 {source_path: [(node_id, payload), ...]} 映射（只收 kb_chunk）"""
    index = {}
    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        payload = node.get("payload", {}) if node else {}
        if payload.get("type") != CHUNK_TYPE:
            continue
        rel = payload.get("source_path", "")
        if not rel:
            continue
        index.setdefault(rel, []).append((nid, payload))
    return index


def _pick_chunk(candidates: list, rel_path: str):
    """选「文档主块」：chunk_index==0 的块优先（文档首块，重建索引后仍能稳定定位）；
    title 等于文件名去掉 .md 的块次之（build_kb_index 给所有块都打该 title，
    仅靠 title 无法区分主块，故先看 chunk_index）；都不中则取第一个匹配块"""
    title = os.path.splitext(os.path.basename(rel_path))[0]
    for nid, payload in candidates:
        if payload.get("chunk_index") == 0:
            return nid
    for nid, payload in candidates:
        if payload.get("title") == title:
            return nid
    return candidates[0][0]


def _edge_exists(store, src: int, dst: int, label: str) -> bool:
    """检查 src → dst 且 label 匹配的出边是否已存在（防重复建边，同 mcp_server）"""
    label = label.upper()
    for edge in store.get_edges(src):
        if edge.target_id == dst and (getattr(edge, "label", "") or "").upper() == label:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="图谱边持久化（source_path 定位，幂等建边）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告会建哪些边，不实际建边")
    args = parser.parse_args()

    with open(EDGES_FILE, "r", encoding="utf-8") as f:
        edges = json.load(f)["edges"]

    store = TriviumStore()
    index = _build_chunk_index(store)
    mode = "DRY-RUN（不实际建边）" if args.dry_run else "实际建边"
    print(f"数据库: {Config.DB_PATH}")
    print(f"模式: {mode} | 边定义: {len(edges)} 条\n")

    created = existed = missing = 0
    for i, spec in enumerate(edges, 1):
        rel = (spec.get("relation") or "RELATED_TO").upper()
        try:
            weight = round(float(spec.get("weight", 0.9)), 6)
        except (TypeError, ValueError):
            weight = 0.9

        # 按 source_path 定位两端节点（重建索引后 id 变了也能重新定位）
        src_id = _pick_chunk(index[spec["source"]], spec["source"]) if spec["source"] in index else None
        dst_id = _pick_chunk(index[spec["target"]], spec["target"]) if spec["target"] in index else None
        if src_id is None or dst_id is None:
            missing += 1
            miss = []
            if src_id is None:
                miss.append(f"source 未找到: {spec['source']}")
            if dst_id is None:
                miss.append(f"target 未找到: {spec['target']}")
            print(f"[{i}] 节点未找到: {'；'.join(miss)}")
            continue
        if src_id == dst_id:
            missing += 1
            print(f"[{i}] 自环跳过: {spec['source']} 与 {spec['target']} 定位到同一节点 {src_id}")
            continue

        # 主边 + 反向边均先查存在再建（幂等）
        main_needed = not _edge_exists(store, src_id, dst_id, rel)
        reverse_needed = (rel in BIDIRECTIONAL_RELATIONS
                          and not _edge_exists(store, dst_id, src_id, rel))
        if not main_needed and not reverse_needed:
            existed += 1
            print(f"[{i}] 已存在: {spec['source']} -> {spec['target']}（{rel}，{src_id}↔{dst_id}）")
            continue

        plan = " + ".join(filter(None, [
            f"主边 {src_id}→{dst_id}" if main_needed else "",
            f"反向边 {dst_id}→{src_id}" if reverse_needed else "",
        ]))
        if args.dry_run:
            created += 1
            print(f"[{i}] [DRY-RUN 将建] {spec['source']} -> {spec['target']}（{rel} weight={weight}：{plan}）")
            continue

        if main_needed:
            store.create_edge(src_id, dst_id, rel, weight=weight)
        if reverse_needed:
            store.create_edge(dst_id, src_id, rel, weight=weight)
        created += 1
        print(f"[{i}] 已建: {spec['source']} -> {spec['target']}（{rel} weight={weight}：{plan}）")

    verb = "将建" if args.dry_run else "已建"
    print(f"\n=== 图谱边同步完成（{mode}）===")
    print(f"{verb}: {created} 条 | 已存在跳过: {existed} 条 | 节点未找到: {missing} 条")


if __name__ == "__main__":
    main()
