"""RP 专用实例开关（config.RP_MODE / RP_DOMAIN）行为契约。

背景（酒馆 / SillyTavern 记忆桥）：需要把酒馆的情景记忆跑在一个独立实例上，
库内只有 RP 域数据，避免与主库（技术记忆）互相污染。``RP_MODE=1`` 时行为
变化只有两条：

  1. 域锁死：所有带 domain 参数（或不带）的入口域一律解析为 RP_DOMAIN——
     不传自动补、传别的 domain → 400（解析集中在一个 helper，
     main._resolve_domain，各端点统一调用）；
  2. ingest 不再挂 kb_chunk 知识关联（``payload.linked_from`` 保持空、不建
     REFERENCES 图边——RP 独立库里没有 kb_chunk，挂边纯属浪费）。

``RP_MODE`` 未设置时：两条都不生效（本测试的回归用例显式锁定）。

实现方式：config 值在**调用时**读取（不在 import 期固化），因此用
``monkeypatch`` 直接改 Config 类属性即能让 TestClient 走真实请求路径生效，
无需重建应用（任务书「重测要能真跑」的最小满足形态：端到端 HTTP + 真实
Provider 路径， 不是只调 helper 的单测）。
"""

import contextlib

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


def _ingest(client, content, domain="unset", meta="unset", type_="memory"):
    body = {"content": content, "type": type_}
    if domain != "unset":
        body["domain"] = domain
    if meta != "unset":
        body["meta"] = meta
    resp = client.post("/mem/ingest", json=body)
    return resp


@pytest.fixture
def rp_mode_on():
    """开启 RP 专用实例（RP_DOMAIN 默认 rp），测完还原——class 属性替换的爆炸
    半径是整个进程，必须 undo，否则漏到后续普通行为用例。"""
    from config import Config

    mp = pytest.MonkeyPatch()
    mp.setattr(Config, "RP_MODE", True)
    mp.setattr(Config, "RP_DOMAIN", "rp")
    yield Config
    mp.undo()


# ---------------------------------------------------------------------------
# 域锁死：不传自动补 / 传别的 400
# ---------------------------------------------------------------------------


