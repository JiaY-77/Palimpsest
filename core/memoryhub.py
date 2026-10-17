"""MemoryHub 记忆包读写
=====================

Palimpsest 的自包含 JSON 记忆包：只要有本模块就能把记忆包「解开读取」，
先当故事听，再挑着入籍（把包内节点写回本库）。

名字渊源：Palimpsest 的 DB 文件就叫 ``data/mh_memory.db``（``mh`` = MemoryHub，
用户 2024 年初的原型名）。

本模块做**读侧 + 入籍落库**：
  - ``build_pack``     从 TriviumStore 产出一个自包含记忆包 dict；
  - ``read_pack``      读回文件、校验格式/版本、扫描敏感信息（只读）；
  - ``summarize_pack`` 生成人读摘要（故事 + 节点预览）；
  - ``pack_fingerprint`` 计算节点+边的稳定指纹；
  - ``apply_import``   把 ``core.strategy.decide_import`` 的判定结果落回本库。

判定（选哪些节点入籍、payload 怎么改写）归本体 ``core.strategy.decide_import``；
本模块只负责**执行**那份判定（写库），三通道（MCP/REST/CLI）只做薄壳。

遍历语义照 ``scripts/export_all_data.py`` 的 ``export_via_store`` 独立实现
（不 import 该脚本——它含 argparse/main，导入有副作用）。triviumdb 0.8.6 起
同一进程禁止嵌套双开连接，故节点与边在同一 ``db`` 连接内读完。
"""

import contextlib
import hashlib
import json
import os
import platform
import time

from core.secret_scan import scan_secret
from core.utils import _to_float
from core.version import get_version

MEMORYHUB_FORMAT = "memoryhub"
MEMORYHUB_VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})


def build_pack(store, *, name: str = "", story: str = "") -> dict:
    """从 TriviumStore 产出 MemoryHub 记忆包（单连接遍历节点与边）。

    边与节点都在同一个 ``db`` 连接内读完，遍历期间不调用 store 的其它方法
    （避免同进程嵌套双开连接报 ``Database locked``）。
    """
    nodes = []
    edges = []
    seen_edges = set()

    db = store._acquire()
    try:
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node or not (node.payload or {}):
                print(f"  警告: 节点 {nid} 读取失败，跳过")
                continue
            payload = node.payload or {}
            nodes.append({"node_id": nid, "payload": payload})

            for edge in db.get_edges(nid) or []:
                source_id = getattr(edge, "source_id", None) or nid
                target_id = edge.target_id
                label = getattr(edge, "label", "")
                weight = getattr(edge, "weight", None)
                key = (source_id, target_id, label)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append({"source_id": source_id, "target_id": target_id, "label": label, "weight": weight})
    finally:
        with contextlib.suppress(Exception):
            db.close()

    fingerprint = pack_fingerprint({"nodes": nodes, "edges": edges})
    manifest = {
        "name": name,
        "kind": "memory_pack",
        "created_at": time.time(),
        "producer": f"palimpsest {get_version()} @ {platform.node()}",
        "origin_db": os.path.basename(str(store.db_path)),
        "fingerprint": fingerprint,
        "node_total": len(nodes),
        "edge_total": len(edges),
        "story": story,
    }
    return {
        "format": MEMORYHUB_FORMAT,
        "format_version": MEMORYHUB_VERSION,
        "manifest": manifest,
        "nodes": nodes,
        "edges": edges,
    }


