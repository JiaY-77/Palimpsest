"""Tests for the thin Hermes adapter (``hermes-plugin/__init__.py``).

After the lifecycle refactor, the adapter holds **no** decision logic: it only
forwards lifecycle events to the Palimpsest本体 lifecycle protocol and returns
whatever the本体 decided. These tests pin the *transport* half:

  * ``prefetch``            -> POST /lifecycle/pre-turn      (returns inject_text)
  * ``thought_skeleton_context``
                            -> POST /lifecycle/pre-turn with thought_skeleton=True
                               (returns the skeleton wrapped in a <thought> instruction)
  * ``sync_turn``           -> POST /lifecycle/post-turn
  * ``on_session_end``      -> POST /lifecycle/session-end
  * ``on_pre_compress``     -> POST /lifecycle/pre-compress  (returns points_text)
  * cron/flush sessions are skipped
  * the backend must fail open (errors never raise)

A boundary guard asserts the decision symbols are gone from the adapter — if
they ever come back, the "strategy lives in the core" invariant has regressed.

The decision-behaviour tests that used to live here moved to
``tests/test_strategy_engine.py`` (they now exercise ``core.strategy``).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fake `agent.memory_provider` module so the plugin imports standalone.
# ---------------------------------------------------------------------------

_fake_agent = types.ModuleType("agent")
_fake_agent.__path__ = []  # make it a package

_fake_mp = types.ModuleType("agent.memory_provider")


class _FakeMemoryProvider:
    pass


class _FakeRecallStatus:
    def __init__(self, provider_label: str = "", count: int = 0) -> None:
        self.provider_label = provider_label
        self.count = count


_fake_mp.MemoryProvider = _FakeMemoryProvider  # type: ignore[attr-defined]
_fake_mp.RecallStatus = _FakeRecallStatus  # type: ignore[attr-defined]

sys.modules.setdefault("agent", _fake_agent)
sys.modules.setdefault("agent.memory_provider", _fake_mp)

_PLUGIN_PATH = __import__("pathlib").Path(__file__).resolve().parent.parent / "hermes-plugin" / "__init__.py"

_spec = importlib.util.spec_from_file_location("palimpsest_plugin_adapter", _PLUGIN_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

PalimpsestMemoryProvider = _mod.PalimpsestMemoryProvider
SEARCH_SCHEMA = _mod.SEARCH_SCHEMA
INGEST_SCHEMA = _mod.INGEST_SCHEMA


class _CapturingProvider(PalimpsestMemoryProvider):
    """Provider that records request payloads instead of doing HTTP."""

    def __init__(self, response: dict | None = None, **env: str) -> None:
        self.sent: list[tuple[str, dict]] = []
        self._response = response if response is not None else {}
        with patch.dict("os.environ", env, clear=False):
            super().__init__()

    def _fake_post(self, url: str, payload: dict, timeout: float = 5.0) -> dict:
        self.sent.append((url, payload))
        return self._response

    def _run(self, method: str, *args, **kwargs):
        with patch.object(_mod, "_http_post", side_effect=self._fake_post):
            return getattr(self, method)(*args, **kwargs)


# ---------------------------------------------------------------------------
# Boundary guard: decisions live in the core, not the adapter.
# ---------------------------------------------------------------------------


class TestAdapterHasNoDecisionLogic:
    def test_decision_symbols_are_gone(self):
        for name in (
            "_extract_points",
            "_is_near_duplicate",
            "_IMPORTANT_RE",
            "_EXPLICIT_INSTRUCTION_RE",
            "_NEAR_DUP_THRESHOLD",
            "is_trivial_prompt",
        ):
            assert not hasattr(_mod, name), f"{name} must live in core/strategy.py, not the adapter"

    def test_lifecycle_endpoints_are_used(self):
        src = _PLUGIN_PATH.read_text(encoding="utf-8")
        for endpoint in (
            "/lifecycle/pre-turn",
            "/lifecycle/post-turn",
            "/lifecycle/session-end",
            "/lifecycle/pre-compress",
        ):
            assert endpoint in src, f"adapter must forward to {endpoint}"


# ---------------------------------------------------------------------------
# prefetch -> /lifecycle/pre-turn
# ---------------------------------------------------------------------------


class TestPrefetchForwarding:
    def test_posts_to_pre_turn_and_returns_inject_text(self):
        cap = _CapturingProvider(response={"inject_text": "[Palimpsest 记忆注入]\n- (0.8) x"})
        cap._enabled = True
        out = cap._run("prefetch", "帮我查一下上周的会议记录")
        assert out == "[Palimpsest 记忆注入]\n- (0.8) x"
        url, payload = cap.sent[0]
        assert url.endswith("/lifecycle/pre-turn")
        assert payload["user_message"] == "帮我查一下上周的会议记录"

    def test_forwards_scope_tier_topk_minscore(self):
        cap = _CapturingProvider(
            response={"inject_text": "x"},
            PALIMPSEST_PREFETCH_SCOPE="memory",
            PALIMPSEST_PREFETCH_TIER="facts",
            PALIMPSEST_PREFETCH_TOP_K="3",
            PALIMPSEST_PREFETCH_MIN_SCORE="0.3",
        )
        cap._enabled = True
        cap._run("prefetch", "帮我查一下上周的会议记录")
        _, payload = cap.sent[0]
        assert payload["scope"] == "memory"
        assert payload["tier"] == "facts"
        assert payload["top_k"] == 3
        assert payload["min_score"] == 0.3

    def test_error_response_yields_empty(self):
        cap = _CapturingProvider(response={"error": "boom"})
        cap._enabled = True
        assert cap._run("prefetch", "帮我查一下上周的会议记录") == ""

    def test_disabled_returns_empty_without_request(self):
        cap = _CapturingProvider()
        cap._enabled = False
        assert cap._run("prefetch", "帮我查一下上周的会议记录") == ""
        assert cap.sent == []


# ---------------------------------------------------------------------------
# thought_skeleton_context -> /lifecycle/pre-turn (thought_skeleton=True)
# ---------------------------------------------------------------------------


class TestThoughtSkeletonForwarding:
    def test_wraps_skeleton_with_thought_instruction(self):
        skeleton = "- 目标：\n- 步骤：\n- 风险："
        cap = _CapturingProvider(response={"thought_skeleton": skeleton, "thought_skeleton_kind": "code"})
        cap._enabled = True
        out = cap._run("thought_skeleton_context", user_message="帮我改一个函数", session_id="s1")
        assert out.startswith("\n[思维底片·预设层]")
        assert "<thought>" in out
        assert out.endswith(skeleton)
        url, payload = cap.sent[0]
        assert url.endswith("/lifecycle/pre-turn")
        assert payload["thought_skeleton"] is True
        assert payload["user_message"] == "帮我改一个函数"
        assert payload["session_id"] == "s1"
        # kind 存类级跨实例共享桶（宿主双加载：钩子实例与 sync_turn 实例不同），
        # 键 = 请求里的 session_id。
        assert PalimpsestMemoryProvider._skeleton_kind_by_session["s1"] == "code"

    def test_empty_skeleton_returns_empty_string(self):
        cap = _CapturingProvider(response={"thought_skeleton": "", "thought_skeleton_kind": ""})
        cap._enabled = True
        assert cap._run("thought_skeleton_context", user_message="在吗") == ""

    def test_error_response_returns_empty_string(self):
        cap = _CapturingProvider(response={"error": "boom"})
        cap._enabled = True
        assert cap._run("thought_skeleton_context", user_message="帮我改一个函数") == ""

    def test_env_off_returns_empty_without_request(self):
        off = _CapturingProvider(PALIMPSEST_THOUGHT_SKELETON="false")
        off._enabled = True
        assert off._run("thought_skeleton_context", user_message="帮我改一个函数") == ""
        assert off.sent == []

    def test_injects_even_when_instance_never_initialized(self):
        """回归：宿主把插件加载两次（插件加载器 + scoped memory provider），
        ``initialize()`` 只调后者，``pre_llm_call`` 钩子挂在前者上——闸门若看
        ``_enabled`` 骨架就永远不注入（2026-10-17 生产实测）。"""
        skeleton = "- 目标：\n- 步骤："
        cap = _CapturingProvider(response={"thought_skeleton": skeleton, "thought_skeleton_kind": "default"})
        assert cap._enabled is False  # 从未 initialize
        out = cap._run("thought_skeleton_context", user_message="帮我改一个函数")
        assert out.startswith("\n[思维底片·预设层]")
        assert out.endswith(skeleton)
        assert cap.sent and cap.sent[0][1]["thought_skeleton"] is True

    def test_cron_platform_returns_empty_without_request(self):
        cap = _CapturingProvider(response={"thought_skeleton": "- x", "thought_skeleton_kind": "default"})
        assert cap._run("thought_skeleton_context", user_message="跑一下每日维护", platform="cron") == ""
        assert cap.sent == []

    # -- 双注入闸门（issue #206） ------------------------------------------

    def test_second_call_in_same_turn_is_suppressed(self):
        """回归：宿主把插件加载两次 → 同轮两个 ``pre_llm_call`` 回调各被调一次，
        第二个必须返回空串（否则 user 消息里出现两份一模一样的骨架）。"""
        skeleton = "- 目标：\n- 步骤："
        cap = _CapturingProvider(response={"thought_skeleton": skeleton, "thought_skeleton_kind": "default"})
        PalimpsestMemoryProvider._skeleton_turn_injected.clear()
        first = cap._run("thought_skeleton_context", user_message="帮我改函数", session_id="s206", turn_id="t1")
        second = cap._run("thought_skeleton_context", user_message="帮我改函数", session_id="s206", turn_id="t1")
        assert first.endswith(skeleton)
        assert second == ""
        assert len(cap.sent) == 1, "第二次回调连 HTTP 请求都不该再发"

    def test_different_turns_both_inject(self):
        skeleton = "- 目标："
        cap = _CapturingProvider(response={"thought_skeleton": skeleton, "thought_skeleton_kind": "default"})
        PalimpsestMemoryProvider._skeleton_turn_injected.clear()
        a = cap._run("thought_skeleton_context", user_message="第一轮问题", session_id="s206b", turn_id="t1")
        b = cap._run("thought_skeleton_context", user_message="第二轮问题", session_id="s206b", turn_id="t2")
        assert a and b
        assert len(cap.sent) == 2

    def test_dedup_keeps_kind_for_post_turn(self):
        """去重不能把第一个实例写好的 kind 清掉——收侧 ``sync_turn`` 还要用它上报。"""
        cap = _CapturingProvider(response={"thought_skeleton": "- x", "thought_skeleton_kind": "research"})
        PalimpsestMemoryProvider._skeleton_turn_injected.clear()
        cap._run("thought_skeleton_context", user_message="帮我调研一下", session_id="s206c", turn_id="t1")
        cap._run("thought_skeleton_context", user_message="帮我调研一下", session_id="s206c", turn_id="t1")
        assert PalimpsestMemoryProvider._skeleton_kind_by_session["s206c"] == "research"

    def test_no_turn_id_keeps_legacy_behaviour(self):
        """宿主不给 turn_id（旧宿主 / 非钩子调用）时不做去重，行为与改动前一致。"""
        cap = _CapturingProvider(response={"thought_skeleton": "- x", "thought_skeleton_kind": "default"})
        PalimpsestMemoryProvider._skeleton_turn_injected.clear()
        assert cap._run("thought_skeleton_context", user_message="帮我改函数") != ""
        assert cap._run("thought_skeleton_context", user_message="帮我改函数") != ""


# ---------------------------------------------------------------------------
# sync_turn -> /lifecycle/post-turn
# ---------------------------------------------------------------------------


class TestSyncTurnForwarding:
    def test_posts_to_post_turn(self):
        cap = _CapturingProvider()
        cap._enabled = True
        cap._run("sync_turn", "记住这个偏好", "好的")
        url, payload = cap.sent[0]
        assert url.endswith("/lifecycle/post-turn")
        assert payload["user_message"] == "记住这个偏好"
        assert payload["assistant_message"] == "好的"

    def test_auto_ingest_off_skips_request(self):
        cap = _CapturingProvider(PALIMPSEST_AUTO_INGEST="false")
        cap._enabled = True
        cap._run("sync_turn", "记住这个偏好", "好的")
        assert cap.sent == []


class TestSyncTurnTurnIndex:
    """sync_turn 自带每会话单调轮次计数（issue #216）。

    宿主给不了轮次号：``memory_manager`` 的 ``sync_turn`` 调用只带
    ``session_id`` / ``messages``（``turn_id`` 只到得了 ``pre_llm_call`` 钩子）。
    插件自己计数上送，服务端才有幂等去重的键——此前缺省恒 0 被当「同一轮」，
    会话缓冲每轮覆盖上一轮（turns 恒为 1）。

    计数是类级跨实例状态（宿主双加载，理由与 ``_skeleton_kind_by_session``
    一致），用例前置清桶保证确定性。
    """

    def setup_method(self):
        PalimpsestMemoryProvider._turn_index_by_session.clear()

    def test_monotonic_within_session(self):
        cap = _CapturingProvider()
        cap._enabled = True
        cap._run("sync_turn", "第一轮", "好的", session_id="s-idx-1")
        cap._run("sync_turn", "第二轮", "好的", session_id="s-idx-1")
        assert [p["turn_index"] for _, p in cap.sent] == [1, 2]

    def test_buckets_independent_per_session(self):
        cap = _CapturingProvider()
        cap._enabled = True
        cap._run("sync_turn", "a", "好的", session_id="s-idx-a")
        cap._run("sync_turn", "b", "好的", session_id="s-idx-b")
        cap._run("sync_turn", "a2", "好的", session_id="s-idx-a")
        assert [p["turn_index"] for _, p in cap.sent] == [1, 1, 2]


class TestTurnIndexResumeAfterReload:
    """重载续接（issue #219）：服务端回传解析序号，插件 max 校正本地计数。

    插件重载（部署同步 / 网关重启）后本地计数桶从 1 重计 → 第一轮 post-turn
    会覆盖活跃缓冲的第 1 轮。服务端在返回体里带 ``turn_index_resolved``，
    sync_turn 拿它把本地桶顶上去（``max(本地, 服务端值)``，只推不拉）——
    重载后第一轮就对齐到缓冲离点。``setup_method`` 清桶 = 模拟重载。
    """

    def setup_method(self):
        PalimpsestMemoryProvider._turn_index_by_session.clear()

    def test_server_gives_5_next_post_turn_sends_6(self):
        """服务端给 5 → 下一轮发 6（重载模拟 = 清桶后第一轮先发 1）。"""
        cap = _CapturingProvider(response={"turn_index_resolved": 5})
        cap._enabled = True
        cap._run("sync_turn", "重载后的第一轮", "好的", session_id="s-resume")
        assert cap.sent[0][1]["turn_index"] == 1, "重载后本地确实从 1 重计"
        cap._run("sync_turn", "重载后的第二轮", "好的", session_id="s-resume")
        assert cap.sent[1][1]["turn_index"] == 6, "服务端给 5，下一轮必须接 6"

    def test_seeding_only_pushes_never_pulls(self):
        """max 只推不拉：服务端 0 / 缺失 / 非法值不把本地计数拉回。"""
        cap = _CapturingProvider(response={"turn_index_resolved": 2})
        cap._enabled = True
        cap._run("sync_turn", "a", "好的", session_id="s-push")
        cap._run("sync_turn", "b", "好的", session_id="s-push")
        assert [p["turn_index"] for _, p in cap.sent] == [1, 3]  # 2 校正后 2→3

        stale = _CapturingProvider(response={"error": "backend down"})
        stale._enabled = True
        stale._run("sync_turn", "c", "好的", session_id="s-push")
        assert stale.sent[0][1]["turn_index"] == 4, "服务端不可用（无 resolved）→ 本地继续单调"

        zero = _CapturingProvider(response={"turn_index_resolved": 0})
        zero._enabled = True
        zero._run("sync_turn", "d", "好的", session_id="s-push")
        assert zero.sent[0][1]["turn_index"] == 5, "resolved=0 不算服务端给号"
        assert PalimpsestMemoryProvider._turn_index_by_session["s-push"] == 5, "本地计数不被拉回"


class TestSyncTurnSkeletonKind:
    """sync_turn 上报本轮骨架类型（事实字段，供本体写进 thought 节点 payload）。

    「文本长得像什么」的归因不在宿主侧——宿主只上报事实（本轮注入了什么），
    本体按 content 形态另判 thought_origin，两者交叉可查「注入了骨架但没照写」。

    状态是**类级跨实例**的：宿主把插件加载两次（插件加载器 + scoped memory
    provider），``pre_llm_call`` 钩子挂实例 A、宿主 ``initialize()`` /
    ``sync_turn()`` 调实例 B（生产实测两实例 self_id 不一致）——用例因此全部
    跨实例构造（A 注入、B 上报），单实例语义抓不到这类丢失。
    """

    @pytest.fixture(autouse=True)
    def _clean_kind_bucket(self):
        """类级桶是全进程状态：每例前后清空——用例间不串味，也不污染既有用例。"""
        PalimpsestMemoryProvider._skeleton_kind_by_session.clear()
        yield
        PalimpsestMemoryProvider._skeleton_kind_by_session.clear()

    def test_kind_passes_across_instances(self):
        """双加载：钩子在实例 A 记下 kind，实例 B 的 sync_turn 上报（跨实例不丢）。"""
        a = _CapturingProvider(
            response={
                "thought_skeleton": "<thought>\n1. 要改什么 / 新增什么（一句话）：\n</thought>",
                "thought_skeleton_kind": "code",
            }
        )
        b = _CapturingProvider()
        a._enabled = True
        b._enabled = True
        a._run("thought_skeleton_context", user_message="帮我改一个函数", session_id="s-inst")
        b._run("sync_turn", "帮我改一个函数", "改好了", session_id="s-inst")
        url, payload = b.sent[-1]
        assert url.endswith("/lifecycle/post-turn")
        assert payload["skeleton_kind"] == "code"

    def test_kind_consumed_after_first_sync(self):
        """取值即消费：同一会话连续两次 sync_turn，第二次必须上报空串。"""
        a = _CapturingProvider(
            response={
                "thought_skeleton": "<thought>\n1. 要改什么 / 新增什么（一句话）：\n</thought>",
                "thought_skeleton_kind": "code",
            }
        )
        b = _CapturingProvider()
        a._enabled = True
        b._enabled = True
        a._run("thought_skeleton_context", user_message="帮我改一个函数", session_id="s-dup")
        b._run("sync_turn", "问题一", "回答一", session_id="s-dup")
        b._run("sync_turn", "问题二", "回答二", session_id="s-dup")
        _, first = b.sent[-2]
        _, second = b.sent[-1]
        assert first["skeleton_kind"] == "code"
        assert second["skeleton_kind"] == ""

    def test_sessions_do_not_leak_kind(self):
        """会话隔离：s1 注入 research 后，s2 的 sync_turn 上报空串（分桶不互染）。"""
        a = _CapturingProvider(
            response={
                "thought_skeleton": "<thought>\n1. 问题是什么（一句话）：\n</thought>",
                "thought_skeleton_kind": "research",
            }
        )
        b = _CapturingProvider()
        a._enabled = True
        b._enabled = True
        a._run("thought_skeleton_context", user_message="调研对比一下这两个方案的可行性", session_id="s1")
        b._run("sync_turn", "问题", "回答", session_id="s2")
        _, payload = b.sent[-1]
        assert payload["skeleton_kind"] == ""

    def test_gate_skip_resets_kind_not_leak_previous_turn(self):
        """本轮没走到给侧（空消息被闸）→ 桶已重置，上报空串，不残留上一轮 kind。"""
        a = _CapturingProvider(
            response={
                "thought_skeleton": "<thought>\n1. 问题是什么（一句话）：\n</thought>",
                "thought_skeleton_kind": "research",
            }
        )
        b = _CapturingProvider()
        a._enabled = True
        b._enabled = True
        a._run("thought_skeleton_context", user_message="调研对比一下这两个方案的可行性", session_id="s-gate")
        a._run("thought_skeleton_context", user_message="   ", session_id="s-gate")  # 空消息：闸门直接返回
        b._run("sync_turn", "问题", "回答", session_id="s-gate")
        _, payload = b.sent[-1]
        assert payload["skeleton_kind"] == ""

    def test_idle_turn_reports_empty_across_instances(self):
        """本体返回空骨架（闲聊轮）：跨实例上报空串（事实字段如实 = 未注入）。"""
        a = _CapturingProvider(response={"thought_skeleton": "", "thought_skeleton_kind": ""})
        b = _CapturingProvider()
        a._enabled = True
        b._enabled = True
        a._run("thought_skeleton_context", user_message="帮我改一个函数", session_id="s-idle")
        b._run("sync_turn", "问题", "回答", session_id="s-idle")
        _, payload = b.sent[-1]
        assert payload["skeleton_kind"] == ""


class TestThoughtExtractionFromReasoning:
    """收侧取思考：本轮任意一条 assistant 的 reasoning 都算，不只看最后一条；
    一轮里的多段全部收进、按消息时间序拼接（issue #216，工具循环里此前只留
    最后一段）。"""

    def test_takes_reasoning_from_earlier_assistant_in_same_turn(self):
        msgs = [
            {"role": "user", "content": "改个函数"},
            {"role": "assistant", "content": "先看代码", "reasoning": "1. 要的是什么：改函数"},
            {"role": "assistant", "content": "最终答案"},  # 末尾这条没有 reasoning
        ]
        assert _mod._assistant_reasoning(msgs) == "1. 要的是什么：改函数"

    def test_all_reasoning_segments_joined_in_order(self):
        """一轮的多段 reasoning 全收、按消息时间序拼接（此前只取最近一段）。"""
        msgs = [
            {"role": "assistant", "content": "早期", "reasoning": "早期思考"},
            {"role": "assistant", "content": "稍后", "reasoning": "稍后思考"},
            {"role": "assistant", "content": "末尾"},
        ]
        assert _mod._assistant_reasoning(msgs) == "早期思考\n\n稍后思考"

    def test_empty_reasoning_segment_skipped(self):
        """空白段不进拼接结果，非空段之间以空行相连。"""
        msgs = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "a", "reasoning": "第一段"},
            {"role": "assistant", "content": "b", "reasoning": "   "},
            {"role": "assistant", "content": "c", "reasoning": "第二段"},
        ]
        assert _mod._assistant_reasoning(msgs) == "第一段\n\n第二段"

    def test_does_not_leak_across_turns(self):
        msgs = [
            {"role": "assistant", "content": "上一轮", "reasoning": "上一轮的思考"},
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "答"},
        ]
        assert _mod._assistant_reasoning(msgs) == ""

    def test_reasoning_content_key_compatible(self):
        msgs = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "答", "reasoning_content": "另一种键名"},
        ]
        assert _mod._assistant_reasoning(msgs) == "另一种键名"


# ---------------------------------------------------------------------------
# on_session_end -> /lifecycle/session-end
# ---------------------------------------------------------------------------


class TestSessionEndForwarding:
    def test_posts_to_session_end(self):
        cap = _CapturingProvider()
        cap._enabled = True
        msgs = [{"role": "user", "content": "记住这个偏好：我喜欢深色模式"}]
        cap._run("on_session_end", msgs)
        url, payload = cap.sent[0]
        assert url.endswith("/lifecycle/session-end")
        assert payload["messages"] == msgs


# ---------------------------------------------------------------------------
# on_pre_compress -> /lifecycle/pre-compress
# ---------------------------------------------------------------------------


class TestPreCompressForwarding:
    def test_returns_points_text(self):
        cap = _CapturingProvider(response={"points_text": "[user] 记住这个偏好"})
        cap._enabled = True
        out = cap._run("on_pre_compress", [{"role": "user", "content": "记住这个偏好"}])
        assert out == "[user] 记住这个偏好"
        assert cap.sent[0][0].endswith("/lifecycle/pre-compress")


# ---------------------------------------------------------------------------
# initialize: cron/flush sessions are skipped
# ---------------------------------------------------------------------------


class TestInitializeContexts:
    def test_cron_context_disables_provider(self):
        cap = _CapturingProvider()
        cap.initialize("s1", agent_context="cron")
        assert cap._enabled is False

    def test_normal_context_enables_provider(self):
        cap = _CapturingProvider()
        cap.initialize("s1", agent_context="cli", platform="desktop")
        assert cap._enabled is True


# ---------------------------------------------------------------------------
# config_schema.py still exposes prefetch_scope
# ---------------------------------------------------------------------------


class TestConfigSchemaSurface:
    def test_prefetch_scope_field_declared(self):
        schema_path = _PLUGIN_PATH.parent / "config_schema.py"
        fake = _fake_config_schema_module()
        # config_schema.py imports `plugins.memory.config_schema` at module load;
        # register the pure-data stand-in under that exact name first.
        with patch.dict(sys.modules, {"plugins.memory.config_schema": fake}):
            spec = importlib.util.spec_from_file_location("_pscope_schema", schema_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        keys = [f.key for f in module.CONFIG_SCHEMA.fields]
        assert "prefetch_scope" in keys


def _fake_config_schema_module() -> types.ModuleType:
    """Minimal stand-in for plugins.memory.config_schema (pure data classes)."""
    mod = types.ModuleType("plugins.memory.config_schema")

    class ProviderField:
        def __init__(self, *, key, label, default, description, kind="text", inline=False):
            self.key = key
            self.label = label
            self.default = default
            self.description = description
            self.kind = kind
            self.inline = inline

    class ProviderConfigSchema:
        def __init__(self, *, name, label, storage, docs_url, fields):
            self.name = name
            self.label = label
            self.storage = storage
            self.docs_url = docs_url
            self.fields = fields

    mod.ProviderField = ProviderField  # type: ignore[attr-defined]
    mod.ProviderConfigSchema = ProviderConfigSchema  # type: ignore[attr-defined]
    mod.STORAGE_FLAT_JSON = "flat_json"  # type: ignore[attr-defined]
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    mem = types.ModuleType("plugins.memory")
    mem.__path__ = []
    sys.modules.setdefault("plugins", pkg)
    sys.modules.setdefault("plugins.memory", mem)
    return mod


# ---------------------------------------------------------------------------
# 写请求超时（issue #191）：默认对齐服务端 30 秒、可配、非法回退、失败可见
# ---------------------------------------------------------------------------


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b'{"ok": true}'


def test_write_timeout_defaults_to_server_budget(monkeypatch):
    """旧版是硬编码 5 秒；现在默认与服务端自留的 30 秒对齐。"""
    monkeypatch.delenv("PALIMPSEST_WRITE_TIMEOUT", raising=False)
    assert _mod._write_timeout() == 30.0


def test_write_timeout_reads_env(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_WRITE_TIMEOUT", "45")
    assert _mod._write_timeout() == 45.0


def test_write_timeout_falls_back_on_invalid_value(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_WRITE_TIMEOUT", "abc")
    assert _mod._write_timeout() == 30.0
    monkeypatch.setenv("PALIMPSEST_WRITE_TIMEOUT", "-5")
    assert _mod._write_timeout() == 30.0


def test_http_post_default_uses_write_timeout(monkeypatch):
    """未显式传 timeout → 用 _write_timeout（不是旧的 5 秒硬编码）。"""
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setenv("PALIMPSEST_WRITE_TIMEOUT", "42")
    monkeypatch.setattr(_mod.urllib.request, "urlopen", fake_urlopen)
    assert _mod._http_post("http://127.0.0.1:8090/x", {}) == {"ok": True}
    assert seen["timeout"] == 42.0


def test_http_post_explicit_timeout_wins(monkeypatch):
    """思考骨架那条热路径显式传更短预算，不被默认值覆盖。"""
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setenv("PALIMPSEST_WRITE_TIMEOUT", "42")
    monkeypatch.setattr(_mod.urllib.request, "urlopen", fake_urlopen)
    _mod._http_post("http://127.0.0.1:8090/x", {}, timeout=2.0)
    assert seen["timeout"] == 2.0


def test_http_post_failure_logged_at_warning(monkeypatch, caplog):
    """失败不能只 debug（旧行为）——否则「写入被吞掉」在用户侧完全无痕。"""
    import logging

    def boom(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(_mod.urllib.request, "urlopen", boom)
    with caplog.at_level(logging.WARNING, logger=_mod.logger.name):
        out = _mod._http_post("http://127.0.0.1:8090/lifecycle/post-turn", {})
    assert "error" in out
    assert any(r.levelno >= logging.WARNING for r in caplog.records), "失败必须留下 warning"
