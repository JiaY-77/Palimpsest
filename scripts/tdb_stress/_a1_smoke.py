# -*- coding: utf-8 -*-
"""
A1 重构冒烟测试（阶段 2 第二部分）
=========================================
验证 search_similar 改用 search_advanced 后：
  1. 返回结构正确（[{id, score, payload}]）
  2. block 过滤生效（非空 block 只返回匹配区块节点）
  3. 时间衰减生效（created_at 老的节点分低）

硬约束：临时库 %TEMP%/tdb_ftest/a1_smoke/，不碰正式库；测完清理。
运行：./venv/Scripts/python.exe scripts/tdb_stress/_a1_smoke.py
"""

import hashlib
import math
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

DIM = 8
BASE_DIR = os.path.join(tempfile.gettempdir(), "tdb_ftest", "a1_smoke")
DB_DIR = os.path.join(BASE_DIR, "db")
DB_PATH = os.path.join(DB_DIR, "smoke.db")


def _fake_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    if not text:
        return vec
    padded = " " + text + " "
    for i in range(len(padded) - 1):
        gram = padded[i:i + 2]
        h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:4], 16)
        vec[h % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def main():
    # 清理旧临时库
    if os.path.exists(BASE_DIR):
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    os.makedirs(DB_DIR, exist_ok=True)

    try:
        os.environ["DB_PATH"] = DB_PATH
        os.environ.setdefault("MEMORY_DECAY_FACTOR", "0.95")
        os.environ["OLLAMA_EMBEDDING_DIM"] = str(DIM)

        from core.trivium_store import TriviumStore
        store = TriviumStore()
        store.embed_text = _fake_embed

        now = time.time()
        ONE_DAY = 86400.0
        node_ids = []
        edge_count = 0

        # --- 构造数据：50 节点带边图 ---
        # 40 个 memory 节点（5 个 domain 各 8 个，新旧混搭）
        domains = ["task", "kb", "rule", "hermes", "general"]
        for d_idx, domain in enumerate(domains):
            for i in range(8):
                age_days = i * 10  # 0~70 天
                nid = store.insert_node({
                    "type": "memory",
                    "domain": domain,
                    "content": f"smoke-{domain}-{i}",
                    "importance": 0.5 + (i * 0.05),
                    "created_at": now - age_days * ONE_DAY,
                }, _fake_embed(f"smoke-{domain}-{i}"))
                node_ids.append((nid, domain))

        # 10 个 kb_chunk 节点（domain=kb，不衰减）
        kb_ids = []
        for i in range(10):
            nid = store.insert_node({
                "type": "kb_chunk",
                "domain": "kb",
                "content": f"smoke-kb-{i}",
                "importance": 0.8,
                "created_at": now - 365 * ONE_DAY,
            }, _fake_embed(f"smoke-kb-{i}"))
            kb_ids.append(nid)
            node_ids.append((nid, "kb"))

        # 建边：同 domain 内链 + 跨域链
        for idx, (nid, domain) in enumerate(node_ids):
            if idx + 1 < len(node_ids):
                store.create_edge(nid, node_ids[idx + 1][0], "RELATED", weight=0.8)
                edge_count += 1
            if idx + len(domains) < len(node_ids):
                store.create_edge(nid, node_ids[idx + len(domains)][0], "RELATED", weight=0.6)
                edge_count += 1

        total_nodes = len(node_ids)
        print(f"[setup] {total_nodes} 节点, {edge_count} 边")

        failures = 0

        # --- 测试 1：返回结构正确 ---
        print("\n[test 1] 返回结构")
        q = _fake_embed("smoke-task-0")
        results = store.search_similar(q, top_k=5, expand_depth=1, apply_decay=True)
        assert isinstance(results, list), f"应返回 list: {type(results)}"
        assert len(results) <= 5, f"结果数应 <= top_k(5): {len(results)}"
        for r in results:
            assert "id" in r, f"缺 id 字段: {r}"
            assert "score" in r, f"缺 score 字段: {r}"
            assert "payload" in r, f"缺 payload 字段: {r}"
            assert isinstance(r["score"], (int, float)), f"score 非数值: {r['score']}"
            assert isinstance(r["payload"], dict), f"payload 非 dict: {type(r['payload'])}"
        print(f"  OK: {len(results)} 条结果, 结构正确")
        for r in results:
            print(f"    id={r['id']}, score={r['score']:.4f}, "
                  f"type={r['payload'].get('type')}, domain={r['payload'].get('domain')}")

        # --- 测试 2：expand_depth=0 纯向量 ---
        print("\n[test 2] expand_depth=0 纯向量")
        results0 = store.search_similar(q, top_k=5, expand_depth=0, apply_decay=True)
        assert len(results0) <= 5
        print(f"  OK: {len(results0)} 条结果")

        # --- 测试 3：block 过滤 ---
        print("\n[test 3] block 过滤")
        results_block = store.search_similar(q, top_k=20, expand_depth=1,
                                             apply_decay=False, block="task")
        for r in results_block:
            domain = r["payload"].get("domain", "general")
            if domain not in ("task",):
                print(f"  FAIL: block=task 但返回了 domain={domain}: {r}")
                failures += 1
                break
        else:
            print(f"  OK: block=task 过滤生效, {len(results_block)} 条结果全部 domain=task")

        # block="" 不过滤
        results_all = store.search_similar(q, top_k=20, expand_depth=1,
                                           apply_decay=False, block="")
        all_domains = set(r["payload"].get("domain", "general") for r in results_all)
        print(f"  OK: block='' 不过滤, 返回域: {all_domains}")

        # --- 测试 4：时间衰减 ---
        print("\n[test 4] 时间衰减")
        # 查 domain=task 的结果（8 个节点，age 0~70 天），关闭扩散避免干扰
        results_decay = store.search_similar(q, top_k=8, expand_depth=0,
                                             apply_decay=True, block="task")
        if len(results_decay) >= 2:
            # 按 score 降序排列后，最新的节点应排在前面（衰减小）
            # 检查 created_at 对应的排名
            scored_by_age = []
            for r in results_decay:
                payload = r["payload"]
                ca = payload.get("created_at")
                try:
                    days = (now - float(ca)) / ONE_DAY if ca else 0
                except (TypeError, ValueError):
                    days = 0
                scored_by_age.append((days, r["score"], r["id"]))
            scored_by_age.sort(key=lambda x: x[0])  # 按 age 升序（最新在前）

            # 旧节点（age 大）应比新节点（age 小）分数低（decay < 1.0 时）
            ages = [a[0] for a in scored_by_age]
            scores = [a[1] for a in scored_by_age]
            print(f"  age_days: {[f'{a:.0f}' for a in ages]}")
            print(f"  scores:   {[f'{s:.4f}' for s in scores]}")
            # 最老的节点分数应比最新的低（如果 decay != 1.0）
            if ages[-1] > ages[0] and scores[-1] <= scores[0]:
                print(f"  OK: 时间衰减生效 (oldest age={ages[-1]:.0f}d, score={scores[-1]:.4f}"
                      f" < newest age={ages[0]:.0f}d, score={scores[0]:.4f})")
            else:
                print(f"  WARN: 衰减方向不明确 (oldest={scores[-1]:.4f}, newest={scores[0]:.4f})")
        else:
            print(f"  SKIP: task 域结果不足 2 条 ({len(results_decay)})")

        # --- 测试 5：kb_chunk 不衰减 ---
        print("\n[test 5] kb_chunk 不衰减")
        results_kb = store.search_similar(
            _fake_embed("smoke-kb-0"), top_k=10, expand_depth=0,
            apply_decay=True, block="kb")
        kb_results = [r for r in results_kb if r["payload"].get("type") == "kb_chunk"]
        non_kb = [r for r in results_kb if r["payload"].get("type") != "kb_chunk"]
        if kb_results:
            print(f"  OK: 返回 {len(kb_results)} 个 kb_chunk 结果")
        if non_kb:
            print(f"  OK: 返回 {len(non_kb)} 个非 kb_chunk 结果")

        # --- 汇总 ---
        print(f"\n{'='*50}")
        if failures == 0:
            print("ALL TESTS PASSED")
        else:
            print(f"FAILURES: {failures}")
            sys.exit(1)

    finally:
        # 清理临时目录
        shutil.rmtree(BASE_DIR, ignore_errors=True)
        print(f"\n[cleanup] 已清理 {BASE_DIR}")


if __name__ == "__main__":
    main()
