# -*- coding: utf-8 -*-
"""
TriviumDB 0.8.0 第二轮压测（修正 + 聚焦真实部署形态）
A. 单实例 16 线程混合高压 30s（贴近 REST 单进程多请求）
B. 同 id 并发 upsert（mem_ingest 冲突场景）
C. 大文本 payload（知识 chunk 规模）
D. 真实 id 段 8 万边 + expand_depth=3 检索
E. 硬杀精确对比（杀前 count vs 恢复 count）
F. compact + 全量核对
"""
import os, sys, time, json, random, tempfile, subprocess, gc
from datetime import datetime
import threading

sys.path.insert(0, r"D:\HeJiaQi\Documents\Code\Python\Palimpsest")
import triviumdb

DIM = 1024
BASE = os.path.join(tempfile.gettempdir(), "tdb_stress")
os.makedirs(BASE, exist_ok=True)
DB = os.path.join(BASE, "stress2.db")
REPORT = os.path.join(BASE, "report2.json")


def rvec(rng=None):
    r = rng if rng else random
    return [r.random() for _ in range(DIM)]


def open_db():
    return triviumdb.TriviumDB(DB, dim=DIM)


def long_payload(i):
    """模拟知识 chunk 的大文本 payload"""
    return {"type": "kb_chunk", "text": f"知识块 {i}：小七的压测文本。" + "这是一段模拟知识内容的文字，用于测试大 payload 的写入与检索性能。" * 50,
            "importance": 0.6, "status": "active"}


