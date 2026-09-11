"""A/B 库准备：从同一份真库快照复制两份副本，只改其中一份的向量模型。

用于 embedding 模型或检索参数的 A/B 对照——单变量控制（同库 / 同题集 / 同机）：
  eval/.tmp/ab2/qwen3/   ← 真库原样（当前 embedding 向量）
  eval/.tmp/ab2/bgem3/   ← 全量重嵌入为 bge-m3 向量

不截断内容：bge-m3 在本机 Ollama 上有上下文硬限（>2770 字报 500），失败如实计数，
不用截断掩盖模型能力边界（截断会引入第二个变量）。

用法：venv/Scripts/python.exe scripts/ab_snapshot_build.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = ROOT / "eval" / ".tmp" / "ab2"
TMP.mkdir(parents=True, exist_ok=True)

_env_db = os.getenv("DB_PATH", "")
ORIG_DB = (Path(_env_db) if os.path.isabs(_env_db) else ROOT / _env_db) if _env_db \
    else ROOT / "data" / "mh_memory.db"
ORIG_FTS = ORIG_DB.parent / "fts.db"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


PRE = sha256(ORIG_DB)

# ── 1) 两份副本都从同一真库快照复制 ──
sidecars = [p for p in ORIG_DB.parent.iterdir()
            if p.name.startswith(ORIG_DB.name) and p.is_file()]
for name in ("qwen3", "bgem3"):
    d = TMP / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for p in sidecars:
        shutil.copy2(p, d / p.name)
    if ORIG_FTS.exists():
        shutil.copy2(ORIG_FTS, d / ORIG_FTS.name)
print(f"副本已建: {TMP}/{{qwen3,bgem3}}  (每份 {len(sidecars) + 1} 个文件)")

# ── 2) bgem3 副本全量重嵌入 ──
os.environ["EMBEDDING_PROVIDER"] = "ollama"
os.environ["OLLAMA_EMBEDDING_MODEL"] = "bge-m3"
os.environ["DB_PATH"] = str(TMP / "bgem3" / ORIG_DB.name)

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402

print(f"provider={Config.EMBEDDING_PROVIDER} model={Config.OLLAMA_EMBEDDING_MODEL}")
store = TriviumStore()
print(f"库维度 dim={store.dim}")

probe = store.embed_text("冒烟：记忆库 embedding 切换")
if len(probe) != store.dim:
    raise SystemExit(f"维度不匹配 bge-m3={len(probe)} vs 库={store.dim}")

# 物化后再写（iter_nodes 生成器占连接，循环内 update_vector 会 Database locked）
targets = [(nid, (node.get("payload") or {}).get("content") or "")
           for nid, node in store.iter_nodes()]
print(f"待重嵌节点 {len(targets)} 条（连接已释放）")

t0 = time.time()
ok = skip = fail = 0
fails: list[tuple[int, int, str]] = []
for i, (nid, content) in enumerate(targets, 1):
    if not content.strip():
        skip += 1
        continue
    try:
        vec = store.embed_text(content)
        if len(vec) != store.dim:
            raise ValueError(f"维度 {len(vec)} != {store.dim}")
        store.update_vector(nid, vec)
        ok += 1
    except Exception as e:  # noqa: BLE001
        fail += 1
        fails.append((nid, len(content), str(e)[:60]))
    if i % 200 == 0:
        print(f"  {i} 条 ok={ok} skip={skip} fail={fail} {time.time() - t0:.0f}s", flush=True)

print(f"重嵌入完成：ok={ok} skip={skip} fail={fail} 用时 {time.time() - t0:.0f}s")
if fails:
    print("失败样例（节点id, 内容长度, 错误）:")
    for row in fails[:8]:
        print("  ", row)

POST = sha256(ORIG_DB)
print(f"真库完整性: {'一致 ✅' if PRE == POST else '不一致 ❌'}")
(TMP / "build_report.json").write_text(json.dumps(
    {"ok": ok, "skip": skip, "fail": fail, "fails": fails[:50],
     "dim": store.dim, "db_unchanged": PRE == POST},
    ensure_ascii=False, indent=1), encoding="utf-8")
