# -*- coding: utf-8 -*-
"""
Palimpsest 数据库重建脚本
========================
从 data/export_backup_20260824.json 恢复到新库（DB_PATH 环境变量指向的副本），
用于 triviumdb 0.6.0 → 0.7.6 升级（存储格式不兼容）后的数据重建。

恢复范围（按任务约定）：
  - 节点：只恢复非 kb_chunk 节点（event / character_state / plot_plan /
    inspiration / user_intent，共 67 个）；kb_chunk 152 个不恢复，后续由
    scripts/build_kb_index.py 重新生成。
  - 边：按 edges 数组原样重建「两端点都在已恢复节点中」的边（旧 id → 新 id
    映射）。RELATED_TO 导出时已含双向对，原样建不补反向；REVISED_BY 单向原样建。
    注意：导出中另有 68 条 RELATED_TO 是 kb_chunk↔kb_chunk 之间的边，其端点
    不在本次恢复范围（kb 重建后 id 全变），本脚本跳过它们——kb 重建后由
    scripts/graph_edges.py 按 source_path 幂等重建（该脚本职责所在）。

实现说明（重要）：
  - 全程使用「单个持久 db 句柄」（with store._acquire() as db: 包住全部操作），
    而不是 store.insert_node / store.create_edge 的「每次调用开/关库」模式。
    原因：实测 triviumdb 0.7.6 的 close() 释放锁是异步的（后台 flush 线程），
    同一进程内紧邻的再次 open 会偶发抛 "Database locked ... already opened
    by another process"（窗口最长数秒，重试也未必恢复，第一次运行即因此中断）。
    单句柄模式下不存在重复 open，彻底规避该问题。
  - 节点插入用 db.insert(emb, payload)：与 store.insert_node 相比省掉了 Python
    层的字段改写（status→"active" / created_at→None / label→""），payload
    原样入库，无需事后 update_payload 还原。
  - 索引（type/importance/status/character_name）在插入完成后各建一次
    （store.insert_node 是每次插入都建，终态一致）。
  - 检索冒烟用 db.search / 余弦 Top-K，在同一个句柄上计算，不再开新库。

重启安全（幂等）：
  - 目标库为空（0 节点）→ 全新恢复 + 验证。
  - 目标库已有 67 个节点 → 跳过恢复，直接验证（上次运行中断后的续跑）。
  - 其他节点数 → 中止并提示，避免重复插入。

安全约束：
  - DB_PATH 环境变量指向 data/mh_memory_new.db（副本），不碰正式库
    data/mh_memory.db；不删除任何已有文件。
  - Ollama embedding 预检失败即中止，不硬来。

运行：
    venv/Scripts/python.exe scripts/rebuild_db.py
（脚本内部自动把 DB_PATH 设为 data/mh_memory_new.db）
"""

import json
import os
import sys
from collections import Counter

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

# 关键：必须在 import config 之前设置 DB_PATH（Config.DB_PATH 在类定义时读 env；
# .env 里的 DB_PATH=data/mh_memory.db 不会覆盖已设置的变量）
os.environ["DB_PATH"] = os.path.join(
    _PROJECT_ROOT, "data", "mh_memory_new.db"
).replace("\\", "/")

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402

EXPORT_PATH = os.path.join("data", "export_backup_20260824.json")
EXPECTED_NODES = 67   # 非 kb_chunk 节点数（event 57 / character_state 5 / plot_plan 3 / inspiration 1 / user_intent 1）
EXPECTED_EDGES = 76   # 两端点都在恢复节点内的边数（REVISED_BY 62 + RELATED_TO 14）
DEFERRED_EDGES = 68   # kb↔kb RELATED_TO（端点未恢复，待 kb 重建后由 graph_edges.py 重建）
INDEX_FIELDS = ("type", "importance", "status", "character_name")


