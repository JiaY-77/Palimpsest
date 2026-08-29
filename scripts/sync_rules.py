#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_rules.py — 规则笔记 → 模型路由决策树 JSON 同步器
=====================================================================
原则：知识库里的「规则类笔记」（frontmatter tags 含 rule）是唯一事实源，
本脚本扫描这些笔记、启发式提取关键字段，自动更新执行层的
模型路由决策树.json（direction_profiles 等条目），保证「笔记改了，路由跟着改」。

功能：
  1. 递归扫描 Knowledge 目录（跳过 .obsidian）下所有 .md，
     解析 frontmatter，找出 tags 含 "rule" 的笔记；
  2. 对每篇规则笔记从正文启发式提取：
       - 触发条件（含「触发/条件/When/仅当」的行）
       - 推荐模型（r1 / qwen9b / glm / phi / opencode / deepseek 等 + 配置词）
       - 权重（含「权重/score/priority/fail_count/降权/打分/×1.3」的行）
     提取不到就标注「见原文」；
  3. 更新决策树 JSON：
       - 按笔记主题映射到 direction_profiles 下的方向条目（如副官加班协议→副官推理/有界推理；
         军团办法→基础代码/提取），更新其 note 字段（增补「来自笔记:XXX」标记，不覆盖原有内容）；
       - 映射不到的方向（宪法→分级执行、决策树→路由规则）→ 新增一个带
         note="来自笔记:XXX" 的条目；
       - 绝对不删除任何现有条目；
       - 写文件前把原 JSON 备份到同目录 .bak-sync-rules-<时间戳>；
  4. 输出变更日志（对比修改前后，打印新增/更新了哪些条目）；
  5. 幂等：二次运行无新变更时输出「无变更」，不写文件、不产生新备份。

用法：
  python scripts/sync_rules.py            # 正式同步（备份 + 写 JSON + 变更日志）
  python scripts/sync_rules.py --dry-run  # 只打印将做的变更，不写任何文件

安全：本脚本只读笔记、只写决策树 JSON（带备份）。不碰其他脚本。
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime

# ----------------------------------------------------------------------------
# 路径常量（环境变量优先；可按需用 --kb-root / --json-path 覆盖）
# ----------------------------------------------------------------------------
from _common import SCRIPT_DIR as _SCRIPT_DIR, PROJECT_ROOT as _PROJECT_ROOT
# 知识库根目录：环境变量 KNOWLEDGE_ROOT 优先；默认约定为项目根下 ./knowledge，不硬编码个人路径
KNOWLEDGE_ROOT = os.getenv("KNOWLEDGE_ROOT", "") or os.path.normpath(
    os.path.join(_PROJECT_ROOT, "knowledge")
)
DECISION_TREE_JSON = os.getenv("DECISION_TREE_JSON", "") or os.path.join(
    KNOWLEDGE_ROOT, "03_技术学习/模型路由决策树.json"
)
RULE_TAG = "rule"

# ----------------------------------------------------------------------------
# 主题 → direction_profiles 方向映射
#   match: 标题/文件名须同时包含的关键词
#   directions: 命中时更新 note 的现有方向（direction_profiles 键）
#   new_direction: 无匹配方向时新增的方向名（None 表示不新增）
# ----------------------------------------------------------------------------
DIRECTION_MAP = [
    {"match": ("副官", "加班"), "directions": ("副官推理", "有界推理"), "new_direction": None},
    {"match": ("宪法",), "directions": (), "new_direction": "分级执行"},
    {"match": ("军团", "办法"), "directions": ("基础代码", "提取"), "new_direction": None},
    {"match": ("决策树",), "directions": (), "new_direction": "路由规则"},
]

# 模型别名：正文里的写法 → 统一短名
MODEL_ALIASES = [
    (re.compile(r"deepseek[- ]r1[:：]?8b", re.I), "r1"),
    (re.compile(r"\br1\b", re.I), "r1"),
    (re.compile(r"qwen3?\.?5?[:：]?9b", re.I), "qwen9b"),
    (re.compile(r"qwen9b", re.I), "qwen9b"),
    (re.compile(r"qwen2\.5[:：]3b", re.I), "3b"),
    (re.compile(r"\b3b\b"), "3b"),
    (re.compile(r"glm[- ]?4\.7|glm[- ]?4\.5|\bglm\b", re.I), "glm"),
    (re.compile(r"\bphi\b", re.I), "phi"),
    (re.compile(r"opencode", re.I), "opencode"),
    (re.compile(r"deepseek", re.I), "deepseek"),
    (re.compile(r"qwen2\.5[- ]coder[:：]?7b", re.I), "qwen7b"),
]

# 配置词（推荐模型行的佐证）
CONFIG_PATTERNS = [
    re.compile(r"preset", re.I),
    re.compile(r"think\s*[:＝=]\s*false", re.I),
    re.compile(r"\d{3,4}\s*tokens?\b", re.I),
    re.compile(r"max_tokens\s*[:＝=]\s*\d+", re.I),
    re.compile(r"endpoint\s*[:＝=]", re.I),
    re.compile(r"retry_on_empty", re.I),
    re.compile(r"\b\d{3,4}\b\s*token", re.I),
]

