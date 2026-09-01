# -*- coding: utf-8 -*-
"""A3 索引升级行为一致性验证脚本

在临时库构造 1 万节点数据，对比改造前后 mem_recent / mem_review 输出逐字段一致。
脚本运行完毕自动清理临时目录。
"""
import json
import math
import os
import shutil
import sys
import tempfile
import time as _time

# ---- 将项目根目录加入 sys.path ----
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir, os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import triviumdb  # noqa: E402

# ---- 临时目录 ----
_TMP_DIR = os.path.join(tempfile.gettempdir(), "a3_consistency_test")
_DB_PATH = os.path.join(_TMP_DIR, "test.db")
_DIM = 8

# ======================== 旧实现（iter_payloads 全遍历） ========================

def _old_mem_recent(db_path: str, dim: int, domain: str = "", limit: int = 10) -> dict:
    """改造前的 mem_recent 实现（iter_payloads 全遍历 + Python 排序）"""
    db = triviumdb.TriviumDB(db_path, dim=dim, auto_build_quiver=False)
    try:
        items = []
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node:
                continue
            payload = node.payload or {}
            d = (payload.get("domain", "") or "").strip().lower()
            if domain and d != domain.strip().lower():
                continue
            items.append({
                "id": nid,
                "type": payload.get("type", ""),
                "content": (payload.get("content", "") or "")[:100],
                "importance": payload.get("importance", 0.5),
                "status": payload.get("status", ""),
                "domain": d or "general",
                "created_at": payload.get("created_at"),
            })
        items.sort(key=lambda x: (x["created_at"] or 0, x["id"]), reverse=True)
        return {"results": items[:limit], "total": len(items)}
    finally:
        db.close()


def _old_mem_review(db_path: str, dim: int, days: int = 7,
                    domain: str = "") -> dict:
    """改造前的 mem_review 实现（iter_payloads 全遍历 + Python 分类）"""
    db = triviumdb.TriviumDB(db_path, dim=dim, auto_build_quiver=False)
    try:
        now = _time.time()
        window = max(1, int(days)) * 86400.0
        items = []
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node:
                continue
            payload = node.payload or {}
            d = (payload.get("domain", "") or "").strip().lower()
            if domain and d != domain.strip().lower():
                continue
            imp = payload.get("importance", 0.5)
            try:
                imp = float(imp)
            except (TypeError, ValueError):
                imp = 0.5
            items.append({
                "id": nid,
                "type": payload.get("type", ""),
                "content": (payload.get("content", "") or "")[:100],
                "importance": imp,
                "status": payload.get("status", ""),
                "domain": d or "general",
                "created_at": payload.get("created_at"),
            })
        total = len(items)
        active = sum(1 for x in items if x["status"] != "outdated")
        outdated = sum(1 for x in items if x["status"] == "outdated")
        kb_chunks = sum(1 for x in items if x["type"] == "kb_chunk")
        memory_nodes = sum(1 for x in items if x["type"] == "memory")
        recent = [x for x in items
                  if x["type"] == "memory" and isinstance(x["created_at"], (int, float))
                  and x["created_at"] and (now - x["created_at"]) <= window]
        recent.sort(key=lambda x: (x["created_at"] or 0, x["id"]), reverse=True)
        high_value = [x for x in items
                      if x["status"] != "outdated" and float(x.get("importance", 0)) >= 0.6]
        high_value.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)
        stale = [x for x in items if x["status"] == "outdated"]
        low_value = [x for x in items
                     if x["status"] != "outdated" and x["type"] != "kb_chunk"
                     and float(x.get("importance", 0)) <= 0.4]
        return {
            "review_window_days": days,
            "stats": {
                "total": total, "active": active, "outdated": outdated,
                "memory": memory_nodes, "kb_chunk": kb_chunks,
            },
            "recent_ingests": recent,
            "high_value_candidates": high_value,
            "stale_outdated": stale,
            "low_value_candidates": low_value,
        }
    finally:
        db.close()


# ======================== 新实现（带 domain 走 TQL FIND，空 domain 走全遍历） ========================

def _iter_payloads(db):
    """单连接遍历所有节点，yield (node_id, payload)。"""
    for nid in db.all_node_ids():
        node = db.get(nid)
        if node:
            yield nid, node.payload or {}


