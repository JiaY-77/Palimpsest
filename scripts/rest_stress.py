#!/usr/bin/env python3
"""Palimpsest REST 应用层压测。

覆盖 6 个场景：高频检索 / 写入 / 图谱扩散 / 边界输入 / 读写混合 / 正确性抽查
（模拟真实使用模式，而不是单纯打 GET）。

用法:
  # 建议先起一个**测试实例**（独立库 + 独立端口），不要对着生产库跑
  DB_PATH=/path/to/test.db python -m uvicorn main:app --port 8091
  python scripts/rest_stress.py --base http://127.0.0.1:8091 --seeds 200 --out report.json
  python scripts/rest_stress.py --base http://127.0.0.1:8091 --quick     # 快速档

注意:
  - 脚本会向目标实例写入种子记忆（--seeds 条）与混合压测数据，**只对测试实例运行**；
  - 依赖 `requests`（见 requirements.txt）；
  - 固定随机种子（42），同样入参可复现；
  - 输出为 JSON 报告：每个场景的 qps / p50 / p95 / p99 / 错误率。
"""
import argparse
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

random.seed(42)

TEMPLATES = [
    ("memory", "用户今天在 {place} 讨论了 {topic}，重点是 {detail}，后续要跟进 {follow}"),
    ("record", "运维记录 {ts}：执行了 {action}，结果为 {result}，注意 {note}"),
    ("plan", "方案 {name}：目标是 {goal}，分三步走：{s1}、{s2}、{s3}，先做{s0}"),
    ("correction", "纠正：之前关于 {topic} 的理解不对，正确的是 {correct}，重要度调高"),
    ("event", "事件 {ts}：{who} 完成了 {what}，影响 {impact}"),
]
PLACES = ["办公室", "会议室A", "线上会议", "家里", "出差路上"]
TOPICS = ["记忆插件架构", "存储引擎升级", "周报模板", "模型路由", "知识库双链", "压测报告", "开源合规"]
DETAILS = ["接口要向后兼容", "需要先写对比测试", "注意脱敏纪律", "优先本地模型", "以官方文档为准"]
FOLLOWS = ["写验收报告", "同步知识库", "发周报给团队", "更新文档沉淀", "做回归测试"]
ACTIONS = ["升级依赖", "迁移数据库", "清理缓存", "重启服务", "跑全量测试"]
RESULTS = ["成功", "部分成功", "需要复查", "失败后回滚", "待验证"]
NOTES = ["记录到知识库", "上游未发版需本地验证", "失败路径要覆盖", "验收须亲自跑"]
NAMES = ["记忆插件 0.6→1.0", "存储引擎 0.8.5 同步", "REST 压测", "配置迁移", "模型盘点"]
GOALS = ["开源可复用", "低延迟检索", "零数据丢失", "成本可控"]
S1S = ["需求评审", "代码审查", "压测验证", "文档完善"]
WHOS = ["助理", "CI 机器人", "评审员", "值班同事"]
IMPACTS = ["性能提升", "稳定性风险", "需人工复核", "无影响"]

def gen_content(i: int, tag: str = "") -> tuple:
    t, tmpl = random.choice(TEMPLATES)
    ts = f"2026-09-{random.randint(1, 5):02d}"
    ctx = dict(place=random.choice(PLACES), topic=random.choice(TOPICS),
               detail=random.choice(DETAILS), follow=random.choice(FOLLOWS),
               ts=ts, action=random.choice(ACTIONS), result=random.choice(RESULTS),
               note=random.choice(NOTES), name=random.choice(NAMES), goal=random.choice(GOALS),
               s1=random.choice(S1S), s2=random.choice(S1S), s3=random.choice(S1S),
               s0=random.choice(S1S), who=random.choice(WHOS), what=random.choice(ACTIONS),
               impact=random.choice(IMPACTS), correct=random.choice(DETAILS))
    content = tmpl.format(**ctx)
    # 每条带唯一标记，便于召回验证
    marker = f"#{tag or 'seed'}{i:05d}#"
    return marker + " " + content, t

def post(base: str, path: str, payload=None, raw: str = None,
         timeout: float = 30) -> tuple:
    """返回 (ok, status, body_str, elapsed)"""
    t0 = time.perf_counter()
    try:
        if raw is not None:
            r = requests.post(base + path, data=raw,
                              headers={"Content-Type": "application/json"},
                              timeout=timeout)
        else:
            r = requests.post(base + path, json=payload, timeout=timeout)
        el = time.perf_counter() - t0
        return r.ok, r.status_code, r.text[:300], el
    except Exception as e:
        el = time.perf_counter() - t0
        return False, -1, f"EXC {type(e).__name__}: {e}", el

def get(base: str, path: str, timeout: float = 30) -> tuple:
    t0 = time.perf_counter()
    try:
        r = requests.get(base + path, timeout=timeout)
        return r.ok, r.status_code, r.text[:300], time.perf_counter() - t0
    except Exception as e:
        return False, -1, f"EXC {type(e).__name__}: {e}", time.perf_counter() - t0