def main():
    report = {"ts": datetime.now().isoformat(), "dim": DIM, "db": DB, "stages": {}}
    # 清库必须连伴生文件一起删（triviumdb 多文件存储：.vec/.quiver/.wal/.lock/.flush_ok）
    for f in os.listdir(BASE):
        if f.startswith(os.path.basename(DB)) or f.startswith("stress2.db"):
            try:
                os.remove(os.path.join(BASE, f))
            except Exception:
                pass

    # 准备基础库：10000 节点（1..10000）
    db = open_db()
    rng = random.Random(1)
    db.batch_insert([rvec(rng) for _ in range(10000)],
                    [{"text": f"base-{i}", "phase": "base", "n": i} for i in range(10000)])
    report["base_count"] = db.node_count()
    db.close()

    # ---- A. 单实例 16 线程混合高压 30s ----
    t0 = time.time()
    stats = {"writes": 0, "reads": 0, "links": 0, "errs": []}
    lock = threading.Lock()
    db = open_db()

    def worker(role, wid):
        rng2 = random.Random(wid * 13 + 1)
        while time.time() - t0 < 30:
            try:
                if role == "writer":
                    db.insert_with_id(1_000_000 + wid * 100_000 + stats["writes"],
                                      rvec(rng2), {"text": f"mixw-{wid}", "phase": "mix"})
                    with lock:
                        stats["writes"] += 1
                elif role == "reader":
                    db.search(rvec(rng2), top_k=5, min_score=0.0)
                    with lock:
                        stats["reads"] += 1
                elif role == "linker":
                    a = rng2.randrange(1, 10000)
                    b = rng2.randrange(1, 10000)
                    if a != b:
                        db.link(a, b, label="RELATED_TO", weight=rng2.random())
                        with lock:
                            stats["links"] += 1
            except Exception as e:
                with lock:
                    stats["errs"].append(f"{role}{wid} {type(e).__name__}: {str(e)[:60]}")

    threads = []
    for w in range(8):
        threads.append(threading.Thread(target=worker, args=("writer", w)))
    for w in range(4):
        threads.append(threading.Thread(target=worker, args=("reader", w)))
    for w in range(4):
        threads.append(threading.Thread(target=worker, args=("linker", w)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    report["stages"]["A_mixed_16t_30s"] = {
        "secs": round(time.time() - t0, 3),
        "writes": stats["writes"], "reads": stats["reads"], "links": stats["links"],
        "err_count": len(stats["errs"]), "errs": stats["errs"][:20],
        "count_after": db.node_count()}
    db.close()

    # ---- B. 同 id 并发 upsert（1000 个 id，8 线程各 300 次） ----
    db = open_db()
    ids = list(range(500_000, 501_000))
    db.batch_insert_with_ids(ids, [rvec() for _ in range(1000)], [{"text": f"upsert-{i}"} for i in range(1000)])
    t0 = time.time()
    errs_b = []

    def upsert_worker(wid):
        rng3 = random.Random(wid * 7 + 2)
        try:
            for r in range(300):
                nid = ids[r % len(ids)]
                db.update_payload(nid, {"worker": wid, "round": r, "rand": rng3.random()})
        except Exception as e:
            errs_b.append(f"{type(e).__name__}: {str(e)[:80]}")

    threads = [threading.Thread(target=upsert_worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    report["stages"]["B_upsert_8x300"] = {
        "secs": round(time.time() - t0, 3), "err_count": len(errs_b), "errs": errs_b[:20],
        "count_after": db.node_count(), "count_expected": 101000 + report["base_count"]}
    # 抽查 50 个被 upsert 的 id 是否可读
    bad = 0
    for nid in ids[:50]:
        try:
            nv = db.get(nid)
            if nv is None:
                bad += 1
        except Exception:
            bad += 1
    report["stages"]["B_upsert_8x300"]["sample_bad"] = bad
    db.close()

    # ---- C. 大文本 payload 500 条 ----
    t0 = time.time()
    errs_c = []
    try:
        db = open_db()
        db.batch_insert([rvec() for _ in range(500)], [long_payload(i) for i in range(500)])
        db.close()
        report["stages"]["C_bigpayload_500"] = {"secs": round(time.time() - t0, 3), "errs": errs_c}
    except Exception as e:
        report["stages"]["C_bigpayload_500"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- D. 真实 id 段 8 万边 + expand_depth=3 ----
    t0 = time.time()
    errs_d = []
    try:
        db = open_db()
        rng = random.Random(4)
        pool = list(range(1, 10001))
        linked = 0
        for i in range(80000):
            a = pool[rng.randrange(len(pool))]
            b = pool[rng.randrange(len(pool))]
            if a == b:
                continue
            try:
                db.link(a, b, label="RELATED_TO", weight=rng.random())
                linked += 1
            except Exception as e:
                errs_d.append(f"{type(e).__name__}: {str(e)[:60]}")
        db.close()
        report["stages"]["D_graph_80k"] = {"linked": linked, "secs": round(time.time() - t0, 3), "errs": errs_d[:20]}
    except Exception as e:
        report["stages"]["D_graph_80k"] = {"fatal": f"{type(e).__name__}: {e}"}

    # expand_depth=3 检索压力
    t0 = time.time()
    errs_d2 = []
    lat = []
    try:
        db = open_db()
        rng = random.Random(5)
        for i in range(300):
            s = time.time()
            try:
                h = db.search(rvec(rng), top_k=10, min_score=0.0, expand_depth=3)
                lat.append((time.time() - s) * 1000)
            except Exception as e:
                errs_d2.append(f"{type(e).__name__}: {str(e)[:60]}")
        db.close()
        lat.sort()
        report["stages"]["D_expand3_300"] = {
            "secs": round(time.time() - t0, 3),
            "rate_per_sec": round(300 / (time.time() - t0), 2) if (time.time() - t0) else 0,
            "lat_ms": {"p50": round(lat[len(lat)//2], 2), "p95": round(lat[int(len(lat)*0.95)], 2), "max": round(lat[-1], 2)},
            "errs": errs_d2[:20]}
    except Exception as e:
        report["stages"]["D_expand3_300"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- E. 硬杀精确对比 ----
    db = open_db()
    count_before = db.node_count()
    db.close()
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--child-hardkill", "8"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4.0)
        proc.kill()
        proc.wait(timeout=10)
        db = open_db()
        count_after = db.node_count()
        report["stages"]["E_hardkill"] = {
            "count_before": count_before, "count_after_kill_reopen": count_after,
            "delta": count_after - count_before, "child_retcode": proc.returncode,
            "reopen_ok": True}
        db.close()
    except Exception as e:
        report["stages"]["E_hardkill"] = {"fatal": f"{type(e).__name__}: {e}"}

    # ---- F. compact + 全量核对 ----
    try:
        db = open_db()
        before = db.node_count()
        t0 = time.time()
        db.compact()
        after = db.node_count()
        # 全量 id 集合核对
        ids_all = db.all_node_ids()
        uniq = len(set(ids_all))
        # 随机抽查 300 个真实存在的 id
        rng = random.Random(7)
        sample = rng.sample(ids_all, min(300, len(ids_all)))
        bad = 0
        for nid in sample:
            try:
                nv = db.get(nid)
                if nv is None:
                    bad += 1
            except Exception:
                bad += 1
        # 搜索 sanity
        try:
            h = db.search(rvec(rng), top_k=5, min_score=0.0)
            search_ok = True
        except Exception as e:
            search_ok = f"ERR {type(e).__name__}: {e}"
        report["stages"]["F_compact_verify"] = {
            "before": before, "after": after, "consistent": before == after,
            "secs": round(time.time() - t0, 3),
            "all_ids": len(ids_all), "unique_ids": uniq, "dup_ids": len(ids_all) - uniq,
            "sample_bad": bad, "search_ok": search_ok,
            "est_memory_mb": round(db.estimated_memory() / (1024*1024), 2)}
        db.close()
    except Exception as e:
        report["stages"]["F_compact_verify"] = {"fatal": f"{type(e).__name__}: {e}"}

    try:
        report["db_size_mb"] = round(os.path.getsize(DB) / (1024*1024), 2)
    except Exception:
        pass

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print("=" * 60)
    print("TriviumDB 第二轮压测完成")
    print("=" * 60)
    for k, v in report.get("stages", {}).items():
        if isinstance(v, dict) and "fatal" in v:
            print(f"[FAIL] {k}: {v['fatal']}")
        elif isinstance(v, dict):
            extra = ""
            for key in ("rate_per_sec", "err_count", "delta", "sample_bad", "dup_ids"):
                if key in v:
                    extra += f"  {key}={v[key]}"
            print(f"[OK]   {k}  耗时={v.get('secs', '?')}s{extra}")
    print(f"最终节点数: {report['stages']['F_compact_verify'].get('after', '?')}  库大小: {report.get('db_size_mb', '?')} MB")
    print(f"报告: {REPORT}")


def child_hard_kill(target_secs):
    t0 = time.time()
    try:
        db = open_db()
        rng = random.Random(4242)
        i = 0
        while time.time() - t0 < target_secs:
            db.insert_with_id(9_000_000 + i, rvec(rng), {"text": f"kill-{i}", "phase": "hardkill"})
            i += 1
        db.close()
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child-hardkill":
        child_hard_kill(float(sys.argv[2]) if len(sys.argv) > 2 else 8)
    else:
        main()
