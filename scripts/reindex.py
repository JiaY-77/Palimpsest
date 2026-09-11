"""
全库向量重嵌入脚本 —— 换 embedding 模型后一键重建所有节点的向量。

用法：
  python scripts/reindex.py --check                       # 体检（只读，不写数据）
  python scripts/reindex.py --dry-run                     # 只报告将要重嵌哪些节点
  python scripts/reindex.py --only memory,record          # 只重嵌指定 payload.type
  python scripts/reindex.py --skip kb_chunk,novel_chunk   # 跳过指定 payload.type
  python scripts/reindex.py --batch 64 --yes              # 批量打印进度、免二次确认
  python scripts/reindex.py --resume                      # 断点续跑（默认）
  python scripts/reindex.py --restart                     # 忽略进度，从头重嵌

设计约束：
  - 只动向量，不碰 payload / 边 / 节点增删
  - 维度不一致时一字节都不写（退出码 2）
  - 库被占用时明确报错（退出码 3）
  - embedding 服务不可用时明确报错（退出码 4）
  - 异常中断保留进度，可 --resume 续跑（状态文件跟库绑定）
"""
import argparse
import json
import os
import signal
import sys
import time

try:
    from ._common import PROJECT_ROOT
except ImportError:
    pass

from core.trivium_store import EmbeddingUnavailableError, TriviumStore

# ---- 进度状态文件（跟库绑定，不再硬编码到项目 data/） ----
PROBE_TEXT = "维度校验探针文本 reindex probe"


def db_state_file(store):
    """计算当前库对应的状态文件路径：<db_dir>/reindex_state_<db_filename>.json"""
    db_dir = os.path.dirname(store.db_path) or "."
    db_name = os.path.basename(store.db_path)
    return os.path.join(db_dir, f"reindex_state_{db_name}.json")


def _load_state(state_path):
    """读取断点续跑状态（JSON）。"""
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_state(state_path, state):
    """写入断点续跑状态。"""
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path)


def _get_db_dim(store):
    """读取库实际维度（从 DB 的 storage_info）。

    返回 (dim: int, error: str | None)。
    """
    import triviumdb
    db = None
    try:
        db = triviumdb.TriviumDB(store.db_path, dim=store.dim)
        info = db.storage_info()
        return info.get("dim"), None
    except Exception as e:
        return None, f"无法读取数据库维度: {e}"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _check_dims(store):
    """维度校验：实测维度 vs **库的实际维度**。

    返回 (ok: bool, actual_dim: int | None, msg: str)。
    不写任何数据。
    """
    # 1) 实测维度（probe embedding）
    try:
        vec = store.embed_text(PROBE_TEXT)
        actual = len(vec)
    except EmbeddingUnavailableError as e:
        return False, None, f"Embedding 服务不可用: {e}"
    except Exception as e:
        return False, None, f"Embedding 服务不可用: {e}"

    # 2) 库实际维度
    db_dim, err = _get_db_dim(store)
    if err:
        return False, None, err

    # 3) 比对：实测 vs 库
    if actual == db_dim:
        return True, actual, f"维度一致: {actual} 维（实测 = 库）"
    return False, actual, (
        f"维度不匹配: 当前 provider 实测 {actual} 维，库实际 {db_dim} 维\n"
        "跨模型向量空间不兼容，混用会互相排斥，无法在现有库上重嵌。\n"
        "必须新建库，请按以下步骤操作：\n"
        f"  1. 导出:  python scripts/export_all_data.py\n"
        f"  2. 重建:  python scripts/rebuild_db.py\n"
        f"  3. 切换:  修改 .env 中 EMBEDDING_DIM / OLLAMA_EMBEDDING_DIM = {actual}\n"
        f"  4. 重索引知识库:  python scripts/build_kb_index.py --full\n"
        f"  5. 如有小说设定:  python scripts/build_novel_index.py --source <vault> --full"
    )


def _text_for_embed(payload):
    """从 payload 提取用于 embedding 的文本（只用 content，与入库一致）。"""
    c = payload.get("content")
    if not isinstance(c, str):
        return ""
    return c


def _vec_summary(vec, n=4):
    """向量前 n 个元素的摘要字符串。"""
    if not vec or len(vec) < n:
        return str(vec)
    return f"[{', '.join(f'{v:.4f}' for v in vec[:n])}, ...] ({len(vec)}d)"


