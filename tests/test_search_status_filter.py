"""检索候选层状态过滤（core.trivium_store._drop_outdated_candidates）。

背景：``status`` 之前只把 ``outdated`` 从候选里剔除，``archived``（显式退役，
例如按策略停用的历史节点）照样出现在检索结果里——字段写了却不生效。
本文件把这个契约钉住：outdated / archived 一律不进候选，缺省 status 视为 active。
"""

from __future__ import annotations

from core.trivium_store import _drop_outdated_candidates


def _node(nid: int, status: str | None = None) -> tuple[float, dict]:
    payload: dict = {"content": f"node-{nid}"}
    if status is not None:
        payload["status"] = status
    return (0.5, {"id": nid, "payload": payload})


def test_archived_candidates_are_dropped():
    scored = [_node(1, "active"), _node(2, "archived"), _node(3)]
    kept = [n["id"] for _, n in _drop_outdated_candidates(scored)]
    assert kept == [1, 3], "archived 节点必须被剔除，active 与缺省 status 保留"


def test_outdated_candidates_are_dropped():
    scored = [_node(1, "outdated"), _node(2, "active")]
    kept = [n["id"] for _, n in _drop_outdated_candidates(scored)]
    assert kept == [2], "既有契约：outdated 仍被剔除"


def test_missing_status_is_treated_as_active():
    scored = [_node(1), _node(2)]
    kept = [n["id"] for _, n in _drop_outdated_candidates(scored)]
    assert kept == [1, 2], "老节点没有 status 字段，必须视为 active 保留"


def test_empty_payload_is_kept():
    """payload 缺失/异常时不能抛异常，也不该误删。"""
    scored = [(0.5, {"id": 9, "payload": {}}), (0.5, {"id": 10})]
    kept = [n["id"] for _, n in _drop_outdated_candidates(scored)]
    assert kept == [9, 10]