def test_rp_mode_ingest_without_domain_falls_into_rp_domain(rp_mode_on):
    c = _client()
    resp = _ingest(c, "RP 实例不传 domain 自动落 rp 域", meta={"chat_id": "rp-auto"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stored"], data
    from mcp_tools import store

    payload = store.get_node(data["node_id"])["payload"]
    assert payload["domain"] == "rp"

    # purge / retag 在 RP 模式下同样不传 domain 自动补
    resp = c.post(
        "/mem/retag-by-meta",
        json={"meta_key": "chat_id", "old_value": "rp-auto", "new_value": "rp-auto2"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["matched_count"] >= 1


def test_rp_mode_rejects_foreign_domain(rp_mode_on):
    c = _client()
    resp = _ingest(c, "RP 实例传入别的 domain 拒绝写入", domain="hermes")
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "RP 专用实例" in str(detail)
    resp = c.post("/mem/search", json={"query": "随便查什么", "domain": "hermes"})
    assert resp.status_code == 400, resp.text


def test_rp_mode_honors_custom_rp_domain(rp_mode_on):
    """RP_DOMAIN 可经环境变量调整：锁定的域跟着变（RP_DOMAIN=custom）。"""
    from config import Config

    Config.RP_DOMAIN = "tavern-custom"
    try:
        c = _client()
        resp = _ingest(c, "自定义 RP 域", domain="rp")  # rp 不再是锁定域 → 400
        assert resp.status_code == 400, resp.text
        resp = _ingest(c, "自定义 RP 域 自动补", domain="")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["stored"], data
        from mcp_tools import store

        assert store.get_node(data["node_id"])["payload"]["domain"] == "tavern-custom"
    finally:
        Config.RP_DOMAIN = "rp"


def test_rp_mode_session_end_ingest_lands_in_rp_domain(rp_mode_on):
    """lifecycle/session-end 没有 domain 字段：RP 模式下落库域也应锁 rp。"""
    c = _client()
    resp = c.post(
        "/lifecycle/session-end",
        json={"messages": [{"role": "user", "content": "这轮我们聊了主线剧情决胜点"}]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 宿主不传 auto_ingest 默认 True 但由策略判定 store；只有真正落库时才断言域
    if data.get("stored") and data.get("node_id"):
        from mcp_tools import store

        assert store.get_node(data["node_id"])["payload"]["domain"] == "rp"
    else:
        pytest.skip("策略判定本轮无沉淀（不落库），域锁死无从断言")


# ---------------------------------------------------------------------------
# kb 关联边关闭：ingest 不再挂 linked_from / REFERENCES 边
# ---------------------------------------------------------------------------


def _kb_linking_probe(c, rp_on: bool):
    """造一条 kb_chunk + 一条与其内容一致的 memory，返回 (nid, kb_id, store)。

    kb_chunk 分支总在 RP_MODE 关闭时写入（探针自身的 kb 文案属于「索引切片」，
    用 docs 域；RP 模式域锁死只对 memory 探针生效），随后按 rp_on 决定 memory
    探针的挂边开关，测完把 Config.RP_MODE 还原为 False 不漏状态。
    """
    from config import Config

    Config.RP_MODE = False
    try:
        kb_content = "知识关联检测探针：独占内容块用于挂边验证"
        resp = c.post("/mem/ingest", json={"content": kb_content, "type": "kb_chunk", "domain": "docs"})
        assert resp.status_code == 200 and resp.json()["stored"], resp.text
        kb_id = resp.json()["node_id"]

        Config.RP_MODE = rp_on
        resp = c.post(
            "/mem/ingest",
            json={"content": kb_content, "type": "memory"},  # RP 模式下不传 domain 自动补
        )
        assert resp.status_code == 200 and resp.json()["stored"], resp.text
        nid = resp.json()["node_id"]

        from mcp_tools import store

        return nid, kb_id, store
    finally:
        Config.RP_MODE = False


def test_rp_mode_ingest_skips_kb_links():
    c = _client()
    nid, _kb_id, store = _kb_linking_probe(c, rp_on=True)
    edges = store.get_edges(nid)
    assert not any(getattr(e, "label", "") == "REFERENCES" for e in edges), edges
    assert store.get_node(nid)["payload"].get("linked_from", []) == []


def test_rp_mode_resolve_domain_blank_fills_rp_domain(rp_mode_on):
    """helper 单点：RP 模式下空域补 RP_DOMAIN（session-end 等无 domain 字段的
    写入口也靠这条保证落库域锁死）。"""
    from main import _resolve_domain

    assert _resolve_domain("") == "rp"
    assert _resolve_domain("rp") == "rp"


def test_rp_mode_purge_blank_domain_targets_rp_domain(rp_mode_on):
    c = _client()
    _ingest(c, "RP 实例 purge 空域自动补", meta={"chat_id": "pk-auto"})
    resp = c.post("/mem/purge-by-meta", json={"meta_key": "chat_id", "meta_value": "pk-auto"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["matched_count"] >= 1


def test_rp_mode_off_keeps_kb_links():
    """回归对照：RP_MODE 关闭时同构场景确实建 REFERENCES 边（开关真有开关作用）。"""
    c = _client()
    nid, kb_id, store = _kb_linking_probe(c, rp_on=False)
    edge_pairs = [(e.get("target_id") if isinstance(e, dict) else e.target_id) for e in store.get_edges(nid)]
    assert kb_id in edge_pairs, store.get_edges(nid)
    assert kb_id in store.get_node(nid)["payload"]["linked_from"]


# ---------------------------------------------------------------------------
# 回归：RP_MODE 未设置时行为 100% 不变
# ---------------------------------------------------------------------------


def test_rp_mode_unset_keeps_old_behavior():
    c = _client()
    resp = _ingest(c, "回归：非 RP 模式显式 hermes 域照常写入", domain="hermes")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stored"], data
    from mcp_tools import store

    assert store.get_node(data["node_id"])["payload"]["domain"] == "hermes"

    # helper 在 RP_MODE 关闭时是恒等函数（不归一、不校验）
    from main import _resolve_domain

    assert _resolve_domain("novel") == "novel"
    assert _resolve_domain("hermes") == "hermes"
    assert _resolve_domain("") == ""
