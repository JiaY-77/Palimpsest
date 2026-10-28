"""常驻域注入（issue #176）：某 domain 的 active 节点**每轮无条件**注入。

覆盖两层：

1. `core/pin.py` 纯函数（不碰库）：筛选 / 排序 / 节点边界截断 / 预算与诊断。
2. `/lifecycle/pre-turn` 端点：零语义重叠也注入、置顶于语义召回之前、与召回去重、
   默认关闭、超预算如实报告。
"""

from __future__ import annotations

import json

import pytest

from core.pin import (
    PIN_PER_NODE_MAX_CHARS,
    compose_pin_block,
    pin_diagnostics,
    select_pin_nodes,
)

# ---------------------------------------------------------------------------
# 1. select_pin_nodes
# ---------------------------------------------------------------------------


def _node(nid: int, content: str = "规则", domain: str = "pin", importance: float = 0.5, status=None):
    payload = {"content": content, "domain": domain, "importance": importance}
    if status is not None:
        payload["status"] = status
    return (nid, payload)


class TestSelectPinNodes:
    def test_filters_by_domain(self):
        entries = [_node(1), _node(2, domain="other")]
        assert [nid for nid, _ in select_pin_nodes(entries, domain="pin")] == [1]

    def test_skips_outdated_but_keeps_missing_status(self):
        """outdated（被新版取代）不注入；缺省 status 的老节点视为 active。"""
        entries = [_node(1, status="outdated"), _node(2, status="active"), _node(3)]
        assert sorted(nid for nid, _ in select_pin_nodes(entries, domain="pin")) == [2, 3]

    def test_orders_by_importance_desc_then_id_desc(self):
        entries = [_node(1, importance=0.5), _node(2, importance=0.9), _node(3, importance=0.5)]
        assert [nid for nid, _ in select_pin_nodes(entries, domain="pin")] == [2, 3, 1]

    def test_skips_blank_content_and_non_dict_payload(self):
        entries = [_node(1, content="   "), (2, "not a dict")]
        assert select_pin_nodes(entries, domain="pin") == []


# ---------------------------------------------------------------------------
# 2. compose_pin_block
# ---------------------------------------------------------------------------


class TestComposePinBlock:
    def test_no_nodes_means_no_text(self):
        out = compose_pin_block([], domain="pin")
        assert out["text"] == ""
        assert out["node_count"] == 0 and out["dropped"] == 0

    def test_header_and_one_line_per_node(self):
        out = compose_pin_block([_node(1, "规则A"), _node(2, "规则B")], domain="pin")
        assert out["text"].splitlines()[0] == "[Palimpsest 常驻规则 · domain=pin]"
        assert "- 规则A" in out["text"] and "- 规则B" in out["text"]
        assert out["node_count"] == 2
        # compose 保持**传入顺序**；排序是 select_pin_nodes 的职责（见上一个 class）
        assert out["ids"] == [1, 2]

    def test_truncates_at_node_boundary_never_mid_node(self):
        """核心合同：预算不够就整条不放，绝不切半个节点。"""
        nodes = [_node(i, "长" * 100) for i in range(1, 6)]
        out = compose_pin_block(nodes, domain="pin", budget_chars=250)
        assert out["chars"] <= 250
        assert out["truncated"] is True and out["dropped"] > 0
        bodies = [ln for ln in out["text"].splitlines()[1:] if ln.startswith("- ")]
        assert bodies, "至少要放进一条"
        for line in bodies:
            assert line == "- " + "长" * 100, f"出现了被截断的节点: {line[:20]}…"

    def test_per_node_cap(self):
        out = compose_pin_block([_node(1, "长" * (PIN_PER_NODE_MAX_CHARS + 50))], domain="pin")
        assert "长" * PIN_PER_NODE_MAX_CHARS in out["text"]
        assert "长" * (PIN_PER_NODE_MAX_CHARS + 1) not in out["text"]

    def test_budget_smaller_than_header_injects_nothing(self):
        out = compose_pin_block([_node(1, "规则")], domain="pin", budget_chars=5)
        assert out["text"] == ""
        assert out["truncated"] is True and out["dropped"] == 1

    def test_overflow_is_reported_in_text(self):
        nodes = [_node(i, "长" * 100) for i in range(1, 5)]
        out = compose_pin_block(nodes, domain="pin", budget_chars=300)
        assert out["truncated"] is True
        assert "未注入" in out["text"]

    def test_diagnostics_carry_no_body(self):
        out = compose_pin_block([_node(1, "规则A")], domain="pin")
        assert set(pin_diagnostics(out)) == {"node_count", "chars", "truncated", "dropped"}


# ---------------------------------------------------------------------------
# 3. /lifecycle/pre-turn 端点
# ---------------------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


def _fake_pin_entries(monkeypatch, *contents: str, importance: float = 0.9, domain: str = "pintest"):
    """把常驻域的取数换成内存构造，端点其余逻辑（筛选 / 拼装 / 注入 / 诊断）照常跑。

    不往临时库写节点：同一 pytest session 内临时库共享，写入会污染其它模块的
    计数断言（实测曾把 test_smoke 的 consolidate_dryrun 打红）。
    """
    import main as main_mod

    entries = [
        (1000 + i, {"content": c, "domain": domain, "importance": importance, "status": "active"})
        for i, c in enumerate(contents)
    ]
    monkeypatch.setattr(main_mod, "_pin_entries", lambda: entries)
    return entries


