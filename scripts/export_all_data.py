"""
Palimpsest 全量数据导出脚本（只读）
====================================
导出 TriviumDB 中所有节点的 payload（元数据）和全部边（图谱关系）为 JSON 备份，
用于跨版本升级（存储格式不兼容、旧库无法直接打开）后的重建。

- 只导出 payload + edges，不导出向量（重建时重新生成）。
- 边按 (source_id, target_id, label) 去重；REVISED_BY 单向、RELATED_TO 双向协议
  会存两条反向边——原样导出，重建时按 label 语义处理。
- 全程只读，不修改数据库。

读取方式（仅一种，TriviumStore API）：
  单连接内遍历全部节点与边。triviumdb 0.8.6 起同一进程禁止嵌套双开连接
  （遍历迭代器持有连接时再 _acquire 新连接会报 Database locked），
  故节点读取与边读取共用同一 db 连接。
  若数据库被其他进程（REST/MCP）占用导致 API 不可用，请先停止服务再运行；
  或直接物理复制 data/ 目录备份（推荐，最简单可靠）。

  历史说明：v0.8.6 之前的「二进制快照解析」兜底（直接解析 .db 主文件内嵌
  payload）已退役——0.8.6 把 payload 迁至 .pld.<gen> sidecar，主文件不再
  内嵌 payload，旧解析必然失败。

运行：
    venv/Scripts/python.exe scripts/export_all_data.py

输出：
    data/export_backup_20260824.json  （格式 {"nodes": [...], "edges": [...]}）
"""

import contextlib
import json
import os
from collections import Counter

# 确保能 import 项目 core 模块（以项目根为基准，_common 导入即把项目根注入 sys.path）
import _common  # noqa: F401

from core.trivium_store import TriviumStore

OUTPUT_PATH = "data/export_backup_20260824.json"
DB_FILE = "data/mh_memory.db"


# --------------------------------------------------------------------------
# 模式一：TriviumStore API（唯一模式，单连接遍历）
# --------------------------------------------------------------------------
def export_via_store(store: TriviumStore) -> dict:
    """用官方 API 遍历节点与边（单连接内完成，适配 triviumdb 0.8.6）。

    0.8.6 起同进程禁止嵌套双开连接：iter 类遍历（iter_nodes/iter_payloads）
    持有的连接在迭代器耗尽前不会释放，若在循环体内再调 store.get_edges()
    （内部 _acquire 新连接）会报 Database locked。故此处不借助 iter_nodes +
    store.get_edges 的组合，而是在一个 db 连接内 all_node_ids + get +
    get_edges 全部完成。
    """
    nodes = []
    edges = []
    seen_edges = set()
    type_counter = Counter()
    label_counter = Counter()

    db = store._acquire()
    try:
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node or not (node.payload or {}):
                print(f"  警告: 节点 {nid} 读取失败，跳过")
                continue
            payload = node.payload or {}
            nodes.append({"node_id": nid, "payload": payload})
            type_counter[payload.get("type", "unknown")] += 1

            for edge in db.get_edges(nid) or []:
                source_id = getattr(edge, "source_id", None) or nid
                target_id = edge.target_id
                label = getattr(edge, "label", "")
                weight = getattr(edge, "weight", None)
                key = (source_id, target_id, label)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append(
                    {"source_id": source_id, "target_id": target_id,
                     "label": label, "weight": weight}
                )
                label_counter[label] += 1
    finally:
        with contextlib.suppress(Exception):
            db.close()

    return {
        "nodes": nodes, "edges": edges,
        "stats": {
            "node_total": len(nodes),
            "type_counts": dict(type_counter),
            "edge_total": len(edges),
            "label_counts": dict(label_counter),
        },
    }


# --------------------------------------------------------------------------
# 模式二（已退役）：二进制快照解析
# 说明：triviumdb 0.6.0 时代的 payload 内嵌在 .db 主文件，可绕过 API 直接解析。
# 0.8.6 起 payload 迁至 .pld.<gen> mmap sidecar，主文件不再内嵌 payload，
# 旧解析逻辑必然失败，故退役（函数已删除）。需要备份时请停服务后物理复制
# data/ 目录。
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
def main() -> None:
    store = TriviumStore()
    print(f"数据库: {store.db_path}")
    print("开始导出（只读）...")

    mode = "TriviumStore API（单连接）"
    try:
        data = export_via_store(store)
    except RuntimeError as e:
        print(f"  TriviumStore API 不可用（{e}）")
        print("  提示: 数据库可能被 REST/MCP 服务占用。请先停止服务后重试，")
        print("  或直接物理复制 data/ 目录备份（含 .db/.vec/.pld.*/.fts.db）。")
        print("  （旧版 export_all_data.py 的二进制快照兜底已在 triviumdb 0.8.6")
        print("   适配中退役——payload 迁至 .pld sidecar 后主文件不再内嵌 payload）")
        raise

    # 写 JSON（ensure_ascii=False 保留中文）
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"nodes": data["nodes"], "edges": data["edges"]},
            f, ensure_ascii=False, indent=2,
        )

    # 回读校验
    with open(OUTPUT_PATH, encoding="utf-8") as f:
        back = json.load(f)
    assert len(back["nodes"]) == len(data["nodes"])
    assert len(back["edges"]) == len(data["edges"])

    stats = data["stats"]
    size = os.path.getsize(OUTPUT_PATH)
    print(f"\n=== 导出完成（模式: {mode}）===")
    print(f"输出文件: {OUTPUT_PATH} ({size:,} bytes)")
    print(f"节点总数: {stats['node_total']}")
    print("各 type 数量:")
    for t, c in sorted(stats["type_counts"].items(), key=lambda x: -x[1]):
        print(f"  {t}: {c}")
    print(f"边总数: {stats['edge_total']}")
    print("各 label 数量:")
    for lb, c in sorted(stats["label_counts"].items(), key=lambda x: -x[1]):
        print(f"  {lb}: {c}")


if __name__ == "__main__":
    main()