def load_export(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def embed_or_abort(store: TriviumStore, text: str) -> list:
    """embed_text 失败会返回零向量，这里显式检测：零向量即中止。"""
    emb = store.embed_text(text)
    if not emb or len(emb) != Config.OLLAMA_EMBEDDING_DIM or not any(emb):
        raise RuntimeError(
            f"Ollama embedding 失败（len={len(emb) if emb else 0}, "
            f"期望 {Config.OLLAMA_EMBEDDING_DIM} 且非零向量）。"
            f"请确认 Ollama 服务运行中且模型 {Config.OLLAMA_EMBEDDING_MODEL} 可用，"
            f"然后重跑本脚本。已中止，未写入任何数据。"
        )
    return emb


def restore(db, store: TriviumStore, nodes: list, edges: list) -> dict:
    """在已打开的句柄上恢复非 kb_chunk 节点 + 可映射边，返回 {旧id: 新id}。"""
    restore_nodes = [n for n in nodes if n["payload"].get("type") != "kb_chunk"]
    print(f"恢复节点: {len(restore_nodes)}（kb_chunk {len(nodes) - len(restore_nodes)} 个跳过，后续由 build_kb_index.py 重建）")

    id_map: dict[int, int] = {}
    type_counter: Counter = Counter()
    for i, n in enumerate(restore_nodes, 1):
        payload = n["payload"]
        ntype = payload.get("type", "unknown")
        type_counter[ntype] += 1
        # 取文本：优先 content 字段，否则用 payload JSON 序列化
        text = payload.get("content") or json.dumps(payload, ensure_ascii=False)
        emb = embed_or_abort(store, text)
        new_id = db.insert(emb, payload)  # 原样入库（无字段改写）
        id_map[n["node_id"]] = new_id
        if i % 10 == 0 or i == len(restore_nodes):
            print(f"  [{i}/{len(restore_nodes)}] 旧id={n['node_id']} -> 新id={new_id} ({ntype})")
    print(f"节点恢复完成: {sum(type_counter.values())} 个 | 分布: {dict(type_counter)}")

    # 索引各建一次（与 store.insert_node 的终态一致）
    for field in INDEX_FIELDS:
        db.create_index(field)

    created: Counter = Counter()
    deferred: Counter = Counter()
    for e in edges:
        src, dst = id_map.get(e["source_id"]), id_map.get(e["target_id"])
        if src is None or dst is None:
            deferred[e["label"]] += 1
            continue
        db.link(src, dst, label=e["label"], weight=e.get("weight", 0.9))
        created[e["label"]] += 1
    print(
        f"建边完成: {sum(created.values())} 条（{dict(created)}）| "
        f"跳过 {sum(deferred.values())} 条 kb↔kb（{dict(deferred)}，端点未恢复，"
        f"kb 重建后由 graph_edges.py 重建）"
    )
    return id_map


def verify(db, store: TriviumStore, nodes: list, edges: list, id_map: dict) -> None:
    """在已打开的句柄上验证：节点数 / 边数 / payload 一致性 / 边逐条核对 / 检索冒烟。"""
    print("\n=== 验证 ===")

    # ---- 节点数 ----
    ids = db.all_node_ids()
    ncount = db.node_count()
    print(f"节点数: all_node_ids={len(ids)} node_count={ncount} | 期望 {EXPECTED_NODES}")

    # ---- 按 payload 深度匹配重建 旧id->新id 映射（同时充当 payload 抽查）----
    export_non_kb = [n for n in nodes if n["payload"].get("type") != "kb_chunk"]
    if not id_map:  # 续跑模式：id_map 未知，用 payload 匹配重建
        by_content = {}
        for n in export_non_kb:
            key = json.dumps(n["payload"], ensure_ascii=False, sort_keys=True)
            by_content.setdefault(key, []).append(n["node_id"])
        for nid in ids:
            node = db.get(nid)
            key = json.dumps(node.payload, ensure_ascii=False, sort_keys=True)
            for old_id in by_content.get(key, []):
                if old_id not in id_map:
                    id_map[old_id] = nid
                    break
    matched = sum(1 for n in export_non_kb if n["node_id"] in id_map)
    print(f"payload 一致性: {matched}/{len(export_non_kb)} 个节点与导出 JSON 完全匹配")
    if matched != len(export_non_kb):
        missing = [n["node_id"] for n in export_non_kb if n["node_id"] not in id_map]
        print(f"  ! 未匹配节点: {missing[:10]}")

    # ---- 边数 + label 分布 ----
    edge_total = 0
    edge_labels = Counter()
    for nid in ids:
        for edge in db.get_edges(nid):
            edge_total += 1
            edge_labels[edge.label] += 1
    print(f"边数: 实际 {edge_total} 条（{dict(edge_labels)}）| 期望 {EXPECTED_EDGES} 条")

    # ---- 边逐条核对（导出边 -> 新库边，映射后端点 + label + weight）----
    expect_edges = [e for e in edges
                    if e["source_id"] in id_map and e["target_id"] in id_map]
    mismatch = 0
    for e in expect_edges:
        new_src, new_dst = id_map[e["source_id"]], id_map[e["target_id"]]
        found = [
            x for x in db.get_edges(new_src)
            if x.target_id == new_dst and x.label == e["label"]
            and abs(float(x.weight or 0.0) - float(e.get("weight", 0.9))) < 1e-6
        ]
        if not found:
            mismatch += 1
            print(f"  [边缺失] {e['source_id']}->{e['target_id']} ({e['label']})")
    print(f"边逐条核对: {len(expect_edges) - mismatch}/{len(expect_edges)} 条存在且 label/weight 一致"
          if mismatch == 0 else f"边逐条核对: {mismatch}/{len(expect_edges)} 条缺失！")

    # ---- RELATED_TO 双向对称性（已恢复部分应成对）----
    pairs = set()
    for e in expect_edges:
        if e["label"] == "RELATED_TO":
            pairs.add((min(e["source_id"], e["target_id"]),
                       max(e["source_id"], e["target_id"])))
    rel_count = sum(1 for e in expect_edges if e["label"] == "RELATED_TO")
    print(f"RELATED_TO 已恢复: {rel_count} 条有向边 = {len(pairs)} 个双向对")

    # ---- 检索冒烟（query: 派兵 练兵）----
    print("\n=== 检索冒烟（query: 派兵 练兵）===")
    qemb = store.embed_text("派兵 练兵")
    import numpy as np  # noqa: E402
    qv = np.array(qemb)
    scored = []
    for nid in ids:
        node = db.get(nid)
        vec = np.array(node.vector)
        norm = np.linalg.norm(vec)
        if norm == 0:
            continue
        score = float(np.dot(qv, vec) / (np.linalg.norm(qv) * norm))
        scored.append((score, nid, node.payload))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        print("  ! 未召回任何结果")
    for score, nid, payload in scored[:3]:
        print(f"  id={nid} score={score:.4f} type={payload.get('type')} "
              f"content={str(payload.get('content', ''))[:60]!r}")


def main() -> None:
    data = load_export(EXPORT_PATH)
    nodes, edges = data["nodes"], data["edges"]
    print(f"导出数据: {len(nodes)} 节点 / {len(edges)} 边")
    print(f"目标库  : {Config.DB_PATH}")

    if Config.DB_PATH.endswith("/data/mh_memory.db"):
        raise RuntimeError("DB_PATH 仍指向正式库 data/mh_memory.db，拒绝执行！")

    # ---- 预检：Ollama embedding ----
    print(f"[预检] Ollama embedding 模型: {Config.OLLAMA_EMBEDDING_MODEL} ...")
    store = TriviumStore()
    probe = embed_or_abort(store, "连接测试（预检）")
    print(f"[预检] OK：{len(probe)} 维非零向量")

    # ---- 单句柄：幂等判断 -> 恢复（如需要）-> 验证 ----
    id_map: dict[int, int] = {}
    with store._acquire() as db:  # 全程一个句柄，规避 0.7.6 开/关锁竞态
        existing = db.node_count()
        if existing == 0:
            print(f"\n目标库为空（{existing} 节点），执行全新恢复 ...")
            id_map = restore(db, store, nodes, edges)
        elif existing == EXPECTED_NODES:
            print(f"\n目标库已有 {existing} 个节点（上次运行已恢复），跳过恢复，直接验证 ...")
        else:
            raise RuntimeError(
                f"目标库已有 {existing} 个节点（期望 0 或 {EXPECTED_NODES}），"
                f"状态异常，中止以避免重复插入。"
            )
        verify(db, store, nodes, edges, id_map)

    print("\n=== 重建完成 ===")
    print(f"库文件     : {Config.DB_PATH}")
    print(f"恢复节点   : {EXPECTED_NODES}（event/character_state/plot_plan/inspiration/user_intent）")
    print(f"建边       : {EXPECTED_EDGES} 条（REVISED_BY 62 / RELATED_TO 14）")
    print(f"跳过边     : {DEFERRED_EDGES} 条 kb↔kb RELATED_TO（待 build_kb_index.py + graph_edges.py 重建）")
    print(f"正式库     : 未触碰（data/mh_memory.db 保持原样）")


if __name__ == "__main__":
    main()