def run_bench(name: str, fn, jobs: list, workers: int,
              max_seconds: float = 0) -> dict:
    """并发执行 jobs；max_seconds>0 时限流持续打直到时间到（用于混合长压）。"""
    times, errors = [], []
    t_start = time.perf_counter()
    n_done = 0
    if max_seconds > 0:
        # 限时模式：workers 个线程自循环打点直到时间到（真并发混合长压）
        stop_at = time.perf_counter() + max_seconds
        state = {"i": 0}
        st_lock = threading.Lock()

        def worker_loop(_idx):
            n = 0
            while time.perf_counter() < stop_at:
                with st_lock:
                    state["i"] += 1
                    j = state["i"]
                ok, status, body, el = fn(j)
                times.append(el)
                if not ok:
                    errors.append({"job": j, "status": status, "body": body[:150]})
                n += 1
            return n

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(worker_loop, i) for i in range(workers)]
            for f in as_completed(futs):
                n_done += f.result()
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fn, j): j for j in jobs}
            for f in as_completed(futs):
                ok, status, body, el = f.result()
                n_done += 1
                times.append(el)
                if not ok:
                    errors.append({"job": futs[f], "status": status, "body": body[:150]})
    elapsed = time.perf_counter() - t_start
    return summarize(name, times, errors, elapsed, n_done)