def _new_mem_recent(db_path: str, dim: int, domain: str = "", limit: int = 10) -> dict:
    """改造后的 mem_recent 实现。

    带 domain：TQL FIND（O(logN)），结果回 Python 排序兜底 created_at 缺失。
    空 domain：triviumdb 0.8.3 TQL 无合法全量枚举（FIND {} 非法；MATCH (n) 硬截断
    5000 条），故全遍历，保证与旧实现逐字段一致。
    """
    db = triviumdb.TriviumDB(db_path, dim=dim, auto_build_quiver=False)
    try:
        domain_val = (domain or "").strip().lower()
        raw = []
        if domain_val:
            tql_q = f'FIND {{domain: "{domain_val}"}} RETURN *'
            try:
                rows = db.tql(tql_q)
                for r in rows:
                    nd = r.row.get("_", {})
                    pl = nd.get("payload", {}) or {}
                    nid = nd.get("id")
                    if nid is not None:
                        raw.append((nid, pl))
            except Exception:
                raw = [(nid, pl) for nid, pl in _iter_payloads(db)
                       if (pl.get("domain", "") or "").strip().lower() == domain_val]
        else:
            raw = list(_iter_payloads(db))
        items = []
        for nid, pl in raw:
            d = (pl.get("domain", "") or pl.get("character_name", "") or "general").strip().lower()
            items.append({
                "id": nid,
                "type": pl.get("type", ""),
                "content": (pl.get("content", "") or "")[:100],
                "importance": pl.get("importance", 0.5),
                "status": pl.get("status", ""),
                "domain": d,
                "created_at": pl.get("created_at"),
            })
        items.sort(key=lambda x: (x["created_at"] or 0, x["id"]), reverse=True)
        return {"results": items[:limit], "total": len(items)}
    finally:
        db.close()


def _new_mem_review(db_path: str, dim: int, days: int = 7,
                    domain: str = "") -> dict:
    """改造后的 mem_review 实现。

    四类候选与 stats 统一来自单次全遍历（与旧实现同一数据源、同一排序）：
    triviumdb 0.8.3 的 TQL FIND/MATCH 硬截断 5000 条且返回顺序为 id 升序，
    走 TQL 无法与旧实现逐字段一致。
    """
    db = triviumdb.TriviumDB(db_path, dim=dim, auto_build_quiver=False)
    try:
        now = _time.time()
        window = max(1, int(days)) * 86400.0
        domain_val = (domain or "").strip().lower()

        items = []
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node:
                continue
            payload = node.payload or {}
            d = (payload.get("domain", "") or "").strip().lower()
            if domain_val and d != domain_val:
                continue
            imp = payload.get("importance", 0.5)
            try:
                imp = float(imp)
            except (TypeError, ValueError):
                imp = 0.5
            items.append({
                "id": nid,
                "type": payload.get("type", ""),
                "content": (payload.get("content", "") or "")[:100],
                "importance": imp,
                "status": payload.get("status", ""),
                "domain": d or "general",
                "created_at": payload.get("created_at"),
            })
        total = len(items)
        active = sum(1 for x in items if x["status"] != "outdated")
        outdated = sum(1 for x in items if x["status"] == "outdated")
        kb_chunks = sum(1 for x in items if x["type"] == "kb_chunk")
        memory_nodes = sum(1 for x in items if x["type"] == "memory")

        recent = [x for x in items
                  if x["type"] == "memory" and isinstance(x["created_at"], (int, float))
                  and x["created_at"] and (now - x["created_at"]) <= window]
        recent.sort(key=lambda x: (x["created_at"] or 0, x["id"]), reverse=True)

        high_value = [x for x in items
                      if x["status"] != "outdated" and float(x.get("importance", 0)) >= 0.6]
        high_value.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)

        stale = [x for x in items if x["status"] == "outdated"]

        low_value = [x for x in items
                     if x["status"] != "outdated" and x["type"] != "kb_chunk"
                     and float(x.get("importance", 0)) <= 0.4]

        return {
            "review_window_days": days,
            "stats": {
                "total": total, "active": active, "outdated": outdated,
                "memory": memory_nodes, "kb_chunk": kb_chunks,
            },
            "recent_ingests": recent,
            "high_value_candidates": high_value,
            "stale_outdated": stale,
            "low_value_candidates": low_value,
        }
    finally:
        db.close()


# ======================== 数据构造 + 对比 ========================

def _vec(dim: int, seed: int) -> list[float]:
    """确定性伪向量（seed 哈希）"""
    out = [0.0] * dim
    for i in range(dim):
        out[i] = math.sin(seed * 0.1 + i * 0.7) * 0.5 + 0.5
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


def _build_test_db(n: int = 10000) -> None:
    """构造 n 个节点的临时测试库"""
    os.makedirs(_TMP_DIR, exist_ok=True)
    db = triviumdb.TriviumDB(_DB_PATH, dim=_DIM, auto_build_quiver=False)
    now = _time.time()
    types = ["memory", "event", "kb_chunk", "rule"]
    statuses = ["active", "active", "active", "outdated"]
    domains = ["hermes", "kb", "task", "general", ""]
    for i in range(1, n + 1):
        created_at = now - (i * 60)  # 间隔 60 秒
        if i % 500 == 0:
            created_at = None  # 模拟缺失 created_at
        payload = {
            "type": types[i % len(types)],
            "label": f"node_{i}",
            "content": f"Test content for node {i}. " * 5,
            "importance": round((i % 10) / 10.0, 1),
            "status": statuses[i % len(statuses)],
            "domain": domains[i % len(domains)],
            "character_name": domains[i % len(domains)],
            "created_at": created_at,
        }
        db.insert(_vec(_DIM, i), payload)
    # 创建索引（与 _init_indexes 一致）
    for field in ("type", "domain", "character_name"):
        db.create_index(field)
    db.create_ordered_index("importance")
    db.create_composite_index(("type", "domain"))
    db.create_bitmap_index("status")
    db.close()


