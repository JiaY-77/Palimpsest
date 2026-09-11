"""
检索体检探针 —— 固化已验证探针查询，输出 top-1 命中率 + 延迟。

用途：判断「该不该换 embedding 模型 / 换维度」的客观基线。
换模型后回跑同一条命令对比即可。

用法：
  python scripts/retrieval_probe.py                      # 跑内置探针集，人类可读输出
  python scripts/retrieval_probe.py --json               # 机器可读 JSON
  python scripts/retrieval_probe.py --top-k 5 --repeat 3 # 自定义 top-k 与延迟采样次数
  python scripts/retrieval_probe.py --probe-file my.json # 自定义探针集
  python scripts/retrieval_probe.py --no-warmup          # 计入冷启动（默认先预热一次）
"""
import argparse
import json
import os
import sys
import time

try:
    try:
        from _common import PROJECT_ROOT as _PROJECT_ROOT
    except ImportError:
        from scripts._common import PROJECT_ROOT as _PROJECT_ROOT

    from config import Config
    from mcp_tools import mem_search
except ImportError as _import_err:
    _hint = (
        "\n"
        "未检测到依赖 / Missing dependencies\n"
        "请先安装项目依赖再运行，参考 README：\n"
        "    python -m venv venv\n"
        "    venv\\Scripts\\activate              # Windows\n"
        "    source venv/bin/activate            # macOS / Linux\n"
        "    pip install -r requirements.txt\n"
        f"详情: {_import_err}\n"
    )
    print(_hint, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# 内置探针集（已在本机真实库验证过，勿改查询文本）
# ---------------------------------------------------------------------------
DEFAULT_PROBES: list[dict] = [
    {
        "name": "payload-regression-086",
        "query": "0.8.6 payload 回退为何慢",
        "scope": "all",
        "expect_source_contains": "0.8.6",
    },
    {
        "name": "novel-character",
        "query": "凌无咎与谁的关系",
        "scope": "all",
        "expect_type": "novel_chunk",
        "expect_text_contains": "凌无咎",
    },
    {
        "name": "rest-autostart",
        "query": "记忆服务的 REST 接口怎么自启动",
        "scope": "all",
        "expect_source_contains": "融合",
    },
    {
        "name": "memory-recall",
        "query": "这台电脑的 CPU 型号是什么",
        "scope": "memory",
        "expect_type_any": ["memory", "correction"],
    },
]


# ---------------------------------------------------------------------------
# 判定逻辑（纯函数，便于单测）
# ---------------------------------------------------------------------------
def evaluate_probe(probe: dict, top1: dict | None) -> bool:
    """判断 top-1 结果是否满足探针的全部 expect 条件。

    top1: mem_search results[0] 结构（含 id/type/score/summary/meta）
    返回 True = 命中。
    """
    if top1 is None:
        return False
    meta = top1.get("meta", {}) or {}
    # expect_source_contains
    src = probe.get("expect_source_contains")
    if src and src not in meta.get("source_path", ""):
        return False
    # expect_type
    exp_type = probe.get("expect_type")
    if exp_type and top1.get("type", "") != exp_type:
        return False
    # expect_type_any
    exp_type_any = probe.get("expect_type_any")
    if exp_type_any and top1.get("type", "") not in exp_type_any:
        return False
    # expect_text_contains（在 summary / meta.title / meta.source_path 中搜索）
    txt = probe.get("expect_text_contains")
    if txt:
        searchable = " ".join([
            top1.get("summary", ""),
            meta.get("title", ""),
            meta.get("source_path", ""),
        ])
        if txt not in searchable:
            return False
    return True


# ---------------------------------------------------------------------------
# 单次探针执行
# ---------------------------------------------------------------------------
def run_one_probe(probe: dict, top_k: int) -> dict:
    """执行单条探针，返回 {name, query, hit, top1, latency_ms}。"""
    t0 = time.perf_counter()
    raw = mem_search(
        query=probe["query"],
        scope=probe.get("scope", "all"),
        top_k=top_k,
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    data = json.loads(raw)
    results = data.get("results", [])
    top1 = results[0] if results else None
    hit = evaluate_probe(probe, top1)
    return {
        "name": probe["name"],
        "query": probe["query"],
        "hit": hit,
        "top1": top1,
        "latency_ms": round(latency_ms, 1),
    }


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------
def _fmt_result(r: dict, idx: int) -> str:
    lines = []
    tag = "✅" if r["hit"] else "❌"
    lines.append(f"[{idx+1}] {r['name']}  {tag}")
    lines.append(f"    query: {r['query']}")
    if r["top1"]:
        t = r["top1"]
        m = t.get("meta", {}) or {}
        lines.append(
            f"    top-1: id={t.get('id')}  type={t.get('type')}  "
            f"score={t.get('score')}  latency={r['latency_ms']}ms"
        )
        if m.get("title"):
            lines.append(f"            title={m['title']}")
        if m.get("source_path"):
            lines.append(f"            source={m['source_path']}")
    else:
        lines.append(f"    top-1: (空)  latency={r['latency_ms']}ms")
    return "\n".join(lines)


def print_human(results: list[dict]) -> None:
    hits = sum(1 for r in results if r["hit"])
    total = len(results)
    latencies = [r["latency_ms"] for r in results]
    for i, r in enumerate(results):
        print(_fmt_result(r, i))
        print()
    print("─── 汇总 ───")
    print(f"top-1 命中率 {hits}/{total}")
    if latencies:
        print(
            f"延迟 min={min(latencies):.1f}ms  "
            f"avg={sum(latencies)/len(latencies):.1f}ms  "
            f"max={max(latencies):.1f}ms"
        )
    print(
        "基线：换 embedding 模型/维度后回跑同一条命令，对比命中率与延迟变化"
    )
    print("（默认已预热一次，模型冷启动不计入；需含冷启动的数字请加 --no-warmup）")


# ---------------------------------------------------------------------------
# JSON 输出
# ---------------------------------------------------------------------------
def build_json(results: list[dict]) -> dict:
    from core.trivium_store import TriviumStore
    _store = TriviumStore()
    provider = getattr(_store, "provider", Config.EMBEDDING_PROVIDER)
    dim = getattr(_store, "dim", Config.OLLAMA_EMBEDDING_DIM)
    latencies = [r["latency_ms"] for r in results]
    hits = sum(1 for r in results if r["hit"])
    probes_out = []
    for r in results:
        entry = {
            "name": r["name"],
            "query": r["query"],
            "hit": r["hit"],
            "top1": r["top1"],
            "latency_ms": r["latency_ms"],
        }
        probes_out.append(entry)
    summary = {
        "hit_rate": f"{hits}/{len(results)}",
        "total": len(results),
        "hits": hits,
        "latency_min_ms": round(min(latencies), 1) if latencies else 0,
        "latency_avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "latency_max_ms": round(max(latencies), 1) if latencies else 0,
        "provider": provider,
        "dim": dim,
    }
    return {"probes": probes_out, "summary": summary}


# ---------------------------------------------------------------------------
# 探针文件加载
# ---------------------------------------------------------------------------
def load_probe_file(path: str) -> list[dict]:
    """加载自定义探针 JSON 文件，失败退出码 2。"""
    if not os.path.isfile(path):
        print(f"错误：探针文件不存在: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"错误：探针文件 JSON 非法: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, list):
        print("错误：探针文件顶层必须是数组", file=sys.stderr)
        sys.exit(2)
    for i, p in enumerate(data):
        if not isinstance(p, dict) or "name" not in p or "query" not in p:
            print(f"错误：第 {i+1} 条探针缺少必要字段 (name/query)", file=sys.stderr)
            sys.exit(2)
    return data


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="retrieval_probe",
        description="检索体检探针：输出 top-1 命中率 + 延迟",
    )
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="输出机器可读 JSON")
    parser.add_argument("--top-k", type=int, default=1,
                        help="检索 top-k（默认 1）")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每条探针重复次数，取延迟中位数（默认 1）")
    parser.add_argument("--probe-file", default="",
                        help="自定义探针集 JSON 文件路径")
    parser.add_argument("--no-warmup", action="store_true",
                        help="跳过预热（默认先跑一次丢弃，避免模型冷启动污染延迟基线）")
    args = parser.parse_args()

    if args.probe_file:
        probes = load_probe_file(args.probe_file)
    else:
        probes = DEFAULT_PROBES

    # 预热一次并丢弃：embedding 模型冷启动（首次加载可达数秒）不计入延迟基线，
    # 否则「换模型前后对比」第一条探针的延迟会不可比。
    if probes and not args.no_warmup:
        try:
            run_one_probe(probes[0], args.top_k)
        except Exception:  # noqa: BLE001 —— 预热失败不影响正式测量（后续会报错）
            pass

    all_results = []
    for probe in probes:
        runs = []
        for _ in range(max(1, args.repeat)):
            runs.append(run_one_probe(probe, args.top_k))
        # 取延迟中位数对应的那次结果作为代表
        runs.sort(key=lambda r: r["latency_ms"])
        representative = runs[len(runs) // 2]
        representative["latency_ms"] = runs[len(runs) // 2]["latency_ms"]
        all_results.append(representative)

    if args.json_output:
        print(json.dumps(build_json(all_results), ensure_ascii=False, indent=2))
    else:
        print_human(all_results)

    hits = sum(1 for r in all_results if r["hit"])
    total = len(all_results)
    sys.exit(0 if hits == total else 1)


if __name__ == "__main__":
    main()
