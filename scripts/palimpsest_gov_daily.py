"""
palimpsest_gov_daily.py — 小帕每日记忆治理哨兵（watchdog 静默模式）
==================================================================
把「记忆治理」从「想起来才做」变成「每天自动巡检一次」——这是小帕
从「记忆库」到「记忆引擎」的收尾动作（治理自动化）。
本文件住仓库 scripts/（2026-10-19 起，issue #128 B 项），路径从脚本自身
位置派生（PROJECT_DIR / venv），不再写死绝对路径。

【写与不写的边界（issue #128 起不再是「纯 dry-run」）】
  - consolidate（容量合并）与 task-archive（任务归档）仍是**不可逆写操作**：
    consolidate 把高相似度的两条记忆合并成一条，旧节点被标 outdated；
    task-archive 把已完成任务节点导出到知识库 md 后**删除节点**。
    二者一旦误判就无法简单回退，因此本脚本对它们**只做 dry-run 巡检**：
    有候选/异常 → 输出记录（cron no_agent 模式下会通知主人）；
    无候选且一切正常 → 输出空字符串（静默，不打扰）。
    真正的 apply 由主人确认后手工执行（或后续另开「自动 + 备份」策略）。
  - 僵尸任务降级（超 30 天未动的 doing/blocked）是**有留痕的可逆写**：
    走 ``PUT /memory/{id}``（= ``core.task_state.apply_task_patch``），自动写
    ``type=record`` 日志节点 + ``LOGS`` 边——何时降的、从什么状态降的全部可回溯。
    动作只有三件：``task_state`` 置 ``blocked``、content 前缀加 ``[待复核]``、
    ``last_touched_at`` 更新，不改正文其余部分；人工复核后可原样恢复
    （去掉前缀 + 置回状态）。content 已带 ``[待复核]`` 前缀的节点不重复降级
    （幂等）。超 60 天的**额外**输出归档建议清单（降级照做——越老越该标待复核），
    但归档 / 删除本身不执行——仍由人拍板。

【巡检内容】（顺序固定）
  1. consolidate    → POST /mem/consolidate  （REST，只读 dry-run）
  2. task-archive   → CLI                    （暂无 REST 端点，见脚注 ①）
  3. doctor         → GET  /doctor           （REST，只读体检）
  4. 僵尸任务治理    → GET /tasks/active（只读列表）+ PUT /memory/{id}（有留痕降级）

【为什么改走 REST（而不是 CLI 直接开库）】
  TriviumDB 是**连接级排他**的：常驻 REST 服务（127.0.0.1:8090）持有库时，
  任何第二个进程都开不了库 —— 连 access_mode="read_only" 也会被拒，报
  `Database locked: ... is already opened with an incompatible access mode`。
  历史上本脚本用 CLI 直接开库跑 consolidate / doctor，等于在 REST 之外又开
  一个库连接：偶发撞库失败，极端情况下会破坏 storage generation（多文件
  世代存储，不可逆）。改走 REST 后这两个动作与常驻服务共用同一份连接，
  不再制造第二个写者。

【PYTHONPATH 必须清（2026-10-14 定位的真因）】
  从 Hermes 起的进程，环境里带着 Hermes 自己的 PYTHONPATH（指向它自己的
  site-packages，如 Python 3.14）。子进程若改用 Palimpsest venv 的 Python 3.12，
  会**先**加载那份 3.14 的编译扩展（_pydantic_core.cp314-*.pyd）而崩：
      ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
  本脚本此前每天都报这一句（cron 记的 task-archive 异常）。故：凡起子进程，
  一律先 pop 掉 PYTHONPATH / PYTHONHOME（见 _clean_env）。

脚注 ①：task-archive 目前没有对应的 REST 端点，仍走 CLI（会开库）。它通常
        「无候选 → 静默」，且排在 REST 调用之后；要彻底消除它的争库风险，
        需要给它补一个 REST 端点（待办）。

用法：
  python scripts/palimpsest_gov_daily.py    # 巡检（watchdog 静默）
退出码恒为 0（治理哨兵本身失败不该让 cron 报红；异常会写进输出）。
"""

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 僵尸任务两级阈值（天，issue #128 B 项）：doing/blocked 超 30 天未动 → 自动降级
# （task_state=blocked + [待复核] 前缀，有留痕可回溯）；超 60 天 → **额外**输出归档建议
# （两级不互斥：超 60 天的既降级又建议归档）。
# ---------------------------------------------------------------------------
DEMOTE_STALE_DAYS = 30
ARCHIVE_SUGGEST_DAYS = 60
REVIEW_PREFIX = "[待复核]"

