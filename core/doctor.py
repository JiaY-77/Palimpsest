"""
部署体检模块 —— doctor 命令
===========================
面向部署者的全面体检：任何一项失败都打印具体修复命令/动作，
而非只说「失败」。

检查项：
  ①~⑤  复用 run_startup_check()（关键文件 / Store 初始化 / FTS5 / 依赖 / Embedding）
  ⑥     向量维度一致性（实测 embedding 维度 vs 库实际维度，复用 reindex 逻辑）

设计原则：
  - 单项失败不中断（embedding 不可用时其余检查项仍全部执行并展示）
  - 每项含 name / ok / detail / fix（fix 为可执行修复建议文本，通过项可为空）
  - 内部永不抛异常（护栏原则）
"""


from core.startup_check import run_startup_check

# 维度校验复用 reindex 的实现（避免两份漂移）
from scripts.reindex import _get_db_dim


def _check_dimension_consistency():
    """向量维度一致性：实测 embedding 维度 vs 库实际维度。

    独立于 startup_check 的 embedding 探针执行，确保 embedding 不可用时
    也能尝试完成 DB 维度读取（提供更多信息给部署者）。
    返回 (ok, detail, fix)。
    """
    from core.trivium_store import TriviumStore

    store = TriviumStore()

    # 1) 实测 embedding 维度（probe）
    actual_dim = None
    probe_err = None
    try:
        vec = store.embed_text("palimpsest doctor dimension probe")
        actual_dim = len(vec)
    except Exception as e:
        probe_err = str(e)

    # 2) 库实际维度（独立于 embedding，只要 DB 文件可读即可获取）
    db_dim = None
    db_err = None
    try:
        db_dim, db_err = _get_db_dim(store)
    except Exception as e:
        db_err = str(e)

    # 3) 汇总判断
    emb_fix = (
        "启动 Ollama 并拉取模型: ollama pull qwen3-embedding:0.6b；"
        "若使用云端 provider，请确认 EMBEDDING_API_KEY 已设置"
    )
    if probe_err and (db_dim is None):
        return (
            False,
            f"Embedding 服务不可用: {probe_err}（无法探测维度）",
            emb_fix,
        )
    if probe_err:
        return (
            False,
            f"Embedding 服务不可用: {probe_err}（DB 维度={db_dim}，无法完成比对）",
            emb_fix,
        )
    if db_err:
        return (
            False,
            f"无法读取数据库维度: {db_err}",
            "",
        )
    if actual_dim == db_dim:
        return (True, f"维度一致: {actual_dim} 维（实测 = 库）", "")

    # 维度不一致 —— 给出完整修复步骤
    fix = (
        "跨模型向量空间不兼容，必须新建库。请按以下步骤操作：\n"
        "  1. 导出:  python scripts/export_all_data.py\n"
        "  2. 重建:  python scripts/rebuild_db.py\n"
        f"  3. 切换:  修改 .env 中 EMBEDDING_DIM / OLLAMA_EMBEDDING_DIM = {actual_dim}\n"
        "  4. 重索引知识库:  python scripts/build_kb_index.py --full\n"
        "  5. 如有小说设定:  python scripts/build_novel_index.py --source <vault> --full"
    )
    return (
        False,
        f"维度不匹配: 当前 provider 实测 {actual_dim} 维，库实际 {db_dim} 维",
        fix,
    )


def run_doctor() -> dict:
    """执行全部体检，返回结构化结果（永不抛异常）。

    返回格式:
        {"ok": bool, "checks": [{"name", "ok", "detail", "fix"}, ...]}
    """
    checks = []

    # 第一阶段：复用 startup_check 的 5 项检查（fail-fast 已在其内部隔离）
    startup = run_startup_check()
    for c in startup["checks"]:
        checks.append({
            "name": c["name"],
            "ok": c["ok"],
            "detail": c["detail"],
            "fix": _suggest_fix(c),
        })

    # 第二阶段：向量维度一致性（独立于上述 5 项，确保 embedding 不可用时仍尝试执行）
    ok, detail, fix = _check_dimension_consistency()
    checks.append({
        "name": "向量维度一致性",
        "ok": ok,
        "detail": detail,
        "fix": fix,
    })

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def _suggest_fix(check: dict) -> str:
    """根据 startup_check 结果生成修复建议（startup_check 本身不含 fix 字段）。"""
    if check["ok"]:
        return ""
    name = check["name"]
    if name == "依赖可导入":
        return "运行: pip install -r requirements.txt"
    if name == "Embedding 服务可用":
        return (
            "启动 Ollama 并拉取模型:\n"
            "  ollama pull qwen3-embedding:0.6b\n"
            "若使用云端 EMBEDDING_PROVIDER=openai，请确认 EMBEDDING_API_KEY 已设置"
        )
    return ""


# ---- 人类可读渲染 ----

def render_text(result: dict) -> str:
    """将 doctor 结果渲染为终端友好的人类可读文本。

    参考 scripts/reindex.py cmd_check 的打印风格。
    """
    lines = []
    lines.append("=" * 60)
    lines.append("  Palimpsest doctor 部署体检")
    lines.append("=" * 60)
    lines.append("")

    for c in result["checks"]:
        icon = "\u2705" if c["ok"] else "\u274c"
        lines.append(f"  {icon}  {c['name']}")
        if c["detail"]:
            lines.append(f"       {c['detail']}")
        if c.get("fix"):
            for fix_line in c["fix"].splitlines():
                lines.append(f"       {fix_line}")
        lines.append("")

    if result["ok"]:
        lines.append("  \u2705 体检通过，部署配置正常")
    else:
        failed = [c["name"] for c in result["checks"] if not c["ok"]]
        lines.append(f"  \u274c 体检未通过: {', '.join(failed)}")
    lines.append("")

    return "\n".join(lines)