def test_pin_disabled_by_default(db_path):
    """PIN_DOMAIN 为空（默认）→ 不注入，pin 诊断全零，行为与改动前一致。"""
    from config import Config

    assert (getattr(Config, "PIN_DOMAIN", "") or "") == ""
    body = _client().post("/lifecycle/pre-turn", json={"user_message": "帮我查一下部署流程"}).json()
    assert body["pin"]["node_count"] == 0
    assert body["pin"]["chars"] == 0
    assert "常驻规则" not in body["inject_text"]


def test_pin_arrives_even_with_zero_overlap(db_path, monkeypatch):
    """**核心验收**：与当前消息零语义重叠的常驻规则也必须每轮到达。"""
    from config import Config

    _fake_pin_entries(monkeypatch, "常驻规则：任何删除操作前必须先与主人确认")
    monkeypatch.setattr(Config, "PIN_DOMAIN", "pintest", raising=False)

    body = _client().post("/lifecycle/pre-turn", json={"user_message": "今天天气怎么样呀"}).json()
    assert body["pin"]["node_count"] == 1
    assert "任何删除操作前必须先与主人确认" in body["inject_text"]


def test_pin_arrives_on_trivial_turn(db_path, monkeypatch):
    """「无条件」= trivial 轮也注入（召回跳过，但常驻块仍在）。"""
    from config import Config

    _fake_pin_entries(monkeypatch, "常驻规则：回答用中文")
    monkeypatch.setattr(Config, "PIN_DOMAIN", "pintest", raising=False)

    body = _client().post("/lifecycle/pre-turn", json={"user_message": "hi"}).json()
    assert body["skip"] is True and body["skip_reason"] == "trivial"
    assert "回答用中文" in body["inject_text"]


def test_pin_placed_before_recall_block(db_path, monkeypatch):
    """常驻块排在语义召回块之前（issue 的 explicit 要求）。"""
    import main as main_mod
    from config import Config

    _fake_pin_entries(monkeypatch, "常驻规则：先停服务再重嵌")
    monkeypatch.setattr(Config, "PIN_DOMAIN", "pintest", raising=False)
    # 端点用 _as_json(...) 解析返回值，所以这里必须回 JSON 字符串（不是 dict）
    monkeypatch.setattr(
        main_mod,
        "_mcp_mem_search",
        lambda *a, **k: json.dumps(
            {"results": [{"id": 2001, "score": 0.8, "summary": "部署流程的记录：先跑体检脚本"}]}
        ),
    )

    body = _client().post("/lifecycle/pre-turn", json={"user_message": "部署流程的记录"}).json()
    text = body["inject_text"]
    assert "先停服务再重嵌" in text and "先跑体检脚本" in text
    assert text.index("先停服务再重嵌") < text.index("先跑体检脚本")


def test_pinned_node_not_injected_twice(db_path, monkeypatch):
    """常驻节点同时被语义命中 → 只保留常驻那份（去重省 token）。"""
    import main as main_mod
    from config import Config

    marker = "去重专用规则：任何删除操作前先确认"
    entries = _fake_pin_entries(monkeypatch, marker)
    pin_id = entries[0][0]
    monkeypatch.setattr(Config, "PIN_DOMAIN", "pintest", raising=False)
    # 让语义召回也命中同一条常驻节点（fake embedder 下无法稳定复现重叠）
    monkeypatch.setattr(
        main_mod,
        "_mcp_mem_search",
        lambda *a, **k: {"results": [{"id": pin_id, "score": 0.9, "summary": marker}]},
    )

    body = _client().post("/lifecycle/pre-turn", json={"user_message": "重嵌怎么做"}).json()
    assert body["inject_text"].count(marker) == 1


def test_over_budget_reported_not_silent(db_path, monkeypatch):
    """超预算 → pin.truncated / dropped 如实报告。"""
    from config import Config

    _fake_pin_entries(monkeypatch, *[f"规则{i}：" + "长" * 500 for i in range(5)])
    monkeypatch.setattr(Config, "PIN_DOMAIN", "pintest", raising=False)
    monkeypatch.setattr(Config, "PIN_BUDGET_CHARS", 1200, raising=False)

    body = _client().post("/lifecycle/pre-turn", json={"user_message": "查一下规则"}).json()
    pin = body["pin"]
    assert pin["truncated"] is True and pin["dropped"] > 0
    assert pin["chars"] <= 1200


def test_fail_open_when_store_raises(db_path, monkeypatch):
    """常驻域查询异常 → 整轮照常返回（fail-open），只是没有常驻块。"""
    import main as main_mod
    from config import Config

    monkeypatch.setattr(Config, "PIN_DOMAIN", "pintest", raising=False)

    def boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(main_mod, "_pin_entries", boom)
    r = _client().post("/lifecycle/pre-turn", json={"user_message": "帮我查一下部署流程"})
    assert r.status_code == 200
    assert r.json()["pin"]["node_count"] == 0


@pytest.mark.parametrize("msg", ["hi", "帮我查一下部署流程"])
def test_pin_never_breaks_response_shape(db_path, monkeypatch, msg):
    """无论是否注入，响应里 pin 诊断字段始终存在且形状稳定。"""
    from config import Config

    _fake_pin_entries(monkeypatch, "常驻规则：形状稳定")
    monkeypatch.setattr(Config, "PIN_DOMAIN", "pintest", raising=False)
    body = _client().post("/lifecycle/pre-turn", json={"user_message": msg}).json()
    assert set(body["pin"]) == {"node_count", "chars", "truncated", "dropped"}
