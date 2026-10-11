"""build_structural_edges —— 结构化建边器 apply/rollback 测试
==============================================================
覆盖 scripts/build_structural_edges.py 的读写路径：

  - ``_set_db`` 真正切换库（回归：``Config.DB_PATH`` 是 import 时求值的类属性，
    import 之后再设 ``os.environ["DB_PATH"]`` 无效——必须显式改 Config.DB_PATH）；
  - ``--apply`` 按显式结构字段建边，``--rollback`` 按批次清单精确撤销，二者幂等；
  - 回滚后图与基线逐字节一致（无残留、无多删）。

隔离保证：本文件用独立临时库（自建 TriviumStore + 自指 DB_PATH），不触碰
conftest 的会话共享临时库。建边器里的技能/知识库目录解析走 ``--skills-dir`` /
``--vault-dir`` 指到空目录，避免依赖本机真实 vault。
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


def _load_module():
    """按文件路径加载建边器脚本（scripts/ 不是常规 import 入口）。"""
    path = _REPO_ROOT / "scripts" / "build_structural_edges.py"
    spec = importlib.util.spec_from_file_location("build_structural_edges", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def iso_db():
    """一个全新临时库的路径；退出时清理。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_edges_iso_")
    db_path = os.path.join(tmp, "iso.db")
    try:
        yield db_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _open_store(db_path: str) -> TriviumStore:
    Config.DB_PATH = db_path
    return TriviumStore()


def _mk_doc(store: TriviumStore, source_path: str, n_chunks: int, domain: str = "hermes"):
    """在某 source_path 下建 n_chunks 个节点（带 chunk_index），返回 id 列表。"""
    ids = []
    for i in range(n_chunks):
        nid = store.insert_node(
            {
                "type": "kb_chunk",
                "content": f"{source_path} chunk {i}",
                "domain": domain,
                "source_path": source_path,
                "chunk_index": i,
                "importance": 0.5,
            },
            store.embed_text(f"{source_path} {i}"),
        )
        ids.append(nid)
    return ids


def test_set_db_switches_config_not_just_env(iso_db):
    """回归 Bug 1：``_set_db`` 必须改写 Config.DB_PATH，否则 TriviumStore 仍连旧库。"""
    mod = _load_module()
    old = Config.DB_PATH
    try:
        target = os.path.abspath(iso_db)
        resolved = mod._set_db(iso_db)
        assert resolved == target
        assert target == Config.DB_PATH, "Config.DB_PATH 未被改写——import 时的类属性仍在生效"
        assert os.environ["DB_PATH"] == target
    finally:
        Config.DB_PATH = old


def test_apply_creates_edges_and_rollback_restores(iso_db):
    """apply 按结构字段建边；rollback 精确撤销，图回到基线。"""
    mod = _load_module()
    old = Config.DB_PATH
    try:
        store = _open_store(iso_db)
        # 两个文档，各 3 chunk：源 1（PART_OF 聚合）+ 源 2（NEXT 顺序）应有候选边
        _mk_doc(store, "doc_a.md", 3)
        _mk_doc(store, "doc_b.md", 3)
        out_of_doc = store.insert_node(
            {"type": "memory", "content": "孤立记忆", "domain": "hermes", "importance": 0.5},
            store.embed_text("孤立记忆"),
        )
        with contextlib.suppress(Exception):
            store._acquire().close()

        def _snapshot():
            s = _open_store(iso_db)
            with s._acquire() as db:
                ids = list(db.all_node_ids())
                edges = set()
                for nid in ids:
                    for e in db.get_edges(nid) or []:
                        edges.add((int(nid), int(e.target_id), e.label))
                # 孤立节点：既无出边也无入边
                touched = set()
                for a, b, _lab in edges:
                    touched.add(a)
                    touched.add(b)
                isolated = [n for n in ids if n not in touched]
                return edges, isolated

        base_edges, base_isolated = _snapshot()
        assert out_of_doc in base_isolated, "新库中该节点应无任何边"

        # 跑 main()（apply），把库指到临时库、目录指到空目录
        # 用唯一 batch_id：避免并发/顺序交错时按 batches[-1] 取到别的测试的清单（曾 flaky）
        empty_dir = tempfile.mkdtemp(prefix="palimpsest_edges_empty_")
        batch_id = f"pytest-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        argv = [
            "build_structural_edges.py",
            "--db",
            iso_db,
            "--apply",
            "--batch-id",
            batch_id,
            "--skills-dir",
            empty_dir,
            "--vault-dir",
            empty_dir,
        ]
        old_argv = sys.argv
        sys.argv = argv
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv
        assert rc == 0

        applied_edges, applied_isolated = _snapshot()
        assert len(applied_edges) > len(base_edges), "apply 后应新增出边"
        # 有 source_path 的 chunk 应被 PART_OF/NEXT 连上 → 不再孤立
        assert len(applied_isolated) < len(base_isolated), "文档 chunk 未被建边连接"
        # 无 source_path 的节点不参与结构建边 → 始终孤立（脚本正确行为，不是漏建）
        assert out_of_doc in applied_isolated

        # 用自己那唯一条清单执行 rollback
        manifest = _REPO_ROOT / "data" / "edge_batches" / f"{batch_id}.json"
        assert manifest.exists(), "apply 未写出批次清单"

        sys.argv = ["build_structural_edges.py", "--db", iso_db, "--rollback", batch_id]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv
        assert rc == 0

        restored_edges, restored_isolated = _snapshot()
        assert restored_edges == base_edges, "回滚未精确还原边集（有多删或漏删）"
        assert set(restored_isolated) == set(base_isolated)
    finally:
        Config.DB_PATH = old
        # 只清理本测试这一次的批次清单（不要 glob 删光——那会误删生产库/其他测试的清单）
        with contextlib.suppress(OSError):
            (_REPO_ROOT / "data" / "edge_batches" / f"{batch_id}.json").unlink()
        shutil.rmtree(empty_dir, ignore_errors=True)