def read_pack(path) -> dict:
    """读回记忆包文件并按顺序校验（只读；失败一律返回 ok=False + code + 中文 error）。

    校验通过后逐条扫描节点 content：命中敏感规则记 ``secret`` issue，
    content 缺失或空记 ``empty_content`` issue。函数内不得调用任何写接口。
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        return {"ok": False, "code": "unreadable", "error": f"记忆包不可读：{path}（{e}）"}

    try:
        pack = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "code": "not_json", "error": "不是合法的 JSON 文件"}

    if not isinstance(pack, dict):
        return {"ok": False, "code": "not_json", "error": "不是合法的 JSON 文件"}

    if pack.get("format") != MEMORYHUB_FORMAT:
        return {
            "ok": False,
            "code": "not_memoryhub",
            "error": '不是记忆包：缺少 format 字段，或值不是 "memoryhub"',
        }

    version = pack.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        return {
            "ok": False,
            "code": "unsupported_version",
            "error": f"记忆包格式版本 {version} 不受支持；本机支持 {sorted(SUPPORTED_VERSIONS)}",
        }

    nodes = pack.get("nodes", [])
    edges = pack.get("edges", [])
    # 脏包守卫：nodes/edges 必须是列表，否则后续逐条扫描会在非 list 上出错。
    # 放在版本校验之后（既有四分支文案/顺序逐字不变），只拦截形状不对的包。
    if not isinstance(nodes, list):
        return {"ok": False, "code": "malformed", "error": "记忆包的 nodes 字段必须是列表"}
    if not isinstance(edges, list):
        return {"ok": False, "code": "malformed", "error": "记忆包的 edges 字段必须是列表"}

    issues: list = []
    type_counts: dict = {}
    for item in nodes:
        if not isinstance(item, dict):
            issues.append({"node_id": None, "kind": "malformed_node", "rules": []})
            continue  # 非 dict 元素跳过，type_counts 也不计它
        payload = item.get("payload") or {}
        nid = item.get("node_id")
        ntype = payload.get("type", "unknown")
        type_counts[ntype] = type_counts.get(ntype, 0) + 1
        content = payload.get("content", "") or ""
        rules = scan_secret(content)
        if rules:
            issues.append({"node_id": nid, "kind": "secret", "rules": rules})
        if not content:
            issues.append({"node_id": nid, "kind": "empty_content", "rules": []})

    return {
        "ok": True,
        "manifest": pack.get("manifest", {}),
        "nodes": nodes,
        "edges": edges,
        "issues": issues,
        "stats": {
            "node_total": len(nodes),
            "edge_total": len(edges),
            "type_counts": type_counts,
        },
    }


def summarize_pack(pack: dict, *, preview_chars: int = 80) -> dict:
    """生成人读摘要：透传 manifest/story，给每条节点一句预览。"""
    manifest = pack.get("manifest", {}) or {}
    nodes = []
    for item in pack.get("nodes", []):
        payload = item.get("payload") or {}
        content = payload.get("content", "") or ""
        nodes.append(
            {
                "node_id": item.get("node_id"),
                "type": payload.get("type", "unknown"),
                "importance": _to_float(payload.get("importance"), 0.0),
                "domain": payload.get("domain", ""),
                "preview": content[:preview_chars],
            }
        )
    return {
        "ok": True,
        "manifest": manifest,
        "story": manifest.get("story", ""),
        "nodes": nodes,
        "edges_total": len(pack.get("edges", [])),
        "issues": pack.get("issues", []),
    }


def pack_fingerprint(pack: dict) -> str:
    """节点+边（不含 manifest，避免自引用）的稳定 sha256 前 16 位十六进制。"""
    core = {"nodes": pack.get("nodes", []), "edges": pack.get("edges", [])}
    blob = json.dumps(core, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def apply_import(store, plan: dict) -> dict:
    """执行 ``core.strategy.decide_import`` 的判定：把入籍节点写回本库。

    ``plan`` = ``core.strategy.decide_import(...)`` 的返回值。逐条把
    ``plan["nodes"]`` 的 payload 写进 ``store``（embed → insert_node），成功记
    ``origin_id → new_id`` 映射；随后只建**两端都入籍**的边。单条失败收集进
    ``failed`` / ``edges_failed`` 并继续处理下一条，不让一条坏节点毁掉整批。

    原子性（刻意为之，不是疏漏）：本函数**不做跨节点整体回滚**——每个节点由
    ``store.insert_node`` 独立提交，逐条原子；失败节点收集进 ``failed`` 而非
    回滚整批。理由：入籍是「挑着搬」的可交互操作，一条坏包不应让已选中的好
    节点全部作废。但**成功的节点必须全部落盘**（不能有写完不提交的半状态）。

    本函数不调用 ``resolve_conflict`` / 任何冲突检测——入籍是纯新增，不取代
    本库已有记忆（避免误标 outdated）。

    返回 ``{"ok", "imported", "failed", "id_map", "edges_created", "edges_failed"}``；
    ``ok`` 为 True 仅当所有节点都插入成功。
    """
    imported: list[dict] = []
    failed: list[dict] = []
    id_map: dict = {}

    for item in plan.get("nodes", []) or []:
        origin_id = item.get("origin_id")
        payload = item.get("payload") or {}
        try:
            emb = store.embed_text(payload.get("content", ""))
            new_id = store.insert_node(payload, emb)
        except Exception as e:  # noqa: BLE001 —— 单条失败不毁整批，收集后继续
            failed.append({"origin_id": origin_id, "error": str(e)[:200]})
            continue
        imported.append({"origin_id": origin_id, "node_id": new_id})
        id_map[origin_id] = new_id

    edges_created = 0
    edges_failed: list[dict] = []
    for edge in plan.get("edges", []) or []:
        src_origin = edge.get("source_origin_id")
        tgt_origin = edge.get("target_origin_id")
        src_new = id_map.get(src_origin)
        tgt_new = id_map.get(tgt_origin)
        if src_new is None or tgt_new is None:
            continue  # 两端未全部入籍（decide_import 已保证，双保险）
        label = edge.get("label") or "RELATED_TO"
        weight = edge.get("weight")
        try:
            if weight is None:
                store.create_edge(src_new, tgt_new, label)
            else:
                store.create_edge(src_new, tgt_new, label, weight=weight)
        except Exception as e:  # noqa: BLE001 —— 单条边失败继续
            edges_failed.append(
                {
                    "source_origin_id": src_origin,
                    "target_origin_id": tgt_origin,
                    "error": str(e)[:200],
                }
            )
            continue
        edges_created += 1

    return {
        "ok": not failed,
        "imported": imported,
        "failed": failed,
        "id_map": id_map,
        "edges_created": edges_created,
        "edges_failed": edges_failed,
    }