TRIGGER_RE = re.compile(r"触发|When|仅当|条件是|判据|判「", re.I)
WEIGHT_RE = re.compile(r"权重|×1\.\d+|score|importance|priority|fail_count|降权|打分|降分|评分", re.I)
FRONTMATTER_RE = re.compile(r"^---\s*$")


# ----------------------------------------------------------------------------
# 1) 扫描规则笔记
# ----------------------------------------------------------------------------
def parse_frontmatter_tags(text):
    """极简 frontmatter 解析：只取 tags 列表（YAML 列表形式），零依赖。"""
    lines = text.splitlines()
    if len(lines) < 3 or not FRONTMATTER_RE.match(lines[0]):
        return []
    end = None
    for i in range(1, len(lines)):
        if FRONTMATTER_RE.match(lines[i]):
            end = i
            break
    if end is None:
        return []
    tags = []
    in_tags = False
    for line in lines[1:end]:
        if line.strip() == "tags:":
            in_tags = True
            continue
        if in_tags:
            m = re.match(r"\s*-\s*(.+?)\s*$", line)
            if m:
                tags.append(m.group(1).strip())
            elif line and not line.startswith(" "):
                in_tags = False
    return tags


def scan_rule_notes(root):
    """递归扫描 .md，返回带 rule 标签的笔记 (path, title)。跳过 .obsidian 等目录。"""
    skip_dirs = {".obsidian", ".git", ".trash", "__pycache__", "node_modules"}
    notes = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not fn.lower().endswith(".md"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError) as e:
                print(f"[警告] 无法读取 {path}: {e}", file=sys.stderr)
                continue
            tags = parse_frontmatter_tags(text)
            if RULE_TAG in tags:
                notes.append((path, os.path.splitext(fn)[0]))
    return notes


# ----------------------------------------------------------------------------
# 2) 启发式提取字段
# ----------------------------------------------------------------------------
def _clip(s, limit):
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + "…" if len(s) > limit else s


def _is_sep_row(line):
    """表格分隔行，如 |---|---|"""
    return bool(re.match(r"^\|[\s:\-|]+\|?$", line))


def _is_table_row(line):
    return line.startswith("|")


def extract_note_fields(body_lines, title):
    """从正文提取 触发条件 / 推荐模型 / 权重。提取不到 → 空串（由调用方标「见原文」）。
    触发条件/权重优先取普通行，表格行（信息密度低、含表头噪音）作兜底。"""
    non_table_trigger, table_trigger = [], []
    non_table_weight, table_weight = [], []
    model_hits = {}  # 模型名 -> 配置词列表（保序去重）
    model_order = []

    for raw in body_lines:
        line = raw.strip()
        if not line or _is_sep_row(line):
            continue
        is_table = _is_table_row(line)
        # --- 触发条件 ---
        if TRIGGER_RE.search(line) and len(line) <= 200:
            bucket = table_trigger if is_table else non_table_trigger
            if line not in bucket:
                bucket.append(line)
        # --- 权重 ---
        if WEIGHT_RE.search(line) and len(line) <= 200:
            bucket = table_weight if is_table else non_table_weight
            if line not in bucket:
                bucket.append(line)
        # --- 推荐模型 ---
        for pat, alias in MODEL_ALIASES:
            if pat.search(line):
                if alias not in model_hits:
                    model_hits[alias] = []
                    model_order.append(alias)
                for cp in CONFIG_PATTERNS:
                    m = cp.search(line)
                    if m:
                        frag = m.group(0)
                        if frag not in model_hits[alias]:
                            model_hits[alias].append(frag)

    trigger_lines = non_table_trigger or table_trigger
    weight_lines = non_table_weight or table_weight

    trigger = "；".join(_clip(x, 120) for x in trigger_lines[:3])[:600]
    weight = "；".join(_clip(x, 120) for x in weight_lines[:3])[:600]

    models = []
    for alias in model_order[:5]:
        cfg = ",".join(model_hits[alias][:3])
        models.append(f"{alias}({cfg})" if cfg else alias)
    recommended = "；".join(models)[:400]

    return {
        "title": title,
        "trigger_condition": trigger,
        "recommended_model": recommended,
        "weight": weight,
    }


def build_note(note_title, fields):
    """构造要写入 JSON 的 note 文本。"""
    parts = [f"来自笔记:{note_title}"]
    if fields["trigger_condition"]:
        parts.append(f"触发条件: {fields['trigger_condition']}")
    if fields["recommended_model"]:
        parts.append(f"推荐模型: {fields['recommended_model']}")
    if fields["weight"]:
        parts.append(f"权重: {fields['weight']}")
    if len(parts) == 1:
        parts.append("关键字段未提取到，详见原文")
    return "；".join(parts)


