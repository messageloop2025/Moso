"""StateMachine 状态转换、属性、持久化测试。"""
from __future__ import annotations

import asyncio

import pytest

from services.agent_state_machine import AgentState, AgentStateMachine


def _sm(**kw):
    return AgentStateMachine(session_id=kw.pop("session_id", 100), user={"id": 1}, **kw)


async def _sm_transition(sm, state, reason=""):
    """Helper: await transition in a new event loop."""
    await sm.transition(state, reason=reason)


class TestStateTransitions:
    @pytest.mark.asyncio
    async def test_initial_state_is_idle(self):
        sm = _sm()
        assert sm.state == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_valid_transition_idle_to_starting(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING, reason="begin")
        assert sm.state == AgentState.STARTING
        assert len(sm.state_history) == 1
        assert sm.state_history[0]["from"] == "idle"
        assert sm.state_history[0]["to"] == "starting"
        assert sm.state_history[0]["reason"] == "begin"

    @pytest.mark.asyncio
    async def test_valid_transition_starting_to_thinking(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.THINKING)
        assert sm.state == AgentState.THINKING
        assert len(sm.state_history) == 2

    @pytest.mark.asyncio
    async def test_valid_transition_thinking_to_acting(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.THINKING)
        await sm.transition(AgentState.ACTING)
        assert sm.state == AgentState.ACTING

    @pytest.mark.asyncio
    async def test_valid_transition_acting_to_observing(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.THINKING)
        await sm.transition(AgentState.ACTING)
        await sm.transition(AgentState.OBSERVING)
        assert sm.state == AgentState.OBSERVING

    @pytest.mark.asyncio
    async def test_valid_transition_observing_to_thinking_loop(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.THINKING)
        await sm.transition(AgentState.ACTING)
        await sm.transition(AgentState.OBSERVING)
        await sm.transition(AgentState.THINKING)
        assert sm.state == AgentState.THINKING
        assert len(sm.state_history) == 5

    @pytest.mark.asyncio
    async def test_valid_transition_to_done(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.THINKING)
        await sm.transition(AgentState.REPLYING)
        await sm.transition(AgentState.DONE)
        assert sm.state == AgentState.DONE

    @pytest.mark.asyncio
    async def test_done_is_terminal(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.THINKING)
        await sm.transition(AgentState.REPLYING)
        await sm.transition(AgentState.DONE)
        await sm.transition(AgentState.THINKING)
        assert sm.state == AgentState.DONE

    @pytest.mark.asyncio
    async def test_cancelled_is_terminal(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.THINKING)
        await sm.transition(AgentState.CANCELLED, reason="user stop")
        await sm.transition(AgentState.THINKING)
        assert sm.state == AgentState.CANCELLED


class TestIllegalTransitions:
    @pytest.mark.asyncio
    async def test_idle_to_thinking_skipped(self):
        sm = _sm()
        await sm.transition(AgentState.THINKING)
        assert sm.state == AgentState.IDLE
        assert len(sm.state_history) == 0

    @pytest.mark.asyncio
    async def test_illegal_transition_error_fallback(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.ERROR)
        assert sm.state == AgentState.ERROR

    @pytest.mark.asyncio
    async def test_illegal_transition_cancelled_fallback(self):
        sm = _sm()
        await sm.transition(AgentState.STARTING)
        await sm.transition(AgentState.CANCELLED)
        assert sm.state == AgentState.CANCELLED


class TestStateProperties:
    @pytest.mark.asyncio
    async def test_elapsed_ms(self):
        sm = _sm()
        assert sm.elapsed_ms >= 0

    def test_step_num_tracking(self):
        sm = _sm()
        assert sm.step_num == 0
        sm.step_num = 3
        assert sm.step_num == 3

    def test_total_steps(self):
        sm = _sm()
        assert sm.total_steps == 0
        sm.total_steps = 10
        assert sm.total_steps == 10

    def test_token_usage_init(self):
        sm = _sm()
        assert sm.token_usage == {"prompt": 0, "completion": 0}

    @pytest.mark.asyncio
    async def test_add_token_usage(self):
        """add_token_usage 内部调用 ensure_future，需要运行在 async 上下文中。"""
        sm = _sm()
        sm.add_token_usage(1000, 500)
        # 在 async 上下文中 ensure_future 可用
        await asyncio.sleep(0.05)
        assert sm.token_usage["prompt"] == 1000
        assert sm.token_usage["completion"] == 500

    @pytest.mark.asyncio
    async def test_add_token_usage_accumulates(self):
        sm = _sm()
        sm.add_token_usage(500, 200)
        await asyncio.sleep(0.05)
        sm.add_token_usage(300, 100)
        await asyncio.sleep(0.05)
        assert sm.token_usage["prompt"] == 800
        assert sm.token_usage["completion"] == 300


class TestDisabledMode:
    @pytest.mark.asyncio
    async def test_disabled_skips_transition_validation(self):
        sm = _sm()
        sm._enabled = False
        await sm.transition(AgentState.THINKING)
        assert sm.state == AgentState.THINKING
        assert len(sm.state_history) == 0
