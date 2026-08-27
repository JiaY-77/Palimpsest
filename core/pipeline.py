# -*- coding: utf-8 -*-
"""ingest 流水线公共函数（P0 重构 2026-08-27）

消除 main.py / 各入口重复的「embed → merge → 计数」样板。
"""


def embed_and_merge(store, merger, node) -> bool:
    """对单个节点：embed + merge，返回是否新增（True=新增 / False=跳过）"""
    content_to_embed = node.get("content", "") or node.get("label", "")
    real_vec = store.embed_text(content_to_embed)
    return bool(merger.merge(node, real_vec))


def ingest_many(store, merger, nodes) -> tuple[int, int]:
    """批量 ingest：embed + merge + 计数，返回 (new_count, skipped_count)"""
    new_count = 0
    skipped = 0
    for node in nodes:
        if embed_and_merge(store, merger, node):
            new_count += 1
        else:
            skipped += 1
    return new_count, skipped
