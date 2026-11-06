"""REST ``/skill/reindex`` 服务侧技能索引 + CLI 默认走 REST 的契约测试（issue #285）。

真实故障：``scripts/build_skill_index.py`` 直连库需要 read_write 连接，而生产
REST 以独占模式常驻持库——CLI 只能 ``DatabaseBusyError``，索引更新永远排不上队。
修法（建议 A）：服务侧补一条写路径（``POST /skill/reindex``，进程内复用共享
连接跑同一 ``build(store=...)``），CLI 默认 HTTP、显式降级 ``--direct``。

锁定：
  1. REST：返回结构化结果（indexed/skipped/elapsed/total），skill_chunk 行数真实变化
  2. REST：刷新后 ``skill_search`` 能命中本次新入索引的技能内容
  3. REST：技能目录解析不到 → 400（绝不按空目录触发孤儿清理）
  4. CLI：默认经 REST ``/skill/reindex``（按 ``--endpoint`` / ``PALIMPSEST_BASE_URL``），
     经真实 app 实例全链路验证
  5. CLI：服务不可达 / HTTP >= 400 → 退出码 1 + 明确错误，绝不静默成功

隔离：conftest 已把 DB_PATH 指向临时库 + fake embedder；按行数差（本目录
前缀）断言，测完清掉新建节点（共享库卫生，沿用 test_meta_tag_purge 模式）。
"""

from __future__ import annotations

