#!/usr/bin/env python
"""GitHub traffic snapshot —— 把 14 天滚动窗口的流量数据落到本地 JSONL。

为什么需要它：GitHub 只保留近 14 天的 traffic 数据（views / clones / referrers /
popular paths），过期永久丢弃，也没有任何接口能回溯历史。想看出「某次发布带来了多少
关注」「流量是涨还是跌」，只能自己每天抓一次攒成时间序列。

这也是仓库所有者**唯一**能拿到的使用侧信号——GitHub 不提供 clone / 下载者的身份，
releases 资产下载数只在挂了资产时才有意义，源码 zip 下载完全不计数。

用法::

    # 今天抓一次（需要 gh CLI 已登录，或设 GH_TOKEN / GITHUB_TOKEN）
    python scripts/github_traffic_snapshot.py --repo JiaY-77/Palimpsest --out-dir ./traffic

    # 补历史（GitHub 只给 14 天，更早的抓不到；这里只是把已有日期重抓覆盖）
    python scripts/github_traffic_snapshot.py --repo owner/name --out-dir ./traffic

输出::

    <out-dir>/traffic.jsonl   每个抓取日一行（同一天重跑覆盖当天那行，幂等）

只读：仅调用 GitHub 的 GET 接口，不写远端任何东西。
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENDPOINTS = {
    "repo": "repos/{repo}",
    "views": "repos/{repo}/traffic/views",
    "clones": "repos/{repo}/traffic/clones",
    "referrers": "repos/{repo}/traffic/popular/referrers",
    "paths": "repos/{repo}/traffic/popular/paths",
}

REPO_FIELDS = (
    "stargazers_count",
    "forks_count",
    "subscribers_count",
    "open_issues_count",
    "network_count",
)


def gh_api(path: str) -> dict:
    """调一次 GitHub API。优先 gh CLI（已登录即用），失败时回退到裸 HTTP。"""
    try:
        out = subprocess.run(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(out.stdout or "{}")
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError(f"gh CLI 不可用（{exc}），且未设置 GH_TOKEN / GITHUB_TOKEN") from exc

        import urllib.request

        req = urllib.request.Request(
            f"https://api.github.com/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "palimpsest-traffic-snapshot",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())


def snapshot(repo: str) -> dict:
    """抓一次完整快照。单个端点失败不阻断其余（记在 errors 里）。"""
    raw: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, tmpl in ENDPOINTS.items():
        try:
            raw[name] = gh_api(tmpl.format(repo=repo))
        except Exception as exc:  # noqa: BLE001 —— 单端点失败不拖垮整次抓取
            errors[name] = f"{type(exc).__name__}: {exc}"

    repo_info = raw.get("repo") or {}
    entry: dict[str, Any] = {
        "repo": repo,
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "stars": repo_info.get("stargazers_count"),
        "forks": repo_info.get("forks_count"),
        "watchers": repo_info.get("subscribers_count"),
        "open_issues": repo_info.get("open_issues_count"),
        "views": None,
        "clones": None,
        "referrers": None,
        "top_paths": None,
    }

    # GitHub 的 count / uniques 是「近 14 天滚动」，daily 数组是明细。
    # 存 daily，才能在 14 天后仍看得到当天那一格。
    if isinstance(raw.get("views"), dict):
        entry["views"] = {
            "count_14d": raw["views"].get("count"),
            "uniques_14d": raw["views"].get("uniques"),
            "daily": raw["views"].get("views") or [],
        }
    if isinstance(raw.get("clones"), dict):
        entry["clones"] = {
            "count_14d": raw["clones"].get("count"),
            "uniques_14d": raw["clones"].get("uniques"),
            "daily": raw["clones"].get("clones") or [],
        }
    if isinstance(raw.get("referrers"), list):
        entry["referrers"] = raw["referrers"]
    if isinstance(raw.get("paths"), list):
        entry["top_paths"] = raw["paths"][:10]

    if errors:
        entry["errors"] = errors
    return entry


def write_entry(out_dir: Path, entry: dict) -> Path:
    """按抓取日写入 JSONL：同一天重跑覆盖当天那行（幂等），历史行原样保留。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "traffic.jsonl"
    today = entry["captured_at"][:10]

    rows: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                rows.append(line)  # 坏行原样保留，不静默吞掉
                continue
            if str(existing.get("captured_at", ""))[:10] == today:
                continue  # 当天旧记录被新记录替换
            rows.append(json.dumps(existing, ensure_ascii=False))

    rows.append(json.dumps(entry, ensure_ascii=False))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot GitHub traffic into a JSONL series (14-day rolling window).")
    parser.add_argument("--repo", required=True, help="owner/name, e.g. JiaY-77/Palimpsest")
    parser.add_argument("--out-dir", default="traffic", help="directory to write traffic.jsonl (default: ./traffic)")
    parser.add_argument("--print", dest="do_print", action="store_true", help="also dump the entry to stdout")
    args = parser.parse_args(argv)

    entry = snapshot(args.repo)
    path = write_entry(Path(args.out_dir), entry)

    if args.do_print:
        print(json.dumps(entry, ensure_ascii=False, indent=2))
    clones = entry["clones"] or {}
    views = entry["views"] or {}
    print(
        f"[{entry['captured_at']}] {args.repo} "
        f"stars={entry['stars']} forks={entry['forks']} "
        f"clones_14d={clones.get('uniques_14d')} unique / {clones.get('count_14d')} total, "
        f"views_14d={views.get('uniques_14d')} unique / {views.get('count_14d')} total → {path}",
        file=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