def _apply_filter(payload_type, only_set, skip_set):
    """判断节点类型是否应被重嵌。"""
    if only_set:
        return payload_type in only_set
    if skip_set:
        return payload_type not in skip_set
    return True


# ---- check 模式 ----

def cmd_check(store):
    """体检模式：只读输出 provider / 模型 / 维度 / 节点分布。"""
    print("=" * 60)
    print("  Palimpsest 重嵌入体检  (--check)")
    print("=" * 60)

    provider = store.provider
    if provider == "openai":
        model = getattr(
            __import__("config").Config, "EMBEDDING_MODEL", "unknown")
        base_url = getattr(
            __import__("config").Config, "EMBEDDING_BASE_URL", "")
    else:
        model = getattr(
            __import__("config").Config, "OLLAMA_EMBEDDING_MODEL", "unknown")
        base_url = getattr(
            __import__("config").Config, "OLLAMA_EMBEDDING_BASE_URL", "")

    print(f"\n当前 provider:  {provider}")
    print(f"当前模型:        {model}")
    print(f"配置维度:        {store.dim}")

    # 库实际维度
    db_dim, err = _get_db_dim(store)
    if err:
        print(f"库实际维度:      {err}")
    else:
        print(f"库实际维度:      {db_dim}")

    # 实测维度
    ok, actual, msg = _check_dims(store)
    if actual is not None:
        print(f"实测维度:        {actual}")
    print(f"维度校验:        {msg}")

    if not ok:
        print("\n❌ 维度校验未通过，无法安全重嵌")
        return 2

    # 库内节点类型分布
    print(f"\n数据库路径:  {store.db_path}")
    type_counts = store.count_by_type()
    total = sum(type_counts.values())
    print(f"节点总数:    {total}")
    if type_counts:
        for tp, cnt in sorted(type_counts.items()):
            print(f"  {tp:20s}  {cnt}")

    # 单次 embedding 延迟
    n_probe = 3
    times = []
    for _ in range(n_probe):
        t0 = time.perf_counter()
        try:
            store.embed_text(PROBE_TEXT)
        except Exception:
            break
        times.append((time.perf_counter() - t0) * 1000)
    if times:
        avg = sum(times) / len(times)
        print(f"\n单次 embedding 延迟:  {avg:.1f} ms（{len(times)} 次采样平均）")

    print("\n✅ 体检通过，可安全执行重嵌入")
    print("  推荐顺序: --check → --dry-run → 正式执行")
    return 0


# ---- 重嵌入主流程 ----