def test_apply_is_idempotent(iso_db):
    """同一库连续 apply 两次：第二次全部跳过（(src,dst,label) 幂等，不产生重复边）。"""
    mod = _load_module()
    old = Config.DB_PATH
    empty_dir = tempfile.mkdtemp(prefix="palimpsest_edges_empty_")
    old_argv = sys.argv
    b1 = f"pytest-idem1-{uuid.uuid4().hex[:8]}"
    b2 = f"pytest-idem2-{uuid.uuid4().hex[:8]}"
    try:
        store = _open_store(iso_db)
        _mk_doc(store, "doc_c.md", 3)
        with contextlib.suppress(Exception):
            store._acquire().close()

        def _edge_count():
            s = _open_store(iso_db)
            with s._acquire() as db:
                total = 0
                for nid in db.all_node_ids():
                    total += len(db.get_edges(nid) or [])
                return total

        base = ["x", "--db", iso_db, "--apply", "--skills-dir", empty_dir, "--vault-dir", empty_dir]
        sys.argv = [*base, "--batch-id", b1]
        assert mod.main() == 0
        first = _edge_count()
        sys.argv = [*base, "--batch-id", b2]
        assert mod.main() == 0
        second = _edge_count()
        assert first == second, "重复 apply 产生了重复边（幂等性被破坏）"
    finally:
        sys.argv = old_argv
        Config.DB_PATH = old
        for bid in (b1, b2):
            with contextlib.suppress(OSError):
                (_REPO_ROOT / "data" / "edge_batches" / f"{bid}.json").unlink()
        shutil.rmtree(empty_dir, ignore_errors=True)


def test_session_chain_and_mentions_sources(iso_db):
    """源 5b 会话链（FOLLOWS）+ 源 6 名称提及（MENTIONS）能真建边。"""
    mod = _load_module()
    old = Config.DB_PATH
    empty_dir = tempfile.mkdtemp(prefix="palimpsest_edges_empty_")
    old_argv = sys.argv
    batch_id = f"pytest-m2-{uuid.uuid4().hex[:8]}"
    try:
        store = _open_store(iso_db)
        # 一个技能节点，供 MENTIONS 命中
        store.insert_node(
            {
                "type": "skill_chunk",
                "name": "opencode",
                "content": "skill opencode body",
                "domain": "hermes",
                "importance": 0.5,
            },
            store.embed_text("skill opencode"),
        )
        # 3 个 record 节点：created_at 相邻 100s（< 1800）→ 期望 2 条 FOLLOWS；
        # 其中一条内容含「opencode」→ 期望 1 条 MENTIONS。
        # 注意：insert_node 的 payload 里 created_at 默认 None（且 extra_fields 不会覆盖已存在键），
        # 所以必须插入后显式 update_payload 补时间戳（见 tests/test_mem_stats.py 的既有做法）。
        base_ts = 1_800_000_000.0
        for i, content in enumerate(
            [
                "第一条 普通记录",
                "第二条 提到了 opencode 技能",
                "第三条 普通记录",
            ]
        ):
            nid = store.insert_node(
                {"type": "record", "content": content, "domain": "hermes", "importance": 0.5},
                store.embed_text(content),
            )
            store.update_payload(nid, {"created_at": base_ts + i * 100})
        with contextlib.suppress(Exception):
            store._acquire().close()

        def _labels():
            s = _open_store(iso_db)
            with s._acquire() as db:
                labs = []
                for nid in db.all_node_ids():
                    for e in db.get_edges(nid) or []:
                        labs.append(str(getattr(e, "label", "") or ""))
                return labs

        before = _labels()
        assert "FOLLOWS" not in before and "MENTIONS" not in before

        sys.argv = [
            "x",
            "--db",
            iso_db,
            "--apply",
            "--batch-id",
            batch_id,
            "--skills-dir",
            empty_dir,
            "--vault-dir",
            empty_dir,
        ]
        assert mod.main() == 0

        after = _labels()
        assert after.count("FOLLOWS") >= 2, f"会话链未建边: {after}"
        assert after.count("MENTIONS") >= 1, f"名称提及未建边: {after}"
    finally:
        sys.argv = old_argv
        Config.DB_PATH = old
        with contextlib.suppress(OSError):
            (_REPO_ROOT / "data" / "edge_batches" / f"{batch_id}.json").unlink()
        shutil.rmtree(empty_dir, ignore_errors=True)
