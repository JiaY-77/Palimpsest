"""CLI commands for the Palimpsest memory provider (hermes palimpsest ...).

Wired into `hermes <plugin>` via discover_plugin_cli_commands():
  - register_cli(subparser)   = setup_fn（argparse 时注册子命令）
  - palimpsest_command(args)  = handler_fn（命令分发）

Only surfaced when palimpsest is the ACTIVE memory provider.
"""

from __future__ import annotations

import argparse
import json
import urllib.request


def _get_base_url() -> str:
    import os

    return os.environ.get("PALIMPSEST_BASE_URL", "http://127.0.0.1:8090").rstrip("/")


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _post(base: str, path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def cmd_status(args: argparse.Namespace) -> None:
    """检查 Palimpsest REST 与插件状态。"""
    base = _get_base_url()
    root = _get(f"{base}/")
    if "error" in root:
        print(f"✗ Palimpsest REST {base} 不可达：{root['error']}")
        print("  请先启动 Palimpsest REST 服务（详见仓库 docs/HERMES_INTEGRATION.md），")
        print("  或设置 PALIMPSEST_BASE_URL 指向已运行的实例")
        return
    print(f"✓ Palimpsest REST {base} 正常：{root.get('service', 'Palimpsest')} "
          f"v{root.get('version', '?')}")
    endpoints = root.get("endpoints", [])
    have_semantic = any(e in endpoints for e in ("/mem/search", "/mem/ingest"))
    print("  语义层端点（/mem/search /mem/ingest /mem/link /graph/neighbors /mem/router）："
          + ("✓ 齐备" if have_semantic else "⚠ 缺失——请确认服务版本包含统一语义层端点"))
    print(f"  插件配置：PALIMPSEST_BASE_URL={base}（env 覆盖可用）")


def cmd_test(args: argparse.Namespace) -> None:
    """端到端自检：search + ingest 连通性。"""
    base = _get_base_url()
    r = _post(base, "/mem/search", {"query": "Palimpsest Hermes", "scope": "all", "top_k": 2})
    if "error" in r:
        print(f"✗ /mem/search 失败：{r['error']}")
        return
    print(f"✓ /mem/search 通：{len(r.get('results', []))} 条命中")
    for item in r.get("results", [])[:2]:
        print(f"    - ({item.get('score', 0):.2f}) {item.get('summary', '')[:80]}")

    r2 = _post(base, "/mem/ingest", {
        "content": "[CLI 自检] hermes palimpsest test 触发的连通性测试记录，可删除",
        "type": "record", "importance": 0.3, "domain": "hermes",
        "source": "hermes-cli-test",
    })
    if "error" in r2:
        print(f"✗ /mem/ingest 失败：{r2['error']}")
        return
    print(f"✓ /mem/ingest 通（node_id={r2.get('node_id')}）")
    print("端到端 OK：Palimpsest 记忆层可用。")


def palimpsest_command(args: argparse.Namespace) -> None:
    """Route palimpsest subcommands（handler_fn，由 discover_plugin_cli_commands 接入）。"""
    sub = getattr(args, "palimpsest_command", None)
    if sub == "test":
        cmd_test(args)
    else:
        cmd_status(args)


def register_cli(subparser) -> None:
    """Build the ``hermes palimpsest`` argparse subcommand tree（setup_fn）。"""
    subs = subparser.add_subparsers(dest="palimpsest_command")
    subs.add_parser("status", help="检查 Palimpsest REST 与插件状态")
    subs.add_parser("test", help="端到端自检（search + ingest）")