def cmd_reindex(store, *, only=None, skip=None, batch=64,
                restart=False, dry_run=False, yes=False):
    """重嵌入库中所有节点的向量。"""
    t0 = time.perf_counter()

    # 1) DB 占用预检（必须在维度校验之前：维度校验要开库，锁库时会被误报成「读不到维度」）
    try:
        db = store._acquire()
        db.close()
    except Exception as e:
        emsg = str(e).lower()
        if any(kw in emsg for kw in ("lock", "busy", "occupied", "concurrent")):
            print(
                f"\n错误: 数据库被占用 —— {e}\n"
                "请先停止占用该库的服务（REST :8090 / MCP），然后重试。",
                file=sys.stderr,
            )
            return 3
        raise

    # 2) 维度红线（4 = embedding 服务不可用；2 = 维度不匹配）
    ok, actual, msg = _check_dims(store)
    print(f"维度校验: {msg}")
    if not ok:
        return 4 if actual is None else 2

    # 3) 收集待处理节点（完全消费 iter_nodes 释放 DB 连接，再逐节点 embed/update）
    only_set = set(only) if only else None
    skip_set = set(skip) if skip else None
    all_nodes = []  # [(nid, payload, old_vector)]
    for nid, node in store.iter_nodes():
        payload = node.get("payload") or {}
        tp = payload.get("type") or "unknown"
        if _apply_filter(tp, only_set, skip_set):
            all_nodes.append((nid, payload, node.get("vector")))

    # 4) 断点续跑状态（状态文件跟库同目录绑定；内容里再记库指纹防同路径换库串用）
    state_file = db_state_file(store)
    db_dim, _db_dim_err = _get_db_dim(store)
    state = _load_state(state_file)
    max_done_id = 0
    done_count = 0
    if not restart and state:
        expected_fp = {
            "provider": store.provider,
            "dim": store.dim,
            "db_dim": db_dim,
        }
        state_fp = {
            "provider": state.get("provider"),
            "dim": state.get("dim"),
            "db_dim": state.get("db_dim"),
        }
        if state_fp == expected_fp:
            max_done_id = state.get("max_done_id", 0)
            done_count = state.get("done_count", 0)
            print(f"断点续跑: 跳过 ID <= {max_done_id}（已完成 {done_count} 个）"
                  f" [状态文件: {state_file}]")
        else:
            print("库指纹（provider / 配置维度 / 库实际维度）不匹配，忽略旧进度从头开始")
            done_count = 0

    if restart:
        done_count = 0
        max_done_id = 0

    # dry-run 只报告不执行
    if dry_run:
        print(f"\n将要重嵌入 {len(all_nodes)} 个节点：")
        types: dict[str, int] = {}
        no_content_types: dict[str, int] = {}
        for _, p, _v in all_nodes:
            tp = p.get("type") or "unknown"
            types[tp] = types.get(tp, 0) + 1
            if not str(p.get("content") or "").strip():
                no_content_types[tp] = no_content_types.get(tp, 0) + 1
        for tp, cnt in sorted(types.items()):
            print(f"  {tp:20s}  {cnt}")
        if no_content_types:
            print("\n其中缺失 content 将被跳过：")
            for tp, cnt in sorted(no_content_types.items()):
                print(f"  {tp:20s}  {cnt}")
        elapsed = time.perf_counter() - t0
        print(f"\n（dry-run 结束，未写任何数据，耗时 {elapsed:.1f}s）")
        return 0

    if not yes and not dry_run:
        confirm = input(f"即将重嵌 {len(all_nodes)} 个节点的向量，确认？[y/N] ")
        if confirm.strip().lower() != "y":
            print("已取消")
            return 0

    # 5) 逐节点重嵌入
    reindexed = 0
    skipped_no_content = 0
    fixed_empty = 0
    failed = 0
    skip_types: dict[str, int] = {}  # 跳过的节点类型统计
    sample_node_id = None
    sample_old_vec = None
    sample_new_vec = None

    progress_interval = max(1, batch)

    def _save_progress(current_id, current_count):
        _save_state(state_file, {
            "provider": store.provider,
            "model": getattr(
                __import__("config").Config,
                "OLLAMA_EMBEDDING_MODEL"
                if store.provider != "openai" else "EMBEDDING_MODEL",
                "unknown",
            ),
            "dim": store.dim,
            "db_dim": db_dim,
            "db_path": os.path.abspath(store.db_path),
            "max_done_id": current_id,
            "done_count": current_count,
            "timestamp": time.time(),
        })

    _interrupted = False

    def _on_sigint(signum, frame):
        nonlocal _interrupted
        _interrupted = True

    old_handler = signal.signal(signal.SIGINT, _on_sigint)

    for nid, payload, old_vec in all_nodes:
        if _interrupted:
            break

        if nid <= max_done_id:
            continue  # resume：跳过已完成节点

        text = _text_for_embed(payload)
        if not text.strip():
            skipped_no_content += 1
            tp = payload.get("type") or "unknown"
            skip_types[tp] = skip_types.get(tp, 0) + 1
            continue

        try:
            new_vec = store.embed_text(text)
        except Exception as e:
            failed += 1
            print(f"\n[失败] ID={nid}: {e}", file=sys.stderr)
            break  # embedding 不可用，快速失败停止

        if len(new_vec) != store.dim:
            failed += 1
            print(
                f"\n[失败] ID={nid}: 向量维度 {len(new_vec)} != 配置 {store.dim}",
                file=sys.stderr,
            )
            break

        # 空向量修复统计：旧向量全零 + 新向量非零 → 计入修复
        if old_vec and not any((v or 0.0) != 0.0 for v in old_vec) \
                and any((v or 0.0) != 0.0 for v in new_vec):
            fixed_empty += 1

        # 保存第一条用于结束报告对比
        if sample_node_id is None:
            sample_node_id = nid
            sample_old_vec = old_vec

        try:
            store.update_vector(nid, new_vec)
        except Exception as e:
            emsg = str(e).lower()
            if any(kw in emsg for kw in ("lock", "busy", "occupied")):
                print(
                    f"\n错误: 数据库写入被拒 —— {e}\n"
                    "请先停止占用该库的服务（REST :8090 / MCP）。",
                    file=sys.stderr,
                )
                signal.signal(signal.SIGINT, old_handler)
                return 3
            failed += 1
            print(f"\n[失败] ID={nid}: {e}", file=sys.stderr)
            break  # 写入异常，停止避免半成品

        reindexed += 1
        sample_new_vec = new_vec

        if reindexed % progress_interval == 0:
            print(f"  进度: {reindexed} / {len(all_nodes)}", flush=True)
            _save_progress(nid, reindexed)

    signal.signal(signal.SIGINT, old_handler)

    # 最终进度
    _save_progress(
        max(nid for nid, _p, _v in all_nodes) if all_nodes and not _interrupted else 0,
        reindexed,
    )

    # 6) 结束报告——四类计数：重嵌 / 跳过（缺 content）/ 修复空向量 / 失败
    elapsed = time.perf_counter() - t0
    print("\n" + "=" * 60)
    print("  重嵌入完成")
    print("=" * 60)
    print(f"  重嵌入:            {reindexed} 个")
    print(f"  跳过（缺 content）: {skipped_no_content} 个")
    if skip_types:
        for tp, cnt in sorted(skip_types.items()):
            print(f"      - {tp}: {cnt}")
    print(f"  修复空向量:        {fixed_empty} 个")
    print(f"  失败:              {failed} 个")
    print(f"  耗时:              {elapsed:.1f} 秒")

    if _interrupted:
        print(
            f"\n⚠ 被中断（Ctrl+C），已完成 {reindexed} 个节点。"
            "下次运行自动从断点续跑。"
        )

    # 抽样对比
    if sample_node_id is not None and sample_old_vec is not None:
        print(f"\n抽样对比（ID={sample_node_id}）:")
        print(f"  重嵌前: {_vec_summary(sample_old_vec)}")
        print(f"  重嵌后: {_vec_summary(sample_new_vec)}")
        if sample_old_vec and sample_new_vec:
            changed = any(
                abs(a - b) > 1e-6
                for a, b in zip(sample_old_vec, sample_new_vec)
            )
            status = "✓ 已变化" if changed else "⚠ 未变化（使用了相同模型？）"
            print(f"  结果:   {status}")

    print(f"\n状态文件: {state_file}")
    if reindexed > 0:
        print("\n建议: 重嵌完成后跑一次检索冒烟验证检索结果")
        print("  python scripts/palimpsest_cli.py search \"测试\" --top-k 3")
        if failed == 0:
            print("\n完成。")
    else:
        print("\n无需重嵌入的节点。")

    # 退出码：4 = embedding 不可用且未写入任何向量（预检/首节点失败）；1 = 中途失败（已写部分）
    return 0 if failed == 0 else (4 if reindexed == 0 and fixed_empty == 0 else 1)


