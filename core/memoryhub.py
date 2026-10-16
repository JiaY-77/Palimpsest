"""MemoryHub 记忆包读侧
=====================

小帕（Palimpsest）的自包含 JSON 记忆包：只要有本模块就能把记忆包「解开读取」，
先当故事听，再挑着入籍（入籍/回写目标库不在本模块范围）。

名字渊源：小帕的 DB 文件就叫 ``data/mh_memory.db``（``mh`` = MemoryHub，
主人 2024 年初的原型名）。

本模块只做**读侧**：
  - ``build_pack``     从 TriviumStore 产出一个自包含记忆包 dict；
  - ``read_pack``      读回文件、校验格式/版本、扫描敏感信息（只读）；
  - ``summarize_pack`` 生成人读摘要（故事 + 节点预览）；
  - ``pack_fingerprint`` 计算节点+边的稳定指纹。

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
                edges.append(
                    {"source_id": source_id, "target_id": target_id, "label": label, "weight": weight}
                )
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
    issues = []
    type_counts: dict = {}
    for item in nodes:
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
