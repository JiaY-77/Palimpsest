"""重嵌超长节点的向量（issue #232）。

背景：``embed()`` 内部有 ``tokens = tokens[:n_batch]``，超过
``EMBEDDING_LOCAL_N_BATCH``（默认 512）的输入被**静默截断**——生产库
732/2912 个节点的向量只覆盖各自 content 的前 512 token（novel_chunk
119/124、task 148/192、record 212/748）。修复端在
``core.trivium_store._embed_local_chunked``（分块池化：按 token 边界切块、
逐块 LAST pooling、按块 token 数加权平均后再归一）；本脚本负责**存量**，
把这些节点重算一遍并写入嵌入审计（payload["embedding"].embedding_mode
= "chunked"）。

选择标准与幂等：
  - 只挑 ``content`` 现算 token 数 > ``EMBEDDING_LOCAL_CHUNK_TOKENS`` 的
    节点——用与 ``embed()`` 同一把分词器现算，**不信 payload 里的旧字段**
    （那正是被截断时没人记录的证据缺口）；
  - 已带 ``payload["embedding"].embedding_mode == "chunked"`` 的节点跳过，
    重跑应报 0 变更（幂等）。

**默认 dry-run**：只读打开（``TriviumStore(read_only=True)``），打印将重嵌
的节点（id / type / tokens），不写库；``--apply`` 才落盘。

⚠ 本脚本**不自动备份**——``--apply`` 之前先跑::

    python scripts/backup_db.py --keep 7

⚠ 仅适用于 ``EMBEDDING_PROVIDER=local``：token 计数与重嵌都走进程内
分词器 / 分块池化实现；ollama / openai 的截断策略由各自服务决定，本脚本
不改、也不迁移（issue #232 范围内检索层零改动）。

⚠ ``--apply`` 以读写模式独占打开库——生产库在 REST 服务运行期间无法写
（triviumdb 连接级排他），须在停服务窗口执行。**绝不顺手跑**：重嵌生产库
由维护者按计划窗口执行。

用法（在仓库根目录）::

    python scripts/reembed_long_nodes.py            # 预览（只读）
    python scripts/reembed_long_nodes.py --apply    # 执行（先备份、停服务）
    python scripts/reembed_long_nodes.py --json     # 机器可读输出
"""

import argparse
import json
import os
import sys
from collections.abc import Callable
from typing import Any

# 允许直接以 `python scripts/reembed_long_nodes.py` 运行（把仓库根加进 sys.path）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config  # noqa: E402


def default_token_count(store) -> Callable[[str], int]:
    """返回 token 计数函数：与 ``embed()`` 同一把分词器、同一口径。

    llama-cpp-python 0.3.36 的 ``embed()`` 内部就是
    ``tokenize(text.encode("utf-8"))``（同默认参数）后按 ``n_batch`` 截断，
    所以「> chunk_tokens 才需要重嵌」的判据必须用同一口径现算。
    """
    from core import trivium_store as ts

    store._ensure_local_llm()

    def count(text: str) -> int:
        with ts._LOCAL_LLM_LOCK:
            return len(ts._LOCAL_LLM.tokenize(text.encode("utf-8")))

    return count


