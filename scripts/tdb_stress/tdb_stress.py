# -*- coding: utf-8 -*-
"""
TriviumDB 0.8.0 高压压测脚本（隔离临时库，不碰 Palimpsest 真实 data/）
维度：顺序批量写 / 单条写 / 多进程并发写 / 同 id 冲突写 / 图谱扩展检索 / 混合读写 / 硬杀恢复 / compact
输出：JSON 报告 + 控制台摘要
"""
import os, sys, time, json, random, math, tempfile, traceback, subprocess, signal
from datetime import datetime
import multiprocessing as mp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import triviumdb

DIM = 1024
BASE = os.path.join(tempfile.gettempdir(), "tdb_stress")
os.makedirs(BASE, exist_ok=True)
DB = os.path.join(BASE, "stress.db")
REPORT = os.path.join(BASE, "report.json")


def rvec(rng=None):
    r = rng if rng else random
    return [r.random() for _ in range(DIM)]


def open_db():
    return triviumdb.TriviumDB(DB, dim=DIM)


def hit_attr(h):
    """SearchHit 字段兜底提取"""
    out = {}
    for a in ("id", "score", "payload", "distance"):
        try:
            out[a] = getattr(h, a)
        except Exception:
            pass
    return out


# ---------------- 阶段函数（顶层，供 multiprocessing spawn） ----------------

def worker_conc_write(worker_id, n, start_id):
    """并发写：每进程独立开库，写入不冲突 id 段"""
    errs = []
    t0 = time.time()
    try:
        db = open_db()
        rng = random.Random(worker_id * 999 + 7)
        for i in range(n):
            nid = start_id + i
            try:
                db.insert_with_id(nid, rvec(rng), {"text": f"cw-{worker_id}-{i}", "worker": worker_id, "n": i})
            except Exception as e:
                errs.append(f"{type(e).__name__}: {e}")
        db.close()
    except Exception as e:
        errs.append(f"FATAL {type(e).__name__}: {e}")
    return {"worker": worker_id, "n": n, "secs": round(time.time() - t0, 3), "errs": errs[:20], "err_count": len(errs)}


def worker_conflict_write(worker_id, ids, rounds):
    """同 id 冲突写：对同一批 id 反复 update_payload + insert_with_id"""
    errs = []
    t0 = time.time()
    try:
        db = open_db()
        rng = random.Random(worker_id * 31 + 5)
        for r in range(rounds):
            nid = ids[r % len(ids)]
            try:
                db.update_payload(nid, {"conflict": worker_id, "round": r, "rand": rng.random()})
            except Exception as e:
                errs.append(f"update {type(e).__name__}: {e}")
        db.close()
    except Exception as e:
        errs.append(f"FATAL {type(e).__name__}: {e}")
    return {"worker": worker_id, "secs": round(time.time() - t0, 3), "errs": errs[:30], "err_count": len(errs)}


def worker_mixed(worker_id, mode, nodes, duration):
    """混合压力 worker：mode=writer 写新节点 / mode=reader 反复 search+get+neighbors"""
    errs = []
    t0 = time.time()
    count = 0
    try:
        db = open_db()
        rng = random.Random(worker_id * 77 + 3)
        while time.time() - t0 < duration:
            if mode == "writer":
                try:
                    db.insert_with_id(10_000_000 + worker_id * 100_000 + count, rvec(rng),
                                      {"text": f"mix-w{worker_id}-{count}", "kind": "mixed"})
                    count += 1
                except Exception as e:
                    errs.append(f"{type(e).__name__}: {e}")
            else:
                try:
                    q = rvec(rng)
                    db.search(q, top_k=5, min_score=0.0)
                    nid = nodes[rng.randrange(len(nodes))]
                    db.get(nid)
                    db.neighbors(nid, depth=1)
                    count += 1
                except Exception as e:
                    errs.append(f"{type(e).__name__}: {e}")
        db.close()
    except Exception as e:
        errs.append(f"FATAL {type(e).__name__}: {e}")
    return {"worker": worker_id, "mode": mode, "ops": count, "secs": round(time.time() - t0, 3),
            "errs": errs[:30], "err_count": len(errs)}


def child_hard_kill(target_secs):
    """被硬杀的子进程：循环写直到被杀"""
    t0 = time.time()
    try:
        db = open_db()
        rng = random.Random(424242)
        i = 0
        while time.time() - t0 < target_secs:
            db.insert_with_id(20_000_000 + i, rvec(rng), {"text": f"kill-{i}", "phase": "hardkill"})
            i += 1
        db.close()
    except Exception:
        pass


# ---------------- 主流程 ----------------

