"""ssh_channel wait_seconds：钳制与 batch poll 汇总。"""
from services.terminal_poll import (
    TerminalPollBatchState,
    apply_terminal_poll_tool_result,
    attach_ssh_channel_wait_fields,
    clamp_ssh_channel_wait_seconds,
)


def test_clamp_ssh_channel_wait_seconds():
    assert clamp_ssh_channel_wait_seconds(None) == 0
    assert clamp_ssh_channel_wait_seconds(0) == 0
    assert clamp_ssh_channel_wait_seconds(10) == 10
    assert clamp_ssh_channel_wait_seconds(30) == 30
    assert clamp_ssh_channel_wait_seconds(999) == 30
    assert clamp_ssh_channel_wait_seconds(-3) == 0
    assert clamp_ssh_channel_wait_seconds("12") == 12
    assert clamp_ssh_channel_wait_seconds("x") == 0


def test_attach_ssh_channel_wait_fields():
    p0 = attach_ssh_channel_wait_fields({"success": True}, {"wait_seconds": 0})
    assert "wait_seconds" not in p0
    assert "next_poll_in_seconds" not in p0

    p10 = attach_ssh_channel_wait_fields({"success": True}, {"wait_seconds": 10})
    assert p10["wait_seconds"] == 10
    assert p10["next_poll_in_seconds"] == 10

    p_clamp = attach_ssh_channel_wait_fields({"success": True}, {"wait_seconds": 999})
    assert p_clamp["wait_seconds"] == 30


def test_apply_terminal_poll_ssh_channel_wait():
    state = TerminalPollBatchState()

    poll0, obj0 = apply_terminal_poll_tool_result(
        state,
        "ssh_channel_read_lines",
        {"channel_id": 1, "wait_seconds": 0},
        {"success": True, "lines": []},
        success=True,
    )
    assert poll0 == 0

    poll10, obj10 = apply_terminal_poll_tool_result(
        state,
        "ssh_channel_read_lines",
        {"channel_id": 1, "wait_seconds": 10},
        {"success": True, "lines": []},
        success=True,
    )
    assert poll10 == 10
    assert obj10["wait_seconds"] == 10
    assert obj10["next_poll_in_seconds"] == 10

    poll30, obj30 = apply_terminal_poll_tool_result(
        state,
        "ssh_channel_has_new",
        {"channel_id": 1, "wait_seconds": 999},
        {"success": True, "has_new": False},
        success=True,
    )
    assert poll30 == 30
    assert obj30["wait_seconds"] == 30

    poll_fail, _ = apply_terminal_poll_tool_result(
        state,
        "ssh_channel_read_length",
        {"channel_id": 1, "wait_seconds": 10},
        {"success": False, "error": "x"},
        success=False,
    )
    assert poll_fail == 0