# ----------------------------------------------------------------------------
# 3) 应用到 JSON（只增补/更新 note 或新增条目，绝不删除）
# ----------------------------------------------------------------------------
def apply_note_to_json(data, fields, changes):
    """把一篇规则笔记的提取结果应用到决策树 JSON，记录变更日志。"""
    title = fields["title"]
    new_note = build_note(title, fields)
    marker = f"来自笔记:{title}"
    dp = data.setdefault("direction_profiles", {})

    target = None
    for entry in DIRECTION_MAP:
        if all(k in title for k in entry["match"]):
            target = entry
            break

    if target is None:
        # 映射不到 → 新增条目（保险兜底，正常不会走到）
        key = f"未归类规则:{title}"
        if key not in dp:
            dp[key] = {"note": new_note, "synced_at": today_str()}
            changes.append(("新增", f"direction_profiles['{key}']", "(无)", new_note))
        return

    for direction in target["directions"]:
        if direction not in dp:
            # 方向不存在（正常都有）→ 新增
            dp[direction] = {"note": new_note, "synced_at": today_str()}
            changes.append(("新增", f"direction_profiles['{direction}']", "(无)", new_note))
            continue
        old_note = dp[direction].get("note", "")
        if marker in old_note:
            # 幂等：该笔记的来源标记已存在 → 不动
            continue
        new_val = (old_note + "；" + new_note) if old_note else new_note
        dp[direction]["note"] = new_val
        changes.append(("更新", f"direction_profiles['{direction}'].note", old_note or "(无)", new_val))

    if target["new_direction"]:
        direction = target["new_direction"]
        if direction not in dp:
            dp[direction] = {
                "note": new_note,
                "trigger_condition": fields["trigger_condition"] or "见原文",
                "recommended_model": fields["recommended_model"] or "见原文",
                "weight": fields["weight"] or "见原文",
                "synced_at": today_str(),
                "synced_by": "scripts/sync_rules.py（笔记为准，自动同步）",
            }
            changes.append(("新增", f"direction_profiles['{direction}']", "(无)", new_note))
        else:
            old_note = dp[direction].get("note", "")
            if marker not in old_note:
                new_val = (old_note + "；" + new_note) if old_note else new_note
                dp[direction]["note"] = new_val
                changes.append(("更新", f"direction_profiles['{direction}'].note", old_note or "(无)", new_val))


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# 4) 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="规则笔记 → 模型路由决策树 JSON 同步器")
    ap.add_argument("--dry-run", action="store_true", help="只输出将做的变更，不写文件、不备份")
    ap.add_argument("--kb-root", default=KNOWLEDGE_ROOT, help="知识库根目录")
    ap.add_argument("--json-path", default=DECISION_TREE_JSON, help="决策树 JSON 路径")
    args = ap.parse_args()

    mode = "dry-run（未写文件）" if args.dry_run else "正式"
    print(f"=== sync_rules 开始（{mode}）===")
    print(f"知识库: {args.kb_root}")
    print(f"决策树: {args.json_path}")

    # 1) 扫描规则笔记
    notes = scan_rule_notes(args.kb_root)
    if not notes:
        print("[结果] 未找到任何带 #rule 标签的笔记，退出。")
        return 0
    print(f"找到 {len(notes)} 篇规则笔记：")
    for path, title in notes:
        print(f"  - {title}（{path}）")

    # 2) 读决策树 JSON
    if not os.path.exists(args.json_path):
        print(f"[错误] 决策树 JSON 不存在: {args.json_path}", file=sys.stderr)
        return 1
    with open(args.json_path, encoding="utf-8") as f:
        data = json.load(f)

    before_dump = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)

    # 3) 提取 + 应用
    changes = []  # (动作, 位置, 原值, 新值)
    for path, title in notes:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        body_lines = text.splitlines()
        fields = extract_note_fields(body_lines, title)
        apply_note_to_json(data, fields, changes)

    after_dump = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    has_diff = before_dump != after_dump

    # 4) 变更日志
    print("\n=== 变更日志 ===")
    if not changes or not has_diff:
        print("无变更：决策树 JSON 已是最新（规则笔记无新增/变更，幂等通过）")
    else:
        for action, where, old, new in changes:
            print(f"[{action}] {where}")
            if action == "更新":
                print(f"    原: {old[:200]}")
                print(f"    新: {new[:200]}")
            else:
                print(f"    内容: {new[:200]}")
    print(f"\n共 {len(changes)} 条变更，内容差异: {'有' if has_diff else '无'}")

    # 5) 写文件（正式模式且有变更）
    if args.dry_run:
        print("\n[dry-run] 未写任何文件。")
        return 0
    if not has_diff:
        print("\n[结果] 无需写文件（幂等）。")
        return 0

    bak = args.json_path + f".bak-sync-rules-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(args.json_path, bak)
    print(f"\n[备份] {bak}")

    with open(args.json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[写入] {args.json_path}")
    print("[结果] 同步完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
