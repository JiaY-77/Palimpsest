# -*- coding: utf-8 -*-
"""
FTS 索引一致性巡检脚本
======================
对比主库（TriviumStore iter_payloads）与 fts.db（mem_fts 虚拟表），找出：
  - 主库有但 FTS 缺失的节点（missing_in_fts）
  - FTS 有但主库缺失的节点（stale_in_fts）
  - 内容漂移：FTS 中有该节点，但存的 content 文本与主库不一致（content_drift）

输出 JSON 报告：
  {"missing_in_fts": [{node_id, content(前80字)}], "stale_in_fts": [{node_id}],
   "content_drift": [{node_id, main_content(前80字), fts_content(前80字)}],
   "total_nodes": int, "fts_count": int, "consistent": bool}

纯读检查默认安全；--repair 时才调用 core.fts_index.rebuild(store) 全量重建，
然后重新检查输出结果。

运行：
    python scripts/check_fts_consistency.py
    python scripts/check_fts_consistency.py --repair
"""

import argparse
import json
import os
import sqlite3
import sys

try:
    from _common import SCRIPT_DIR as _SCRIPT_DIR, PROJECT_ROOT as _PROJECT_ROOT
except ImportError:
    import os as _os
    _scripts_dir = _os.path.dirname(_os.path.abspath(__file__))
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from _common import SCRIPT_DIR as _SCRIPT_DIR, PROJECT_ROOT as _PROJECT_ROOT
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from core.fts_index import _db_path  # noqa: E402  复用 fts.db 路径
from core.trivium_store import TriviumStore  # noqa: E402


def _fts_node_ids() -> tuple[dict, int]:
    """返回 (fts 中 {node_id: content} 映射, fts 节点总数)。异常时返回 ({}, 0)。"""
    path = _db_path()
    if not os.path.isfile(path):
        return {}, 0
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT node_id, content FROM mem_fts").fetchall()
    except sqlite3.Error:
        return {}, 0
    finally:
        conn.close()
    mapping = {r[0]: r[1] for r in rows}
    return mapping, len(mapping)


def check(store=None) -> dict:
    """对比主库与 fts.db，返回一致性报告。纯读，不修改任何数据。"""
    store = store or TriviumStore()

    main_ids: set = set()
    main_content: dict = {}
    for nid, payload in store.iter_payloads():
        main_ids.add(nid)
        main_content[nid] = payload.get("content", "")

    fts_mapping, fts_count = _fts_node_ids()
    fts_ids = set(fts_mapping.keys())

    missing_in_fts = [
        {"node_id": nid, "content": (main_content[nid] or "")[:80]}
        for nid in sorted(main_ids - fts_ids)
        if main_content.get(nid)  # 空 content 节点无需索引（index_node 跳过），不算缺失
    ]
    stale_in_fts = [
        {"node_id": nid}
        for nid in sorted(fts_ids - main_ids)
    ]

    # 内容级对账：主库有非空 content 且 FTS 中也有该节点，但 content 不一致
    content_drift = []
    for nid in sorted(main_ids & fts_ids):
        mc = main_content.get(nid, "")
        fc = fts_mapping.get(nid, "")
        if mc and mc != fc:
            content_drift.append({
                "node_id": nid,
                "main_content": mc[:80],
                "fts_content": fc[:80],
            })

    consistent = not missing_in_fts and not stale_in_fts and not content_drift

    return {
        "missing_in_fts": missing_in_fts,
        "stale_in_fts": stale_in_fts,
        "content_drift": content_drift,
        "total_nodes": len(main_ids),
        "fts_count": fts_count,
        "consistent": consistent,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="FTS 索引与主库一致性巡检")
    parser.add_argument(
        "--repair", action="store_true",
        help="发现不一致时调用 core.fts_index.rebuild(store) 全量重建后重新检查",
    )
    args = parser.parse_args()

    store = TriviumStore()
    result = check(store)

    if args.repair and not result["consistent"]:
        from core.fts_index import rebuild
        count = rebuild(store)
        print(f"[repair] 已全量重建 FTS 索引（{count} 节点），重新检查…")
        result = check(store)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
