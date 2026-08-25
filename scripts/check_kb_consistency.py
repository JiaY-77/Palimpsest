# -*- coding: utf-8 -*-
"""
知识库一致性检查脚本（v2.0 统一语义层）
========================================
检查规则类文档（domain=rule）的入库一致性：
  1. 扫描知识库根目录（KNOWLEDGE_DIR）下的规则类文档（文件名/路径含
     副官加班协议 / 宪法 / 模型军团管理办法 / 模型路由决策树），用与
     build_kb_index.py 相同的 split_markdown 逻辑估算应有切片数。
  2. 查 TriviumDB 中 domain="rule" 的 kb_chunk 切片（source_path + 数量）。
  3. 对比：规则文件未入库 / 切片数不匹配 / 库中存在但源文件已删除 → 警告；
     全部一致 → OK。

可直接运行，也可 import 调用 check()：
    from scripts.check_kb_consistency import check
    result = check()

运行：
    python scripts/check_kb_consistency.py
"""

import os
import sys

# 确保能 import 项目 core 模块与 build_kb_index（以项目根为基准）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_PROJECT_ROOT, _SCRIPT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# 切换到项目根目录，保证 config 里的相对路径（data/mh_memory.db）解析正确
os.chdir(_PROJECT_ROOT)

try:
    # 以包形式导入（python -m scripts.check_kb_consistency 时可用）
    from scripts.build_kb_index import (
        CHUNK_TYPE,
        KNOWLEDGE_DIR,
        RULE_DOMAIN,
        _doc_domain,
        _kb_md_files,
        split_markdown,
    )
except ImportError:  # 直接运行 scripts/check_kb_consistency.py 时退化为同目录导入
    from build_kb_index import (
        CHUNK_TYPE,
        KNOWLEDGE_DIR,
        RULE_DOMAIN,
        _doc_domain,
        _kb_md_files,
        split_markdown,
    )

from core.trivium_store import TriviumStore  # noqa: E402


def check(knowledge_dir: str = KNOWLEDGE_DIR, store=None) -> dict:
    """
    一致性检查主函数。
    返回 {"status": "OK"|"WARN", "warnings": [...], "stats": {...}, "detail": {...}}
    """
    store = store or TriviumStore()
    warnings = []

    # ---- 1. 扫描规则类文档，估算应有切片数 ----
    rule_files = []  # [(rel_path, expected_chunks)]
    for fp in _kb_md_files(knowledge_dir):
        rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
        if _doc_domain(rel) != RULE_DOMAIN:
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError as e:
            warnings.append(f"规则文档读取失败: {rel}: {e}")
            continue
        expected = len(split_markdown(text))
        rule_files.append((rel, expected))

    # ---- 2. 查库中 domain=rule 切片（source_path -> 数量） ----
    db_map = {}  # rel -> chunk 数
    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        payload = node.get("payload", {}) if node else {}
        if payload.get("type") != CHUNK_TYPE:
            continue
        if payload.get("domain") != RULE_DOMAIN:
            continue
        rel = payload.get("source_path", "")
        if not rel:
            continue
        db_map[rel] = db_map.get(rel, 0) + 1

    # ---- 3. 对比 ----
    detail = {}
    for rel, expected in rule_files:
        got = db_map.get(rel, 0)
        detail[rel] = {"expected": expected, "in_db": got}
        if got == 0:
            warnings.append(
                f"规则文档未入库: {rel}（应有约 {expected} 块，库中 0 块）"
                " → 请运行 python scripts/build_kb_index.py"
            )
        elif got != expected:
            warnings.append(
                f"切片数不匹配: {rel}（应有约 {expected} 块，库中 {got} 块）"
                " → 请运行 python scripts/build_kb_index.py"
            )
    # 库中存在但源文件已不存在的 rule 切片（孤儿，增量模式应已清理，此处兜底提示）
    known = {rel for rel, _ in rule_files}
    for rel in sorted(db_map):
        if rel not in known:
            warnings.append(
                f"库中存在但源文件已不存在: {rel}（{db_map[rel]} 块）"
                " → 请运行 python scripts/build_kb_index.py"
            )

    status = "OK" if not warnings else "WARN"
    stats = {
        "rule_files": len(rule_files),
        "expected_chunks": sum(e for _, e in rule_files),
        "db_rule_chunks": sum(db_map.values()),
        "db_rule_files": len(db_map),
    }
    return {"status": status, "warnings": warnings, "stats": stats, "detail": detail}


if __name__ == "__main__":
    result = check()
    print("=" * 60)
    print("知识库一致性检查（v2.0 统一语义层，规则类文档 domain=rule）")
    print("=" * 60)
    stats = result["stats"]
    print(f"规则类文档数: {stats['rule_files']} 篇 | 应有切片约 {stats['expected_chunks']} 块")
    print(f"库中 rule 切片: {stats['db_rule_chunks']} 块（来自 {stats['db_rule_files']} 个文件）")
    if result["warnings"]:
        print(f"\n[结果] {result['status']}（{len(result['warnings'])} 条警告）\n")
        for w in result["warnings"]:
            print(f"  ⚠ {w}")
        print("\n提示：运行 python scripts/build_kb_index.py 重建索引后重试本检查。")
    else:
        print("\n[结果] OK：全部规则文档已入库且切片数一致 ✅")
    print("=" * 60)
