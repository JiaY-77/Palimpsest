"""契约测试：会话缓冲的轮次序号与兜底（issue #216——缓冲 turns 恒为 1 的成因修复）。

验收闸门（策略判定在 core/strategy.py 纯函数，端点执行在 main.py，本文件钉行为）：

  1. 宿主不发 ``turn_index``（真实形态：Hermes 插件 / 不维护轮次号的宿主）
     → 服务端不再拿缺省 0 当「同一轮」幂等覆盖，而是按 ``last_turn_index + 1``
     追加：连发 3 轮 → 缓冲 ``turns`` 3 项、序号 [1, 2, 3]；
  2. 显式非 0 ``turn_index`` 的幂等语义保留：同序号重复提交 → 原位覆盖
     （turns 长度不变、内容被替换、不追加第二条）；
  3. 有 ``<thought>`` 原文但解析不出编号小节（原生思考 / 骨架没注入的轮）
     → 不再整轮按 ``no_sections`` 丢弃：原文截断进 thought 节，session-end
     合并出的胶囊【思路】节是真实内容而非「（待补）」。

隔离保证：conftest 已把 DB_PATH 指向临时库 + fake embedder，不触碰正式库。
"""

from __future__ import annotations

import contextlib

import pytest

from core.strategy import SESSION_BUFFER_TYPE


@pytest.fixture(autouse=True)
def _no_leak_to_shared_db():
    """共享 session 库里跑集成测试——测完删掉本测试新建的节点。

    与 test_session_capsule 同款隔离模式：conftest 用 session 级临时库，
    所有测试文件共享；残留节点会污染顺序敏感测试。删除节点会级联删边 +
    清 FTS（delete_node 语义），因此这里只需追踪 node_id 集合差集。
    """
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
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


_CODE_BLOCK = (
    "<thought>\n"
    "1. 本轮改什么 / 新增什么（一句话）：给轮次序号加兜底\n"
    "2. 失败路径（哪里会坏、坏了什么表现、怎么兜住）：重复提交不落第二条\n"
    "3. 怎么验（跑什么、看到什么算数）：pytest 全绿\n"
    "4. 交付物（PR / 文件路径 / 节点号）：main.py\n"
    "</thought>"
)

# 原生思考样本：有 <thought> 段但没有任何编号行（骨架没注入时模型的自然写法）。
_FREE_TEXT_THOUGHT = (
    "<thought>\n"
    "先核对 post-turn 端点的缓冲路径，确认它确实没带轮次序号；\n"
    "再给插件补一个每会话的单调计数，随请求上送。\n"
    "</thought>"
)


def _session_nodes() -> list[tuple[int, dict]]:
    """全库的 type=session 缓冲节点。"""
    from mcp_tools import store

    return [
        (nid, payload)
        for nid, payload in store.iter_payloads()
        if isinstance(payload, dict) and payload.get("type") == SESSION_BUFFER_TYPE
    ]


def _buffer_by_session(session_id: str) -> tuple[int, dict] | None:
    nodes = [(nid, pl) for nid, pl in _session_nodes() if pl.get("session_id") == session_id]
    assert len(nodes) <= 1, f"同一 session_id 只允许一条缓冲节点，实际 {len(nodes)} 条"
    return nodes[0] if nodes else None


def test_post_turn_without_turn_index_appends_per_turn(db_path):
    """宿主不发 turn_index（真实形态）连发 3 轮 → turns 3 项、序号 [1, 2, 3]。

    回归背景（issue #216）：旧实现里缺省 turn_index=0 被当「同一轮」幂等覆盖，
    每轮顶掉上一轮，缓冲永远只剩最后一轮（全库实测 turns 恒为 1）。
    """
    client = _client()
    for i in (1, 2, 3):
        r = client.post(
            "/lifecycle/post-turn",
            json={
                "session_id": "buf-turns-1",
                "user_message": f"第 {i} 轮：看看缓冲轮次",
                "assistant_message": f"正文开始\n{_CODE_BLOCK}\n正文结束",
                "skeleton_kind": "code",
                # 刻意不带 turn_index 字段——PostTurnRequest 缺省落 0，
                # 服务端必须自己解析出追加序号，而不是拿 0 幂等覆盖。
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["session_buffer"]["buffered"] is True

    buf = _buffer_by_session("buf-turns-1")
    assert buf is not None
    payload = buf[1]
    assert len(payload["turns"]) == 3
    assert [t["turn_index"] for t in payload["turns"]] == [1, 2, 3]
    assert payload["last_turn_index"] == 3


def test_explicit_same_turn_index_repost_overwrites_in_place(db_path):
    """显式同一 turn_index 重复提交 → 原位覆盖（长度不变、内容被替换）。

    这是防重复提交的幂等语义（重试同一轮不落第二条）；第 1 条的追加兜底
    不能破坏它——所以兜底只对 ``turn_index <= 0`` 生效。
    """
    client = _client()
    base = {"session_id": "buf-turns-2", "user_message": "看看幂等", "skeleton_kind": "default"}

    def _post(block: str) -> None:
        r = client.post(
            "/lifecycle/post-turn",
            json={**base, "turn_index": 1, "assistant_message": f"正文\n{block}\n"},
        )
        assert r.json()["session_buffer"]["buffered"] is True

    _post(_CODE_BLOCK)
    _post(_CODE_BLOCK)  # 同序号第二发：模拟重试，不是新的一轮

    replaced = _CODE_BLOCK.replace("给轮次序号加兜底", "换成重试后的新答案")
    _post(replaced)

    buf = _buffer_by_session("buf-turns-2")
    assert buf is not None
    payload = buf[1]
    assert len(payload["turns"]) == 1
    assert payload["turns"][0]["turn_index"] == 1
    assert payload["last_turn_index"] == 1
    assert "换成重试后的新答案" in payload["turns"][0]["sections"]["thought"]


def test_free_text_thought_round_is_buffered_not_dropped(db_path):
    """有 <thought> 原文但解析不出编号小节 → 该轮被接纳，原文进 thought 节。

    回归背景（issue #216）：旧实现把这种轮整轮按 no_sections 丢弃——原生思考 /
    骨架没注入的轮全被挡在缓冲外。session-end 合并出的胶囊【思路】必须是真实
    内容，不是「（待补）」。
    """
    client = _client()
    r = client.post(
        "/lifecycle/post-turn",
        json={
            "session_id": "buf-turns-3",
            "user_message": "帮我把轮次兜底想清楚",
            "assistant_message": f"正文开始\n{_FREE_TEXT_THOUGHT}\n正文结束",
            "skeleton_kind": "",  # 骨架没注入的轮：kind 为空
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["session_buffer"]["buffered"] is True  # 旧实现这里 False（no_sections）

    buf = _buffer_by_session("buf-turns-3")
    assert buf is not None
    turns = buf[1]["turns"]
    assert len(turns) == 1
    assert "单调计数" in turns[0]["sections"]["thought"]

    end = client.post("/lifecycle/session-end", json={"session_id": "buf-turns-3", "messages": []})
    assert end.status_code == 200, end.text
    capsule = end.json()["capsule"]
    assert capsule["turns"] == 1
    assert capsule["sections"]["thought"] != "（待补）"
    assert "单调计数" in capsule["sections"]["thought"]
    assert "thought" not in capsule["pending"]