# ---------------------------------------------------------------------------
# 常量：Palimpsest 环境（从脚本自身位置派生，不再写死绝对路径；
# REST 地址保留环境变量 PALIMPSEST_BASE_URL 的覆盖能力）
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_DIR / "venv" / "Scripts" / "python.exe"
CLI = PROJECT_DIR / "scripts" / "palimpsest_cli.py"
REST_BASE = (os.environ.get("PALIMPSEST_BASE_URL") or "http://127.0.0.1:8090").rstrip("/")


def _force_utf8_io():
    for stream in (sys.stdout, sys.stderr):
        # 老解释器 / 非 tty 没有 reconfigure —— 静默跳过
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _clean_env():
    """子进程环境：剥掉 PYTHONPATH / PYTHONHOME。

    否则 venv 解释器会先加载 Hermes 自己环境（另一 Python 版本）的编译扩展而崩
    （详见模块 docstring「PYTHONPATH 必须清」一节）。
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _rest(method, path, body=None, timeout=300):
    """调 REST，返回 (ok: bool, text: str)。失败时 text 是错误描述。"""
    url = REST_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 —— 统一转成可读文本，不向上抛栈
        return False, f"{type(exc).__name__}: {exc}"


def run_step(argv, timeout):
    """运行一条 CLI 子命令，返回 (exit_code, stdout, stderr, timed_out)。"""
    cmd = [str(VENV_PYTHON), str(CLI), *argv]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(PROJECT_DIR),
            env=_clean_env(),
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", False
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
        return None, out, err, True


# ---------------------------------------------------------------------------
# 各步骤输出分析：有候选/异常 → 记录文本；无 → None（静默）
# ---------------------------------------------------------------------------
def _last_json(stdout):
    """从输出里取最后一个 JSON 对象（CLI 可能先打日志行再打 JSON）。失败返回 None。"""
    text = (stdout or "").strip()
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        try:
            return json.loads(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
    return None


def analyze_consolidate(stdout):
    """预览可合并的记忆对：will_merge 非空 → 记录；否则静默。"""
    data = _last_json(stdout)
    if not isinstance(data, dict):
        # 解析不出 JSON 又非空 → 当作异常信号（避免静默吞掉）
        return f"consolidate 输出无法解析（{stdout.strip()[:120]}）" if (stdout or "").strip() else None
    will = data.get("will_merge") or []
    n = len(will)
    if n <= 0:
        return None
    return f"记忆治理：发现 {n} 对高相似度记忆可合并（consolidate dry-run，确认后 --apply）"


def analyze_task_archive(stdout):
    """预览可归档的已完成任务：candidates 非空 → 记录；否则静默。"""
    data = _last_json(stdout)
    if not isinstance(data, dict):
        return f"task-archive 输出无法解析（{stdout.strip()[:120]}）" if (stdout or "").strip() else None
    cands = data.get("candidates") or []
    n = len(cands)
    if n <= 0:
        return None
    return f"任务治理：发现 {n} 个已完成任务可归档（task-archive dry-run，确认后 --apply）"


def analyze_health(stdout):
    """doctor 体检（REST JSON）：任一 check ok=false → 记录；全 OK → 静默。

    不用原来的字符串匹配：REST 端点返回结构化 ``{"ok", "checks": [{name, ok, detail, fix}]}``，
    按 ok 字段判定比匹配「警告/不可用」等字样准。
    """
    data = _last_json(stdout)
    if not isinstance(data, dict):
        return f"doctor 输出无法解析（{stdout.strip()[:120]}）" if (stdout or "").strip() else None
    checks = data.get("checks") or []
    bad = [c for c in checks if not c.get("ok")]
    if not bad:
        return None
    items = " | ".join(f"{c.get('name')}: {(c.get('detail') or '')[:80]}" for c in bad[:4])
    return f"小帕健康警告（{len(bad)}/{len(checks)} 项失败）: {items}"[:300]


# ---------------------------------------------------------------------------
# 僵尸任务治理（issue #128 B 项）：判定 → 降级决策 → 有留痕的可逆写
# ---------------------------------------------------------------------------
def _iso_epoch(value):
    """ISO8601 串 → epoch 秒；解析不出 → None（naive 按 UTC，与既有约定一致）。"""
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _touched_epoch(task):
    """僵尸判据时间（epoch 秒）：``last_touched_at`` 与 ``created_at`` **取更早者**。

    与 ``core.task_state._freshness_epoch`` 同口径（2026-10-19 主人拍板）：存量
    任务的 ``last_touched_at`` 会被批量回填刷成同一天，单看它会把「52 天前建的老
    任务」判成「11 天前动过」而永远治不到。两个时间戳都缺 / 解析不出 → None（不猜）。
    """
    candidates = []
    raw = task.get("last_touched_at")
    if raw:
        epoch = _iso_epoch(raw)
        if epoch is not None:
            candidates.append(epoch)
    created = task.get("created_at")
    if isinstance(created, (int, float)) and created > 0:
        candidates.append(float(created))
    elif created:
        epoch = _iso_epoch(created)
        if epoch is not None:
            candidates.append(epoch)
    return min(candidates) if candidates else None


def decide_stale_actions(briefs, now, demote_days=DEMOTE_STALE_DAYS, archive_days=ARCHIVE_SUGGEST_DAYS):
    """僵尸判定 + 降级决策（**纯函数**：输入清单 + 时刻 + 阈值，输出两份清单）。

    不碰网络、不碰库，便于单测。输入：``briefs`` = ``GET
    /tasks/active?states=doing,blocked&fresh_days=0`` 的 ``results``（每条含 id /
    state / title / last_touched_at / created_at；调用方可选补 content 供幂等预判）、
    当前时刻 ``now``（epoch 秒）、两级阈值（天）。

    输出 ``(demotions, archive_suggestions)``：
      - demotions：age > demote_days 的条目，应降级（**含超 archive_days 的**——
        越老越该标待复核）。content 已带 ``[待复核]`` 前缀的跳过（幂等——决策时
        拿得到 content 就不重复降）；无 ``last_touched_at`` / 解析不出的跳过（不猜）。
      - archive_suggestions：age > archive_days 的条目，只建议归档不执行；
        与 demotions **不互斥**（超 60 天的既降级又建议归档）。
    """
    demotions = []
    archive_suggestions = []
    for task in briefs or []:
        if task.get("state") not in ("doing", "blocked"):
            continue  # 只治理进行中/阻塞的；todo 没人认领是另一回事
        epoch = _touched_epoch(task)
        if epoch is None:
            continue  # 无判据 → 跳过不猜
        age = (now - epoch) / 86400  # 浮点天：比较用它，取整只影响展示（否则「30天+1秒」被截回 30 天）
        info = {
            "id": task.get("id"),
            "title": task.get("title") or "",
            "state": task.get("state"),
            "age_days": int(age),
        }
        already_reviewed = str(task.get("content") or "").lstrip().startswith(REVIEW_PREFIX)
        if age > demote_days and not already_reviewed:
            demotions.append(info)
        if age > archive_days:
            archive_suggestions.append(info)
    return demotions, archive_suggestions


def run_stale_demotion(records):
    """僵尸任务治理步：只读列表 → 决策 → 对降级清单逐条 PUT（有留痕的可逆写）。

    ``fresh_days=0`` 必须显式传：``/tasks/active`` 默认已按新鲜度折叠，超 30 天的
    条目根本不在 ``results`` 里——不展开就永远看不到要治理的对象（A/B 两处口径
    必须对齐，issue #128）。

    决策用的 brief 不带 content，幂等在执行点用全文再核一道（双保险）：
    取回 content 已带 ``[待复核]`` 前缀的跳过，不重复降级。
    """
    ok, text = _rest("GET", "/tasks/active?states=doing,blocked&limit=100&fresh_days=0")
    if not ok:
        records.append(f"治理脚本异常: 僵尸任务治理（REST {REST_BASE} 不可达）{text[:200]}")
        return
    data = _last_json(text)
    briefs = data.get("results") or [] if isinstance(data, dict) else []
    demotions, archive_suggestions = decide_stale_actions(briefs, now=time.time())

    demoted = []
    for item in demotions:
        nid = item["id"]
        ok_get, body = _rest("GET", f"/memory/{nid}")
        node = _last_json(body) if ok_get else None
        payload = (node.get("payload") or {}) if isinstance(node, dict) else {}
        content = str(payload.get("content") or "")
        if not ok_get or payload.get("type") != "task" or not content or content.lstrip().startswith(REVIEW_PREFIX):
            continue  # 节点没了 / 类型变了 / 空正文 / 已降级过 —— 都不写
        ok_put, put_text = _rest(
            "PUT",
            f"/memory/{nid}",
            {"task_state": "blocked", "content": f"{REVIEW_PREFIX} {content}"},
        )
        label = str(item.get("title") or "")[:40]
        if ok_put:
            demoted.append(f"#{nid} [{item.get('state')}] {item.get('age_days')}天 {label}")
        else:
            records.append(f"治理脚本异常: 僵尸任务降级 PUT /memory/{nid} 失败 {put_text[:120]}")

    if demoted:
        records.append(
            f"僵尸任务治理：{len(demoted)} 条 doing/blocked 超 {DEMOTE_STALE_DAYS} 天未动，已降级待复核"
            f"（task_state=blocked + [待复核] 前缀，record 日志可回溯）→ " + " | ".join(demoted)
        )
    if archive_suggestions:
        items = " | ".join(
            f"#{a['id']} [{a['state']}] {a['age_days']}天 {str(a.get('title') or '')[:40]}"
            for a in archive_suggestions[:5]
        )
        head = f"任务治理：{len(archive_suggestions)} 条超 {ARCHIVE_SUGGEST_DAYS} 天未动，建议归档（未执行）"
        records.append(f"{head} → {items}"[:300])


def main():
    _force_utf8_io()
    records = []

    # 1) consolidate —— 走 REST（只读 dry-run，不开库）
    ok, text = _rest("POST", "/mem/consolidate", {"threshold": 0.85})
    if not ok:
        records.append(f"治理脚本异常: consolidate（REST {REST_BASE} 不可达）{text[:200]}")
    else:
        rec = analyze_consolidate(text)
        if rec:
            records.append(rec)

    # 2) task-archive —— 暂无 REST 端点，仍走 CLI（见模块 docstring 脚注 ①）
    code, out, err, timed_out = run_step(["task-archive"], 300)
    if timed_out or code != 0:
        detail = (err or "").strip()[:200] or (out or "").strip()[:200] or "无输出"
        tag = "task-archive（超时 300s）" if timed_out else f"task-archive（退出码 {code}）"
        records.append(f"治理脚本异常: {tag} {detail}")
    else:
        rec = analyze_task_archive(out)
        if rec:
            records.append(rec)

    # 3) doctor —— 走 REST（只读体检，不开库）
    ok, text = _rest("GET", "/doctor")
    if not ok:
        records.append(f"治理脚本异常: doctor（REST {REST_BASE} 不可达）{text[:200]}")
    else:
        rec = analyze_health(text)
        if rec:
            records.append(rec)

    # 4) 僵尸任务治理 —— 只读列表 + 有留痕的可逆降级写（issue #128 B 项）
    run_stale_demotion(records)

    # watchdog 静默模式：有记录才输出；无记录 → stdout 为空
    if records:
        print("\n".join(records))


if __name__ == "__main__":
    main()
