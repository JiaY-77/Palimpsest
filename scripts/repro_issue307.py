"""issue #307 复现脚本：local embedding + local_ce rerank 交替使用多轮段错误。

不经过服务，直接调 ``mcp_tools.memory.mem_hybrid_search``。

用法（venv/Scripts/python.exe，在仓库根运行）::

    python scripts/repro_issue307.py [ROUNDS]     # 默认 10 轮

环境变量（对照实验用，不设则全走 Config 默认值）：
    REPRO_DB          复现库路径（默认 scratch 目录下 repro307.db；文件不存在时自动
                      造 30 条 domain=repro307 的测试节点——造数本身也走 store 的
                      本地 embedding 实例）
    REPRO_MODELS_DIR  GGUF 模型目录（默认主仓库 models/，worktree 里没有 635MB 模型）
    RERANK_LOCAL_N_BATCH / RERANK_LOCAL_N_UBATCH   原样透传给 Config（A/B/C 对照组）

崩溃证据：进程段错误时本脚本没有机会打印——exit code（Windows access violation
= 3221225477 / 139）+ stderr 尾部由调用方（shell）留存。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_MODELS = os.environ.get("REPRO_MODELS_DIR", "D:/HeJiaQi/Documents/Code/Python/Palimpsest/models")

# ---- 必须在 import config 之前设好（Config 在类定义时读环境变量）----
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["EMBEDDING_MODEL_PATH"] = str(Path(_MODELS) / "Qwen3-Embedding-0.6B-Q8_0.gguf")
os.environ["RERANK_ENABLED"] = "1"
os.environ["RERANK_BACKEND"] = "local_ce"
os.environ["RERANK_POOL"] = "20"
os.environ["RERANK_LOCAL_MODEL_PATH"] = str(Path(_MODELS) / "bge-reranker-v2-m3-Q8_0.gguf")
os.environ.setdefault("DB_PATH", str(REPO / "data" / f"repro307_{os.getpid()}.db"))
# RERANK_LOCAL_N_BATCH / RERANK_LOCAL_N_UBATCH：父进程设了就透传，没设走 Config 默认

sys.path.insert(0, str(REPO))

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
QUERY = "记忆分层冲突时的裁决规则"

db_path = Path(os.environ["DB_PATH"])
if os.environ.get("REPRO_REBUILD") == "1" and db_path.exists():
    db_path.unlink()
    print(f"[setup] REPRO_REBUILD=1：删除旧复现库 {db_path}", flush=True)

# 必须在 import mcp_tools（store 随 import 开库、文件落盘）**之前**记录库是否已存在
db_existed_before_import = db_path.exists()
from mcp_tools.memory import mem_hybrid_search, mem_ingest  # noqa: E402

if not db_existed_before_import:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[setup] 复现库不存在，造 30 条测试节点：{db_path}", flush=True)
    for i in range(1, 31):
        content = (
            f"repro307 语料 {i}：检索质量评估的不同侧面。"
            f"第 {i} 条讲的是{'向量召回与重排' if i % 2 else '关键词倒排与融合'}的实测记录，"
            f"关键参数取值 {i * 7}，结论随池深变化。"
        )
        out = mem_ingest(content, type="memory", importance=0.5, domain="repro307")
        if '"ok": false' in out.replace(" ", ""):
            print(f"[setup] ingest 第 {i} 条失败：{out}", flush=True)
            sys.exit(2)
    print("[setup] 30 条测试节点就绪", flush=True)
else:
    print(f"[setup] 复现库已存在，直接复用：{db_path}", flush=True)

t0 = time.time()
import core.reranker as rr  # noqa: E402

for r in range(1, ROUNDS + 1):
    result = mem_hybrid_search(QUERY, domain="repro307", top_k=5)
    import json

    data = json.loads(result)
    channels = (data.get("channels") or {}) if isinstance(data, dict) else {}
    if channels.get("rerank") != "ok":
        print(
            f"[fatal] 第 {r} 轮 rerank 未生效（channels={channels}）——复现条件不成立，退出",
            flush=True,
        )
        sys.exit(3)
    if rr._CE_MODEL is None:
        print("[fatal] cross-encoder 模型从未加载——rerank 走的是「整池都要」提前返回，复现条件不成立", flush=True)
        sys.exit(3)
    print(
        f"round {r} ok (rerank=ok, candidates={len(data.get('results') or [])}, {time.time() - t0:.1f}s)",
        flush=True,
    )

print(f"DONE: {ROUNDS} 轮全部完成，未复现崩溃（总耗时 {time.time() - t0:.1f}s）", flush=True)
