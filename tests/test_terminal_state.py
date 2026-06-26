"""终端 buffer 闲/忙推断。"""
from services.terminal_poll import infer_poll_from_buffer
from services.terminal_state import analyze_terminal_buffer


def test_idle_when_prompt_after_xxd_percent_noise():
    buf = (
        "stty -F /dev/ttyS9 9600 raw -echo && timeout 12 cat /dev/ttyS9 | xxd\n"
        "00000000: 2525 2525 2525 2525  25%25%25%25\n"
        "loading |###\n"
        "Terminated\n"
        "root@EG628:~/code/rtu_client#"
    )
    st = analyze_terminal_buffer(buf, connected=True)
    assert st["buffer_idle"] is True
    assert st["session_state"] == "idle"
    assert st["prompt_detected"] is True
    assert infer_poll_from_buffer(buf) == 0


def test_busy_when_still_on_terminated_line():
    buf = "cmd\n00000000: abcd\nTerminated\n"
    st = analyze_terminal_buffer(buf, connected=True)
    assert st["buffer_idle"] is False
    assert st["session_state"] == "busy"
    assert st["busy_reason"] == "no_prompt_at_tail"


def test_idle_with_ansi_prompt():
    buf = "\x1b[01;32mroot@EG628:~/code/rtu_client#\x1b[0m "
    st = analyze_terminal_buffer(buf, connected=True)
    assert st["buffer_idle"] is True
    assert st["session_state"] == "idle"


def test_can_send_command_when_idle():
    from services.terminal_state import merge_connection_flags

    analysis = analyze_terminal_buffer("root@host:~# ", connected=True)
    merged = merge_connection_flags(
        analysis,
        connected=True,
        exists=True,
        pending=False,
        can_read_buffer=True,
        disconnect_reason=None,
    )
    assert merged["can_send_command"] is True