def main():
    report = {"ts": datetime.now().isoformat(), "tdb_version": getattr(triviumdb, "__version__", "?"),
              "dim": DIM, "db": DB, "stages": {}}

    if os.path.exists(DB):
        os.remove(DB)
    # 先看 SearchHit 属性（兜底）
    probe_db = open_db()
    try:
        rid = probe_db.insert(rvec(), {"text": "probe"})
        h = probe_db.search(rvec(), top_k=1, min_score=0.0)[0]
        report["searchhit_attrs"] = list(hit_attr(h).keys())
        report["nodeview_attrs"] = list(getattr(probe_db.get(rid), "__dict__", {}).keys()) if hasattr(probe_db.get(rid), "__dict__") else "no_dict"
    except Exception as e:
        report["probe_err"] = f"{type(e).__name__}: {e}"
    finally:
        probe_db.close()

    # ---- 阶段1：顺序批量写 20000 ----
    t0 = time.time()
    errs1 = []
    try:
        db = open_db()
        rng = random.Random(1)
        batch = 500
        total = 0
        for b in range(20000 // batch):
            vecs = [rvec(rng) for _ in range(batch)]
            payloads = [{"text": f"seq-{total + j}", "phase": "seq", "n": total + j} for j in range(batch)]
            try:
                ids = db.batch_insert(vecs, payloads)
                if len(ids) != batch:
                    errs1.append(f"batch len mismatch: got {len(ids)}")
                total += len(ids)
            except Exception as e:
                errs1.append(f"batch {type(e).__name__}: {e}")
                break
        dt = time.time() - t0
        report["stages"]["seq_batch_20k"] = {"nodes": total, "secs": round(dt, 3),
                                             "rate_per_sec": round(total / dt, 1), "errs": errs1[:20]}
        db.close()
    except Exception as e:
        report["stages"]["seq_batch_20k"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- 阶段2：单条 insert 5000 ----
    t0 = time.time()
    lat = []
    errs2 = []
    try:
        db = open_db()
        rng = random.Random(2)
        for i in range(5000):
            s = time.time()
            try:
                db.insert(rvec(rng), {"text": f"single-{i}", "phase": "single"})
                lat.append((time.time() - s) * 1000)
            except Exception as e:
                errs2.append(f"{type(e).__name__}: {e}")
        dt = time.time() - t0
        lat.sort()
        report["stages"]["single_5k"] = {"nodes": 5000, "secs": round(dt, 3), "rate_per_sec": round(5000 / dt, 1),
                                         "lat_ms": {"p50": round(lat[len(lat)//2], 2), "p90": round(lat[int(len(lat)*0.9)], 2),
                                                    "p99": round(lat[int(len(lat)*0.99)], 2), "max": round(lat[-1], 2)},
                                         "errs": errs2[:20]}
        db.close()
    except Exception as e:
        report["stages"]["single_5k"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- 阶段3：多进程并发写 8×2500 ----
    t0 = time.time()
    try:
        ctx = mp.get_context("spawn")
        with ctx.Pool(8) as pool:
            results = pool.starmap(worker_conc_write, [(w, 2500, 100_000 + w * 2500) for w in range(8)])
        report["stages"]["conc_write_8x2500"] = {"secs": round(time.time() - t0, 3), "workers": results,
                                                 "total_errs": sum(r["err_count"] for r in results)}
    except Exception as e:
        report["stages"]["conc_write_8x2500"] = {"fatal": f"{type(e).__name__}: {e}"}

    # 此时 count 应为 20000+5000+20000 = 45000
    try:
        db = open_db()
        report["count_after_conc"] = db.node_count()
        db.close()
    except Exception as e:
        report["count_after_conc"] = f"ERR {type(e).__name__}: {e}"

    # ---- 阶段4：同 id 冲突写 ----
    try:
        db = open_db()
        ids = list(range(200_000, 200_400))  # 先建 400 个节点
        db.batch_insert_with_ids(ids, [rvec() for _ in range(400)], [{"text": f"conf-{i}"} for i in range(400)])
        db.close()
        t0 = time.time()
        ctx = mp.get_context("spawn")
        with ctx.Pool(4) as pool:
            results = pool.starmap(worker_conflict_write, [(w, ids, 800) for w in range(4)])
        report["stages"]["conflict_4x800"] = {"secs": round(time.time() - t0, 3), "workers": results,
                                              "total_errs": sum(r["err_count"] for r in results)}
    except Exception as e:
        report["stages"]["conflict_4x800"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- 阶段5：图谱 6 万边 + expand 检索 ----
    t0 = time.time()
    errs5 = []
    try:
        db = open_db()
        rng = random.Random(5)
        # 取前 30000 个 id 建随机边（每节点平均 2 条）
        ids = list(range(1, 30001))
        linked = 0
        for i in range(60000):
            a = ids[rng.randrange(len(ids))]
            b = ids[rng.randrange(len(ids))]
            if a == b:
                continue
            try:
                db.link(a, b, label="RELATED_TO", weight=rng.random())
                linked += 1
            except Exception as e:
                errs5.append(f"link {type(e).__name__}: {e}")
        db.close()
        report["stages"]["graph_60k_edges"] = {"linked": linked, "secs": round(time.time() - t0, 3), "errs": errs5[:20]}
    except Exception as e:
        report["stages"]["graph_60k_edges"] = {"fatal": f"{type(e).__name__}: {e}"}

    # expand 检索性能
    t0 = time.time()
    errs5b = []
    qs = []
    try:
        db = open_db()
        rng = random.Random(6)
        for i in range(200):
            try:
                h = db.search(rvec(rng), top_k=10, min_score=0.0, expand_depth=2)
                qs.append(len(h))
            except Exception as e:
                errs5b.append(f"search d2 {type(e).__name__}: {e}")
        dt = time.time() - t0
        report["stages"]["expand_search_200"] = {"queries": 200, "secs": round(dt, 3),
                                                 "avg_results": round(sum(qs)/len(qs), 1) if qs else 0,
                                                 "rate_per_sec": round(200/dt, 2), "errs": errs5b[:20]}
        db.close()
    except Exception as e:
        report["stages"]["expand_search_200"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- 阶段6：混合压力 60 秒 ----
    t0 = time.time()
    try:
        # 读节点池（前 2000 个节点 id）
        node_pool = list(range(1, 2001))
        ctx = mp.get_context("spawn")
        tasks = [("w", w, None, 60) for w in range(4)] + [("r", w, node_pool, 60) for w in range(4)]
        with ctx.Pool(8) as pool:
            results = pool.starmap(worker_mixed, [("writer", w, None, 60) for w in range(4)] +
                                                  [("reader", w, node_pool, 60) for w in range(4)])
        report["stages"]["mixed_60s"] = {"secs": round(time.time() - t0, 3), "workers": results,
                                         "total_ops": sum(r["ops"] for r in results),
                                         "total_errs": sum(r["err_count"] for r in results)}
    except Exception as e:
        report["stages"]["mixed_60s"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- 阶段7：硬杀恢复 ----
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--child-hardkill", "25"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(12)  # 让它写一会儿
        proc.kill()
        proc.wait(timeout=10)
        report["stages"]["hard_kill"] = {"killed_after_secs": 12, "child_retcode": proc.returncode}
    except Exception as e:
        report["stages"]["hard_kill"] = {"fatal": f"{type(e).__name__}: {e}"}

    # 硬杀后重开：能否打开？count 是否一致？
    try:
        db = open_db()
        report["reopen_after_kill"] = {"ok": True, "node_count": db.node_count()}
        # 随机抽查 200 个节点 get
        rng = random.Random(9)
        bad = 0
        total_ids = db.node_count()
        for _ in range(200):
            nid = rng.randrange(1, total_ids + 1)
            try:
                nv = db.get(nid)
                if nv is None:
                    bad += 1
            except Exception:
                bad += 1
        report["reopen_after_kill"]["sample_bad"] = bad
        # search sanity
        try:
            h = db.search(rvec(rng), top_k=5, min_score=0.0)
            report["reopen_after_kill"]["search_ok"] = len(h) >= 0
        except Exception as e:
            report["reopen_after_kill"]["search_err"] = f"{type(e).__name__}: {e}"
        db.close()
    except Exception as e:
        report["reopen_after_kill"] = {"ok": False, "err": f"{type(e).__name__}: {e}"}

    # ---- 阶段8：compact + 最终核对 ----
    t0 = time.time()
    try:
        db = open_db()
        before = db.node_count()
        db.compact()
        after = db.node_count()
        report["stages"]["compact"] = {"before": before, "after": after, "secs": round(time.time() - t0, 3),
                                       "consistent": before == after,
                                       "est_memory_mb": round(db.estimated_memory() / (1024*1024), 2) if hasattr(db, "estimated_memory") else None}
        db.close()
    except Exception as e:
        report["stages"]["compact"] = {"fatal": f"{type(e).__name__}: {e}"}

    # 最终核对：总 count、抽查
    try:
        db = open_db()
        report["final_count"] = db.node_count()
        db.close()
    except Exception as e:
        report["final_count"] = f"ERR {type(e).__name__}: {e}"

    # 磁盘占用
    try:
        report["db_size_mb"] = round(os.path.getsize(DB) / (1024*1024), 2)
    except Exception:
        pass

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    # ---- 控制台摘要 ----
    print("=" * 60)
    print("TriviumDB 高压压测完成")
    print("=" * 60)
    for k, v in report.get("stages", {}).items():
        if isinstance(v, dict) and "fatal" in v:
            print(f"[FAIL] {k}: {v['fatal']}")
        elif isinstance(v, dict):
            extra = ""
            if "rate_per_sec" in v:
                extra = f" 速率={v['rate_per_sec']}/s"
            if "err_count" in v or "errs" in v:
                ec = v.get("err_count", len(v.get("errs", [])))
                extra += f"  错误={ec}"
            if "total_errs" in v:
                extra += f"  总错误={v['total_errs']}"
            print(f"[OK]   {k}  耗时={v.get('secs', '?')}s{extra}")
    print(f"硬杀后重开: {report.get('reopen_after_kill')}")
    print(f"最终节点数: {report.get('final_count')}  库大小: {report.get('db_size_mb', '?')} MB")
    print(f"报告: {REPORT}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child-hardkill":
        child_hard_kill(float(sys.argv[2]) if len(sys.argv) > 2 else 25)
    else:
        main()
