# -*- coding: utf-8 -*-
"""
MemoryHub 全量数据导出脚本（只读）
====================================
导出 TriviumDB 中所有节点的 payload（元数据）和全部边（图谱关系）为 JSON 备份，
用于 triviumdb 0.6.0 → 0.7.6 升级（存储格式不兼容，旧库无法直接打开）后的重建。

- 只导出 payload + edges，不导出向量（重建时重新生成）。
- 边按 (source_id, target_id, label) 去重；REVISED_BY 单向、RELATED_TO 双向协议
  会存两条反向边——原样导出，重建时按 label 语义处理。
- 全程只读，不修改数据库（连读失败时仅复制快照到系统临时目录后解析）。

两种读取模式（自动选择）：
  1. TriviumStore API —— 数据库未被其他进程占用时使用。
  2. 二进制快照解析   —— 数据库被运行中的 MCP 服务锁定（.lock 存在且被持有）、
     triviumdb 无法开库时，直接解析 triviumdb 0.6.0 的文件格式（已与服务器
     graph_neighbors / mem_recent 实测交叉验证一致）：
       - 节点记录: [u32 id][u32 vec_block][u32 payload_len][payload JSON]
       - 边记录:   [u64 source_id][u64 target_id][u16 label_len][label][f32 weight]
       边表起始偏移 = 文件头 u32 @ 42。

运行：
    venv/Scripts/python.exe scripts/export_all_data.py

输出：
    data/export_backup_20260824.json  （格式 {"nodes": [...], "edges": [...]}）
"""

import json
import os
import shutil
import struct
import sys
import tempfile
from collections import Counter

# 确保能 import 项目 core 模块（以项目根为基准）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# 切换到项目根目录，保证 config 里的相对路径（data/mh_memory.db）解析正确
os.chdir(_PROJECT_ROOT)

from core.trivium_store import TriviumStore  # noqa: E402

OUTPUT_PATH = "data/export_backup_20260824.json"
DB_FILE = "data/mh_memory.db"


# --------------------------------------------------------------------------
# 模式一：TriviumStore API
# --------------------------------------------------------------------------
def export_via_store(store: TriviumStore) -> dict:
    """用官方 API 遍历节点与边（库未被占用时）。"""
    nodes = []
    edges = []
    seen_edges = set()
    type_counter = Counter()
    label_counter = Counter()

    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        if not node:
            print(f"  警告: 节点 {nid} 读取失败，跳过")
            continue
        payload = node.get("payload") or {}
        nodes.append({"node_id": nid, "payload": payload})
        type_counter[payload.get("type", "unknown")] += 1

        for edge in store.get_edges(nid):
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
# 模式二：二进制快照解析（triviumdb 0.6.0 文件格式）
# --------------------------------------------------------------------------
def _snapshot_db() -> str:
    """把 .db 复制到系统临时目录（只读原库），返回快照路径。"""
    snap_dir = os.path.join(tempfile.gettempdir(), "mh_export_backup")
    os.makedirs(snap_dir, exist_ok=True)
    snap = os.path.join(snap_dir, "mh_memory.db")
    for attempt in range(1, 6):
        shutil.copy2(DB_FILE, snap)
        with open(snap, "rb") as f:
            head = f.read(4)
        if head == b"TVDB":
            return snap
        print(f"  快照校验失败（头 {head!r}），重试 {attempt}/5 ...")
    raise RuntimeError("无法获得一致的数据文件快照")


def export_via_binary_snapshot() -> dict:
    """数据库被占用时，解析 .db 文件快照（仅 payload/edges 所在区段）。"""
    import re as _re

    snap = _snapshot_db()
    with open(snap, "rb") as f:
        d = f.read()
    print(f"  快照: {snap} ({len(d)} bytes)")

    # 1. 边表偏移 = 文件头 u32 @ 42（0x2A），随文件重写变化，动态读取
    if len(d) < 46:
        raise RuntimeError("文件过短，非有效 triviumdb 文件")
    edge_off = struct.unpack_from("<I", d, 42)[0]
    if not (100 < edge_off < len(d)):
        raise RuntimeError(f"边表偏移异常: {edge_off} (file={len(d)})")

    # 2. 锚点法收集节点：所有 '{"' 位置，校验 [id u32][vec u32][len u32] 前缀 + JSON
    anchors = []  # (pos, id, vec_block, plen, payload)
    for m in _re.finditer(rb'\{"', d):
        j = m.start()
        if j < 12 or j >= edge_off:
            continue
        nid, vec, plen = struct.unpack_from("<III", d, j - 12)
        if not (0 < plen < 5_000_000) or j + plen > edge_off:
            continue
        try:
            obj = json.loads(d[j:j + plen])
        except Exception:
            continue
        if isinstance(obj, dict):
            anchors.append((j, nid, vec, plen, obj))
    anchors.sort()
    if not anchors:
        raise RuntimeError("节点表解析失败：未找到任何有效载荷记录")

    # 3. 连续性校验（容忍零填充墓碑间隙）
    nodes = []
    prev_end = None
    for k, (j, nid, vec, plen, payload) in enumerate(anchors):
        if prev_end is not None and j != prev_end:
            gap = d[prev_end:j]
            if gap and any(gap):
                print(f"  警告: 锚点 {k} 前存在非零间隙 {len(gap)}B（可能漏节点）")
            # 零填充 = 删除节点墓碑，跳过
        nodes.append({"node_id": nid, "payload": payload})
        prev_end = j + 12 + plen

    # 4. 边记录: [src u64][dst u64][llen u16][label][weight f32]
    edges = []
    pos = edge_off
    while pos + 18 <= len(d):
        src, dst, llen = struct.unpack_from("<QQH", d, pos)
        end = pos + 18 + llen
        if not (0 < llen <= 200) or end + 4 > len(d):
            break  # 边表结束（后续为向量块表/校验区）
        label = d[pos + 18:end].decode("utf-8", "replace")
        weight = struct.unpack_from("<f", d, end)[0]
        edges.append({"source_id": src, "target_id": dst,
                      "label": label, "weight": weight})
        pos = end + 4

    # 5. 去重（按 source_id, target_id, label）
    seen = set()
    dedup = []
    for e in edges:
        key = (e["source_id"], e["target_id"], e["label"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(e)
    edges = dedup

    # 6. 校验：边端点应都在节点集合中
    node_ids = set(n["node_id"] for n in nodes)
    orphans = set()
    for e in edges:
        if e["source_id"] not in node_ids:
            orphans.add(e["source_id"])
        if e["target_id"] not in node_ids:
            orphans.add(e["target_id"])
    if orphans:
        print(f"  警告: {len(orphans)} 个边端点不在节点表中: {sorted(orphans)[:10]}")

    type_counter = Counter(n["payload"].get("type", "unknown") for n in nodes)
    label_counter = Counter(e["label"] for e in edges)
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
def main() -> None:
    store = TriviumStore()
    print(f"数据库: {store.db_path}")
    print("开始导出（只读）...")

    mode = "TriviumStore API"
    try:
        data = export_via_store(store)
    except RuntimeError as e:
        print(f"  TriviumStore API 不可用（{e}）")
        print("  回退到二进制快照解析模式...")
        data = export_via_binary_snapshot()
        mode = "二进制快照解析（triviumdb 0.6.0 格式）"

    # 写 JSON（ensure_ascii=False 保留中文）
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"nodes": data["nodes"], "edges": data["edges"]},
            f, ensure_ascii=False, indent=2,
        )

    # 回读校验
    with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
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