def reembed_long_nodes(store, token_count: Callable[[str], int], dry_run: bool = True) -> dict:
    """扫描并（可选）重嵌超长节点。返回统计报告 dict。

    token_count：注入的 token 计数函数（生产 = 分词器现算；测试 = mock）。
    幂等：已带 chunked 审计的节点跳过，重跑 total_changed == 0。
    """
    limit = int(Config.EMBEDDING_LOCAL_CHUNK_TOKENS)

    # 先把遍历结果物化再写：iter_payloads 是单连接生成器，边遍历边写会
    # 在同一连接内制造读/写窗口重叠（锁库）——issue #167 教训
    scanned = 0
    already = 0  # 已带 chunked 审计（跳过）
    empty_content = 0
    candidates: list[dict[str, Any]] = []
    by_type: dict[str, int] = {}

    for nid, payload in store.iter_payloads():
        scanned += 1
        if not isinstance(payload, dict):
            continue
        if (payload.get("embedding") or {}).get("embedding_mode") == "chunked":
            already += 1
            continue
        content = payload.get("content") or ""
        if not content:
            empty_content += 1
            continue
        n_tokens = token_count(content)
        if n_tokens <= limit:
            continue
        ntype = payload.get("type") or "unknown"
        candidates.append({"id": nid, "type": ntype, "tokens": n_tokens, "chars": len(content)})
        by_type[ntype] = by_type.get(ntype, 0) + 1

    applied = 0
    failed: list[dict[str, Any]] = []
    if not dry_run:
        for cand in candidates:
            nid = cand["id"]
            try:
                # reembed_node：content → embed_text_with_audit（分块池化）
                # → update_vector + payload["embedding"] 审计；单节点原子
                # 成功、失败逐条收集不中断整批（同 backfill 范本）
                if store.reembed_node(nid):
                    applied += 1
                else:
                    failed.append({"id": nid, "error": "节点不存在（reembed_node 返回 False）"})
            except Exception as e:  # noqa: BLE001 —— 单条失败不毁整批
                failed.append({"id": nid, "error": str(e)[:200]})

    return {
        "dry_run": dry_run,
        "chunk_tokens": limit,
        "provider": getattr(Config, "EMBEDDING_PROVIDER", "local") or "local",
        "scanned": scanned,
        "already_chunked": already,
        "empty_content": empty_content,
        "candidates_total": len(candidates),
        "candidates_by_type": by_type,
        "candidates": candidates,
        "applied": applied,
        "failed": failed,
        "total_changed": applied,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重嵌超长节点的向量（默认 dry-run 预览，issue #232）")
    parser.add_argument(
        "--apply", action="store_true", help="真正写库（默认只预览不落盘；跑之前先 scripts/backup_db.py）"
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（默认人类可读）")
    args = parser.parse_args(argv)

    # 惰性 import：避免仅 --help 就初始化库
    from core.trivium_store import TriviumStore

    provider = getattr(Config, "EMBEDDING_PROVIDER", "local") or "local"
    if provider != "local":
        print(
            f"仅支持 EMBEDDING_PROVIDER=local（当前 {provider}）：分块池化与 token 计数都走进程内实现，",
            file=sys.stderr,
        )
        print("ollama / openai 的截断策略由各自服务决定，不在本脚本范围。", file=sys.stderr)
        return 2

    store = TriviumStore(read_only=not args.apply)  # dry-run 只读，不与 REST 争写
    result = reembed_long_nodes(store, default_token_count(store), dry_run=not args.apply)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    mode = "dry-run（未写库）" if result["dry_run"] else "已写库"
    print(f"超长节点重嵌（issue #232） —— {mode}")
    print("=" * 56)
    print(f"chunk_tokens={result['chunk_tokens']} | provider={result['provider']}")
    print(
        f"扫描节点: {result['scanned']} | 已带 chunked 审计(跳过): {result['already_chunked']}"
        f" | 空 content: {result['empty_content']}"
    )
    print(f"待重嵌: {result['candidates_total']} 个，按类型: {result['candidates_by_type']}")
    print("-" * 56)
    print("将重嵌的节点（id / type / tokens）：")
    for cand in result["candidates"]:
        print(f"  id={cand['id']:>6}  type={cand['type']:<12} tokens={cand['tokens']:>6}  chars={cand['chars']}")
    if result["failed"]:
        print(f"失败 {len(result['failed'])} 条（重跑幂等续接）：")
        for f_entry in result["failed"]:
            print(f"  id={f_entry['id']}: {f_entry['error'][:120]}")
    if result["dry_run"]:
        print("-" * 56)
        print("dry-run 未写库；确认无误后：")
        print("  1) python scripts/backup_db.py --keep 7   # 先备份（本脚本不自动备份）")
        print("  2) 停 REST 服务窗口内 python scripts/reembed_long_nodes.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
