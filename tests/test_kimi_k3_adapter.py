"""Kimi K3 官方契约适配：reasoning_effort + 完整 assistant 回传。"""

import os

import pytest

from services.llm_adapter import (
    apply_provider_request_extensions,
    build_assistant_history_message,
    is_kimi_k3_model,
    resolve_kimi_reasoning_effort,
)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("kimi-k3", True),
        ("Kimi-K3", True),
        ("moonshot/kimi-k3", True),
        ("kimi-k3-preview", True),
        ("kimi-k2.6", False),
        ("kimi-k2.7-code", False),
        ("qwen3.5-plus", False),
        ("", False),
        (None, False),
    ],
)
def test_is_kimi_k3_model(name, expected):
    assert is_kimi_k3_model(name) is expected


def test_apply_injects_reasoning_effort_default_low(monkeypatch):
    monkeypatch.delenv("EDGEOPS_KIMI_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("MOSS_KIMI_REASONING_EFFORT", raising=False)
    out = apply_provider_request_extensions(
        {"model": "kimi-k3", "messages": [], "enable_thinking": False, "temperature": 0.7},
        model="kimi-k3",
    )
    assert out["reasoning_effort"] == "low"
    assert "enable_thinking" not in out
    assert "thinking" not in out
    assert "temperature" not in out


def test_apply_respects_explicit_reasoning_effort():
    out = apply_provider_request_extensions(
        {"model": "kimi-k3", "reasoning_effort": "high"},
        model="kimi-k3",
    )
    assert out["reasoning_effort"] == "high"


def test_apply_env_edgeops_overrides_default(monkeypatch):
    monkeypatch.setenv("EDGEOPS_KIMI_REASONING_EFFORT", "max")
    monkeypatch.delenv("MOSS_KIMI_REASONING_EFFORT", raising=False)
    assert resolve_kimi_reasoning_effort() == "max"
    out = apply_provider_request_extensions({"model": "kimi-k3"}, model="kimi-k3")
    assert out["reasoning_effort"] == "max"


def test_apply_env_moss_compat(monkeypatch):
    monkeypatch.delenv("EDGEOPS_KIMI_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("MOSS_KIMI_REASONING_EFFORT", "high")
    assert resolve_kimi_reasoning_effort() == "high"


def test_apply_no_op_for_non_k3():
    payload = {
        "model": "qwen3.5-plus",
        "enable_thinking": False,
        "temperature": 0.5,
    }
    out = apply_provider_request_extensions(payload, model="qwen3.5-plus")
    assert out["enable_thinking"] is False
    assert out["temperature"] == 0.5
    assert "reasoning_effort" not in out


def test_build_assistant_history_preserves_reasoning_and_tools():
    msg = {
        "content": "先查一下",
        "reasoning_content": "我需要调用 fs_list",
    }
    tool_calls = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "fs_list", "arguments": "{}"},
    }]
    out = build_assistant_history_message(msg, tool_calls=tool_calls)
    assert out["role"] == "assistant"
    assert out["content"] == "先查一下"
    assert out["reasoning_content"] == "我需要调用 fs_list"
    assert out["tool_calls"] == tool_calls


def test_build_assistant_history_omits_empty_reasoning():
    out = build_assistant_history_message({"content": "hi"}, tool_calls=None)
    assert out == {"role": "assistant", "content": "hi"}
    assert "reasoning_content" not in out


def test_build_assistant_history_reads_reasoning_alias():
    out = build_assistant_history_message({"content": "", "reasoning": "think"})
    assert out["reasoning_content"] == "think"


def test_invalid_env_effort_falls_back_low(monkeypatch):
    monkeypatch.setenv("EDGEOPS_KIMI_REASONING_EFFORT", "ultra")
    monkeypatch.delenv("MOSS_KIMI_REASONING_EFFORT", raising=False)
    assert resolve_kimi_reasoning_effort() == "low"