import contextlib

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑契约测试——测完删掉本测试新建的节点。"""
    from mcp_tools import store

    def _ids() -> set:
        try:
            return {nid for nid, _ in store.iter_payloads()}
        except Exception:  # noqa: BLE001 —— 桩失败则不清理（不掩盖真实错误）
            return set()

    before = _ids()
    yield
    for nid in _ids() - before:
        with contextlib.suppress(Exception):  # 清理尽力而为
            store.delete_node(nid)


def _client():
    from main import app

    return TestClient(app)


def _write_skill(root, relative_dir: str, name: str, description: str, body: str):
    """写一个带 frontmatter 的 SKILL.md（结构沿用 tests/test_skill.py）。"""
    path = root / relative_dir / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def _skill_chunk_ids_under(prefix: str) -> set:
    """按 source_path 前缀过滤自建目录产生的 skill_chunk 节点 id。"""
    from mcp_tools import store

    ids = set()
    for nid, payload in store.iter_payloads():
        if payload.get("type") != "skill_chunk":
            continue
        source_path = str(payload.get("source_path") or "").replace("\\", "/")
        if source_path.startswith(prefix.replace("\\", "/")):
            ids.add(nid)
    return ids


UNIQUE_TOKEN = "紫晶舰坞只此一家的锚词"


# ---------------------------------------------------------------------------
# 1. POST /skill/reindex：结构化结果 + 行数真实变化
# ---------------------------------------------------------------------------


def test_skill_reindex_endpoint_indexes_rows(tmp_path):
    """端点返回 indexed 等结构化统计，且本目录的 skill_chunk 行数 0 → 1。"""
    from pathlib import Path

    skills_dir = Path(tmp_path) / "skills"
    _write_skill(skills_dir, "demo", "demo", "技能索引端点演示", str(UNIQUE_TOKEN))

    before = _skill_chunk_ids_under(str(skills_dir))
    assert not before, "前置卫生：进测试时共享库里不应有本目录前缀的节点"

    c = _client()
    resp = c.post("/skill/reindex", json={"skills_dir": str(skills_dir)})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["indexed"] == 1, data
    assert data["skipped"] == 0
    assert data["cleaned"] == 0
    assert data["failed"] == 0
    assert data["mode"] == "incremental"
    assert "elapsed" in data and data["elapsed"] >= 0
    assert "output" in data  # 脚本原样 stdout（单行摘要）随返回带回

    added = _skill_chunk_ids_under(str(skills_dir))
    assert len(added) == 1, f"库中本目录前缀的 skill_chunk 行数没有真实变化：{data}"

    # 幂等可重入：第二次调用走增量跳过（mtime 未变）
    resp2 = c.post("/skill/reindex", json={"skills_dir": str(skills_dir)})
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["indexed"] == 0, data2
    assert data2["skipped"] == 1, data2
    assert data2["failed"] == 0


def test_skill_reindex_then_skill_search_hits_new_skill(tmp_path):
    """刷新后 skill_search 能检索到刚入索引的技能内容（验收口径 2）。"""
    from pathlib import Path

    skills_dir = Path(tmp_path) / "skills"
    _write_skill(
        skills_dir,
        "fresh",
        "fresh",
        f"带专有锚词的技能 {UNIQUE_TOKEN}",
        f"正文同样包含 {UNIQUE_TOKEN}，供向量命中",
    )
    c = _client()
    resp = c.post("/skill/reindex", json={"skills_dir": str(skills_dir)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["indexed"] == 1

    search = c.post("/skill/search", json={"query": UNIQUE_TOKEN, "top_k": 5})
    assert search.status_code == 200
    results = search.json().get("results", [])
    assert results, "skill_search 未命中刚刷新的技能"
    assert any(UNIQUE_TOKEN in (r.get("description", "") + r.get("name", "")) for r in results), results


def test_skill_reindex_missing_skills_dir_returns_400():
    """技能目录解析不到 → 400：绝不按空目录触发孤儿清空。"""
    c = _client()
    resp = c.post("/skill/reindex", json={"skills_dir": "X:/_not_exist_skills_dir_42_"})
    assert resp.status_code == 400, resp.text
    assert "not_exist" in resp.json()["detail"]


def test_skill_reindex_registered_in_root_index():
    """路由注册 + root() 端点索引包含 /skill/reindex（五处齐改之②）。"""
    from main import app

    assert any(getattr(r, "path", None) == "/skill/reindex" for r in app.routes)
    c = _client()
    endpoints = c.get("/").json()["endpoints"]
    assert "/skill/reindex" in endpoints


# ---------------------------------------------------------------------------
# 2. CLI：默认经 REST（--endpoint / PALIMPSEST_BASE_URL）
# ---------------------------------------------------------------------------


def _cli_httpx_post_via_app(monkeypatch):
    """把 httpx.post 指到真实 app（TestClient）——CLI 默认路径的全链路验证。

    记录每次请求的 URL / body，返回真实 Response。
    """
    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        from main import app

        calls.append({"url": url, "json": json, "timeout": timeout})
        return TestClient(app).post("/skill/reindex", json=json)

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_cli_default_goes_through_rest_endpoint(monkeypatch, capsys, tmp_path):
    """CLI 默认模式经 REST /skill/reindex（--endpoint 显式指向测试 app），返回统计。"""
    from pathlib import Path

    from scripts.build_skill_index import main as skill_index_main

    skills_dir = Path(tmp_path) / "skills"
    _write_skill(skills_dir, "cli_key", "cli_key", "CLI 经 REST 构建技能", "内容正文")
    calls = _cli_httpx_post_via_app(monkeypatch)

    skill_index_main(["--endpoint", "http://127.0.0.1:8090", "--skills-dir", str(skills_dir)])

    assert len(calls) == 1, calls
    assert calls[0]["url"].endswith("/skill/reindex"), calls
    assert calls[0]["json"]["full"] is False
    assert calls[0]["json"]["skills_dir"] == str(skills_dir), calls[0]["json"]

    out = capsys.readouterr()
    assert "经 REST 服务端完成" in out.out
    assert "建边" in out.out  # HTTP 模式不在本地建边（直接开库会撞 REST 独占锁），须有说明


def test_cli_uses_palimpsest_base_url_env(monkeypatch, capsys, tmp_path):
    """缺省 --endpoint 时按 PALIMPSEST_BASE_URL 环境变量覆盖基地址（尾斜杠归一）。"""
    from pathlib import Path

    from scripts.build_skill_index import main as skill_index_main

    skills_dir = Path(tmp_path) / "skills"
    _write_skill(skills_dir, "env_key", "env_key", "环境变量基地址技能", "内容正文")
    calls = _cli_httpx_post_via_app(monkeypatch)
    monkeypatch.setenv("PALIMPSEST_BASE_URL", "http://127.0.0.1:8090/")

    skill_index_main(["--skills-dir", str(skills_dir)])

    assert calls, "未发起任何 HTTP 请求"
    assert calls[0]["url"].startswith("http://127.0.0.1:8090"), calls
    assert calls[0]["url"].rstrip("/").endswith("/skill/reindex")


def test_cli_unreachable_service_exits_1_with_clear_error(monkeypatch, capsys, tmp_path):
    """服务不可达 → 退出码 1 + 明确错误（绝不静默按成功继续）。"""

    def boom(url, json=None, timeout=None):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(httpx, "post", boom)
    from scripts.build_skill_index import main as skill_index_main

    with pytest.raises(SystemExit) as excinfo:
        skill_index_main(["--skills-dir", str(tmp_path)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "无法连接 REST 服务" in err
    assert "--direct" in err  # 明确指路：显式降级路径存在


def test_cli_http_error_propagates_nonzero(monkeypatch, capsys, tmp_path):
    """REST 返回 >= 400 → 退出码 1 并带回服务端 detail（目录不存在场景）。"""
    from pathlib import Path

    def fake_post(url, json=None, timeout=None):
        from main import app

        return TestClient(app).post("/skill/reindex", json=json)

    monkeypatch.setattr(httpx, "post", fake_post)
    from scripts.build_skill_index import main as skill_index_main

    with pytest.raises(SystemExit) as excinfo:
        skill_index_main(["--skills-dir", str(Path(tmp_path) / "_never_exists_")])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "REST 返回 400" in err