# ---- 入口 ----

def main():
    p = argparse.ArgumentParser(
        prog="reindex",
        description="Palimpsest 全库向量重嵌入（换 embedding 模型后使用）",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true",
        help="体检模式：只读检查 provider / 模型 / 维度 / 节点分布，不写数据",
    )
    mode.add_argument(
        "--dry-run", action="store_true",
        help="试运行：只报告将要重嵌哪些节点，不实际写入",
    )
    mode.add_argument(
        "--restart", action="store_true",
        help="忽略断点进度，从头重嵌所有节点",
    )
    p.add_argument(
        "--resume", action="store_true", default=False,
        help="断点续跑（跳过已完成节点，这也是默认行为）",
    )
    p.add_argument(
        "--only", default="",
        help="只重嵌指定 payload.type（逗号分隔，如 memory,record）",
    )
    p.add_argument(
        "--skip", default="",
        help="跳过指定 payload.type（逗号分隔，如 kb_chunk,novel_chunk）",
    )
    p.add_argument(
        "--batch", type=int, default=64,
        help="每处理多少个节点打印一次进度并保存状态（默认 64）",
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="跳过二次确认提示（脚本 / CI 自动化时使用）",
    )

    args = p.parse_args()

    store = TriviumStore()

    if args.check:
        code = cmd_check(store)
    else:
        only = [s.strip() for s in args.only.split(",") if s.strip()] or None
        skip = [s.strip() for s in args.skip.split(",") if s.strip()] or None
        code = cmd_reindex(
            store, only=only, skip=skip, batch=args.batch,
            restart=args.restart, dry_run=args.dry_run, yes=args.yes,
        )

    sys.exit(code)


if __name__ == "__main__":
    main()