def summarize(name: str, times: list, errors: list, elapsed: float, n_done: int):
    if not times:
        return {"name": name, "n": 0, "error_count": len(errors), "qps": 0.0,
                "note": "no samples"}
    times_sorted = sorted(times)
    def pct(p):
        idx = min(len(times_sorted) - 1, int(p / 100 * len(times_sorted)))
        return round(times_sorted[idx] * 1000, 2)  # ms
    return {
        "name": name,
        "n": n_done,
        "error_count": len(errors),
        "error_pct": round(100.0 * len(errors) / n_done, 2),
        "qps": round(n_done / elapsed, 2),
        "min_ms": round(times_sorted[0] * 1000, 2),
        "p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
        "max_ms": round(times_sorted[-1] * 1000, 2),
        "errors_sample": errors[:5],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8091",
                    help="目标实例地址（建议指向测试实例，脚本会写入数据）")
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--out", default="")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    base = args.base
    report = {"base": base, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "scenarios": []}

    # ---------- 0. 健康检查 ----------
    ok, status, body, el = get(base, "/")
    print(f"[0] GET / -> {status} {body[:120]}")
    if not ok:
        print("FATAL: service not ready"); return

    # ---------- 1. 预热：写入种子记忆 ----------
    print(f"[1] seeding {args.seeds} memories ...")
    seeds = [gen_content(i) for i in range(args.seeds)]
    seed_ids = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {}
        for i, (content, typ) in enumerate(seeds):
            futs[ex.submit(post, base, "/mem/ingest",
                           {"content": content, "type": typ,
                            "importance": round(random.uniform(0.3, 0.9), 2),
                            "domain": random.choice(["hermes", "kb", "work", "general"]),
                            "source": "stress-seed"})] = i
        for f in as_completed(list(futs)):
            okf, status, body, el = f.result()
            if okf:
                try:
                    nid = json.loads(body).get("node_id")
                    if nid is not None:
                        seed_ids.append(nid)
                except Exception:
                    pass
    print(f"    seeded {len(seed_ids)}/{args.seeds} in {time.perf_counter()-t0:.1f}s")

    # ---------- 2. 场景 S1 高频检索 ----------
    queries = []
    for i in range(min(args.seeds, 60)):
        c, _ = seeds[i]
        words = [w for w in c.replace("#", " ").split() if len(w) > 1]
        queries.append(random.choice(words) if words else "记忆插件")
    queries = (queries * 3)[: args.seeds]
    s1 = run_bench("S1 mem_search 高频检索", 
                   lambda j: post(base, "/mem/search", {"query": queries[j % len(queries)], "scope": "memory", "top_k": 5}),
                   list(range(min(args.seeds, 100))), workers=10)
    report["scenarios"].append(s1); print(f"[S1] {s1}")

    # ---------- 3. 场景 S2 写入压测 ----------
    if args.quick:
        n_write = 30
    else:
        n_write = 120
    write_jobs = []
    for i in range(n_write):
        c, typ = gen_content(i + 10000, tag="wr")
        write_jobs.append({"content": c, "type": typ, "importance": 0.6,
                           "domain": "hermes", "source": "stress-write"})
    s2 = run_bench("S2 mem_ingest 写入压测",
                   lambda j: post(base, "/mem/ingest", write_jobs[j]),
                   list(range(len(write_jobs))), workers=6)
    report["scenarios"].append(s2); print(f"[S2] {s2}")

    # ---------- 4. 场景 S3 图谱扩散 ----------
    # 先建一批边（link 前 40 个种子节点成链）
    if len(seed_ids) >= 10:
        link_jobs = []
        for i in range(min(len(seed_ids) - 1, 40)):
            link_jobs.append({"source_id": seed_ids[i], "target_id": seed_ids[i + 1], "relation": "RELATED_TO"})
        s3a = run_bench("S3a mem_link 建边",
                        lambda j: post(base, "/mem/link", link_jobs[j]),
                        list(range(len(link_jobs))), workers=4)
        report["scenarios"].append(s3a); print(f"[S3a] {s3a}")
        # 邻居扩散（并发查不同节点）
        nb_jobs = [{"node_id": seed_ids[i % len(seed_ids)], "depth": 1} for i in range(60)]
        s3b = run_bench("S3b graph/neighbors 邻居扩散",
                        lambda j: post(base, "/graph/neighbors", nb_jobs[j]),
                        list(range(len(nb_jobs))), workers=8)
        report["scenarios"].append(s3b); print(f"[S3b] {s3b}")
        # 社区发现
        s3c = run_bench("S3c graph/communities",
                        lambda j: post(base, "/graph/communities", {"min_community_size": 2, "top_k": 5}),
                        list(range(10)), workers=2)
        report["scenarios"].append(s3c); print(f"[S3c] {s3c}")

    # ---------- 5. 场景 S4 边界输入 ----------
    boundary = []
    cases = {
        "empty_content": {"content": "", "type": "memory"},
        "null_payload": None,
        "bad_json_raw": "{not-json",
        "huge_content": {"content": "X" * 200_000, "type": "memory"},
        "bad_type": {"content": "abc", "type": 123},
        "neg_topk": {"query": "x", "top_k": -5},
        "none_content": {"content": None, "type": "memory"},
        "missing_query": {"scope": "memory"},
        "get_missing_id": None,
    }
    for name, payload in cases.items():
        if name == "bad_json_raw":
            okf, status, body, el = post(base, "/mem/ingest", raw="not-json{")
        elif name == "get_missing_id":
            okf, status, body, el = get(base, "/memory/99999999")
        else:
            okf, status, body, el = post(base, "/mem/ingest", payload)
        boundary.append({"case": name, "status": status, "ok": okf, "body": body[:120], "ms": round(el * 1000, 1)})
        print(f"    [S4] {name}: status={status} ok={okf} body={body[:80]}")
    report["boundary"] = boundary

    # ---------- 6. 场景 S5 混合并发长压（限时） ----------
    n_workers = 8
    counter = {"i": 0}
    lock = threading.Lock()
    def mixed(j):
        with lock:
            counter["i"] += 1
            k = counter["i"]
        if k % 3 == 0:  # 33% 写入
            c, typ = gen_content(20000 + k, tag="mix")
            return post(base, "/mem/ingest", {"content": c, "type": typ, "importance": 0.5})
        else:
            q = queries[k % len(queries)]
            return post(base, "/mem/hybrid-search", {"query": q, "top_k": 5, "mode": "rrf"})
    s5 = run_bench("S5 读写混合 20s", mixed, [], workers=n_workers, max_seconds=20)
    report["scenarios"].append(s5); print(f"[S5] {s5}")

    # ---------- 7. 场景 S6 正确性抽查 ----------
    correctness = []
    marker = f"#verify{int(time.time())}#"
    unique_word = marker + "量子记忆封装协议验证"
    okf, status, body, el = post(base, "/mem/ingest",
                                 {"content": unique_word, "type": "memory", "importance": 0.9})
    correctness.append({"case": "ingest_unique", "status": status})
    time.sleep(0.5)
    okf, status, body, el = post(base, "/mem/search",
                                 {"query": "量子记忆封装协议", "scope": "memory", "top_k": 3})
    hit = marker in body
    correctness.append({"case": "search_recall_marker", "hit": hit, "status": status,
                        "body": body[:150]})
    # 冲突检测：立即重写几乎相同的内容，应被标记 outdated 或提示（不崩即可）
    okf2, status2, body2, el2 = post(base, "/mem/ingest",
                                     {"content": unique_word, "type": "memory", "importance": 0.9})
    correctness.append({"case": "duplicate_ingest", "status": status2, "body": body2[:150]})
    report["correctness"] = correctness
    for c in correctness:
        print(f"    [S6] {c}")

    # ---------- 汇总 ----------
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["seed_count"] = len(seed_ids)
    print("\n==== SUMMARY ====")
    for s in report["scenarios"]:
        print(f"{s['name']}: n={s['n']} qps={s['qps']} err={s['error_count']}({s['error_pct']}%) "
              f"p50={s['p50_ms']}ms p95={s['p95_ms']}ms p99={s['p99_ms']}ms max={s['max_ms']}ms")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("report ->", args.out)


if __name__ == "__main__":
    main()