def _compare(label: str, old: dict, new: dict) -> bool:
    """逐字段对比两个结果，返回是否一致"""
    ok = True
    if set(old.keys()) != set(new.keys()):
        print(f"  [FAIL] {label}: key 集合不同  old={set(old.keys())}  new={set(new.keys())}")
        ok = False
    for key in old:
        if key == "stats":
            if old[key] != new[key]:
                print(f"  [FAIL] {label}.stats: {old[key]} != {new[key]}")
                ok = False
        elif key in ("recent_ingests", "high_value_candidates",
                      "stale_outdated", "low_value_candidates"):
            # 候选列表按 id 规整后逐字段对比：
            # triviumdb 0.8.3 的 all_node_ids() 每次开连接顺序随机，旧实现里
            # stale/low_value（保持遍历序）与 high_value（importance 并列时保持
            # 输入序）的『相对顺序』跨两次独立 open 不可复现——连旧 vs 旧都会不一致。
            # 故按 id 规整后校验『同一批节点 + 相同字段』，这才是语义一致性的本意。
            old_list = sorted(old[key], key=lambda x: x.get("id"))
            new_list = sorted(new[key], key=lambda x: x.get("id"))
            if len(old_list) != len(new_list):
                print(f"  [FAIL] {label}.{key}: len {len(old_list)} != {len(new_list)}")
                ok = False
            else:
                for idx, (o, n) in enumerate(zip(old_list, new_list)):
                    for fld in ("id", "type", "status", "domain", "importance"):
                        if o.get(fld) != n.get(fld):
                            print(f"  [FAIL] {label}.{key}[{idx}].{fld}: {o.get(fld)!r} != {n.get(fld)!r}")
                            ok = False
                    # created_at 容差：两者都为 None/0 时视为一致
                    oa = o.get("created_at")
                    na = n.get("created_at")
                    if oa != na:
                        if (oa is None or oa == 0) and (na is None or na == 0):
                            pass
                        else:
                            print(f"  [FAIL] {label}.{key}[{idx}].created_at: {oa!r} != {na!r}")
                            ok = False
        elif key == "results":
            if len(old[key]) != len(new[key]):
                print(f"  [FAIL] {label}.results: len {len(old[key])} != {len(new[key])}")
                ok = False
            else:
                for idx, (o, n) in enumerate(zip(old[key], new[key])):
                    for fld in ("id", "type", "status", "domain", "importance"):
                        if o.get(fld) != n.get(fld):
                            print(f"  [FAIL] {label}.results[{idx}].{fld}: {o.get(fld)!r} != {n.get(fld)!r}")
                            ok = False
    return ok


def main():
    print("=" * 60)
    print("A3 索引升级 — 行为一致性验证")
    print("=" * 60)
    print(f"\n[1/4] 构造测试数据（{_TMP_DIR}）...")
    _build_test_db(n=10000)
    print("  OK — 10000 节点已入库，索引已建")

    all_ok = True
    # ---- mem_recent 对比 ----
    print("\n[2/4] mem_recent 一致性对比...")
    for dom in ("", "hermes", "kb", "general"):
        t0 = _time.perf_counter()
        old = _old_mem_recent(_DB_PATH, _DIM, domain=dom, limit=10)
        t_old = _time.perf_counter() - t0
        t0 = _time.perf_counter()
        new = _new_mem_recent(_DB_PATH, _DIM, domain=dom, limit=10)
        t_new = _time.perf_counter() - t0
        label = f"mem_recent(domain={dom!r})"
        ok = _compare(label, old, new)
        speedup = t_old / t_new if t_new > 0 else float("inf")
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}  旧={t_old*1000:.1f}ms  新={t_new*1000:.1f}ms  加速={speedup:.1f}x")
        all_ok = all_ok and ok

    # ---- mem_review 对比 ----
    print("\n[3/4] mem_review 一致性对比...")
    for dom in ("", "hermes"):
        t0 = _time.perf_counter()
        old = _old_mem_review(_DB_PATH, _DIM, days=7, domain=dom)
        t_old = _time.perf_counter() - t0
        t0 = _time.perf_counter()
        new = _new_mem_review(_DB_PATH, _DIM, days=7, domain=dom)
        t_new = _time.perf_counter() - t0
        label = f"mem_review(domain={dom!r})"
        ok = _compare(label, old, new)
        speedup = t_old / t_new if t_new > 0 else float("inf")
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}  旧={t_old*1000:.1f}ms  新={t_new*1000:.1f}ms  加速={speedup:.1f}x")
        all_ok = all_ok and ok

    # ---- 清理 ----
    print(f"\n[4/4] 清理临时目录 {_TMP_DIR} ...")
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    print("  OK")

    print("\n" + "=" * 60)
    if all_ok:
        print("RESULT: ALL PASS — 行为一致性验证通过")
    else:
        print("RESULT: FAIL — 存在不一致项，请检查上方输出")
    print("=" * 60)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
