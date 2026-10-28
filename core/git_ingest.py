"""git 提交入库（业务级批操作，issue #168）
==========================================

把 ``ingest-git`` 的核心逻辑从 scripts/palimpsest_cli.py 抽到这里：CLI（经 REST）
与服务端端点共用同一实现，不在客户端开库。

契约：
  - 幂等：库中已有相同 ``commit_hash`` 的节点则跳过；
  - 单条端点单次请求完成「去重 + 插入」，事务边界留在服务端；
  - ``git log`` 由服务端执行（repo 路径必须对服务进程可读）。
"""

from __future__ import annotations

import subprocess

from core.trivium_store import TriviumStore


def collect_commits(repo: str, since: int) -> list[dict[str, str]]:
    """跑 ``git log`` 并解析为 ``{commit_hash, commit_time, subject}`` 列表。"""
    fmt = "%H|%ad|%s"
    cmd = [
        "git",
        "-C",
        repo,
        "log",
        f"--since={since} days ago",
        f"--pretty=format:{fmt}",
        "--date=short",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git log 失败：{result.stderr.strip()}")

    commits = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append(
                {
                    "commit_hash": parts[0],
                    "commit_time": parts[1],
                    "subject": parts[2],
                }
            )
    return commits


def ingest_git_commits(store: TriviumStore, repo: str, since: int) -> dict:
    """将近期 commit 入库为 ``git_commit`` 节点（幂等）。

    返回 ``{"added", "skipped", "total"}``：added 为新插入数，skipped 为已存在
    而跳过数，total 为窗口内 commit 总数。
    """
    commits = collect_commits(repo, since)

    # 幂等检查：遍历库中已有节点，收集已有的 commit_hash
    existing_hashes = set()
    for _nid, payload in store.iter_payloads():
        if isinstance(payload, dict):
            ch = payload.get("commit_hash")
            if ch:
                existing_hashes.add(ch)

    added = 0
    skipped = 0
    for c in commits:
        if c["commit_hash"] in existing_hashes:
            skipped += 1
            continue
        node_data = {
            "type": "git_commit",
            "content": c["subject"],
            "importance": 0.5,
            "commit_hash": c["commit_hash"],
            "commit_time": c["commit_time"],
            "repo": repo,
            "source": "git_memory",
        }
        emb = store.embed_text(c["subject"])
        store.insert_node(node_data, emb)
        added += 1

    return {"added": added, "skipped": skipped, "total": len(commits)}
