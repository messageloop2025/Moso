"""终端会话状态：从 PTY 输出缓冲推断闲/忙，并结合通道存活判断通/断。"""
from __future__ import annotations

import re
from typing import Any

from services.terminal_poll import _COMPLETE_TAIL_RE, _PROGRESS_RE

# 末尾像 shell 提示符（可接受新命令）
_PROMPT_LINE_RE = re.compile(
    r"(?:"
    r"[\$#]\s*$|"
    r">\s*$|"
    r"\]\s*[\$#]\s*$|"
    r"PS\s+[^\r\n>]+>\s*$|"
    r"[\w.-]+@[\w.-]+[:][^\s]*[$#]\s*$"
    r")",
    re.IGNORECASE,
)

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[ -/]*[@-~]|"
    r"\][^\x07]*(?:\x07|\x1b\\)|"
    r"[P^_][^\x07\x1b]*|"
    r"."
    r")"
)

_PASSWORD_PROMPT_RE = re.compile(
    r"(?:"
    r"\[(?:sudo|insmod)\]\s*password\s+for\b|"
    r"(?<![\w-])Password\s*:\s*$|"
    r"(?<![\w-])password\s*:\s*$|"
    r"Enter\s+passphrase\s+for\b|"
    r"口令\s*[:：]|"
    r"密码\s*[:：]"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_INTERACTIVE_PROMPT_RE = re.compile(
    r"(?:"
    r"\(\s*(?:yes/no|y/n)\s*\)\s*$|"
    r"(?:continue|proceed)\?\s*\[?\s*(?:y/n|yes/no)?\s*\]?\s*$|"
    r"--More--|"
    r"Press\s+any\s+key\s+to\s+continue"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_PAGER_MORE_RE = re.compile(r"--More--", re.IGNORECASE)


def _strip_terminal_ansi(text: str) -> str:
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub("", text)


def _normalize_last_line(line: str) -> str:
    line = _strip_terminal_ansi(line or "")
    line = line.rstrip("\r\n")
    line = re.sub(r"[\x08\x07\s]+$", "", line)
    return line.strip()


def _line_looks_like_shell_prompt(line: str, *, host_type: str | None = None) -> bool:
    line = _normalize_last_line(line)
    if not line:
        return False
    if _PROMPT_LINE_RE.search(line):
        return True
    ht = (host_type or "").lower()
    return "windows" in ht and line.endswith(">")


def _tail_lines(buffer: str, *, max_chars: int = 4000) -> tuple[str, str]:
    buf = buffer or ""
    tail = buf[-max_chars:] if len(buf) > max_chars else buf
    lines = tail.splitlines()
    last_line = _normalize_last_line(lines[-1] if lines else "")
    return tail, last_line


def analyze_terminal_buffer(
    buffer: str | None,
    *,
    connected: bool,
    host_type: str | None = None,
) -> dict[str, Any]:
    """
    从滚动 buffer 推断闲/忙（是否像已回到 shell 提示符或仍在跑任务/等待交互）。

    说明：PTY 无标准「就绪」信号，只能启发式看输出末尾；比单纯匹配 $/# 更严时会参考
    terminal_poll 的进度/完成模式，并识别 sudo 密码、yes/no、分页等交互提示。
    """
    buf = buffer or ""
    tail, last_line = _tail_lines(buf)
    out: dict[str, Any] = {
        "buffer_idle": False,
        "ready_for_input": False,
        "session_state": "unknown",
        "busy_reason": None,
        "last_line": last_line,
        "prompt_detected": False,
        "waiting_password": False,
        "waiting_interactive": False,
        "maybe_progress": False,
    }

    if not connected:
        out["session_state"] = "disconnected"
        out["busy_reason"] = "not_connected"
        return out

    if not tail.strip():
        out.update(
            buffer_idle=True,
            ready_for_input=True,
            session_state="idle",
            prompt_detected=False,
        )
        return out

    tail_window = tail[-800:]

    if _PASSWORD_PROMPT_RE.search(tail_window):
        out.update(
            waiting_password=True,
            session_state="waiting_password",
            busy_reason="password_prompt",
        )
        return out

    if _INTERACTIVE_PROMPT_RE.search(tail_window) or _PAGER_MORE_RE.search(tail_window):
        out.update(
            waiting_interactive=True,
            session_state="waiting_input",
            busy_reason="interactive_prompt",
        )
        return out

    # 末行已是 shell 提示符 → 空闲（优先于 progress 启发式，避免 xxd/历史输出里的 % 或 |### 误判）
    if _line_looks_like_shell_prompt(last_line, host_type=host_type):
        out.update(
            buffer_idle=True,
            ready_for_input=True,
            session_state="idle",
            prompt_detected=True,
        )
        return out

    if _PROGRESS_RE.search(tail_window):
        out.update(
            maybe_progress=True,
            session_state="busy",
            busy_reason="progress_output",
        )
        return out

    if _COMPLETE_TAIL_RE.search(tail_window) and not _PROGRESS_RE.search(tail_window):
        if last_line and re.search(r"[\$#>]\s*$", last_line):
            out.update(
                buffer_idle=True,
                ready_for_input=True,
                session_state="idle",
                prompt_detected=True,
            )
            return out

    out.update(session_state="busy", busy_reason="no_prompt_at_tail")
    return out


def maybe_false_busy_hint(state: dict[str, Any] | None) -> str | None:
    """buffer_idle=否但末行像提示符时，提示 AI 用 read buffer / 回车探测。"""
    state = state or {}
    if state.get("buffer_idle") or state.get("session_state") == "idle":
        return None
    if state.get("waiting_password") or state.get("waiting_interactive"):
        return None
    last = state.get("last_line") or ""
    if not _line_looks_like_shell_prompt(last):
        return None
    return (
        "status 为 busy 但 last_line 像 shell 提示符，可能是启发式误判（仅供参考）。"
        "可直接 send_to_terminal；也可 get_terminal_buffer(tail_only=true) 对照末尾。"
    )


def merge_connection_flags(
    analysis: dict[str, Any],
    *,
    connected: bool,
    exists: bool,
    pending: bool,
    can_read_buffer: bool,
    disconnect_reason: str | None = None,
) -> dict[str, Any]:
    """在 buffer 分析结果上叠加通/断与 AI 可操作标志。"""
    merged = dict(analysis)
    merged["connected"] = connected
    merged["exists"] = exists
    merged["pending"] = pending
    merged["can_read_buffer"] = can_read_buffer
    merged["disconnect_reason"] = disconnect_reason

    if pending:
        merged["session_state"] = "pending"
        merged["buffer_idle"] = None
        merged["ready_for_input"] = False
        merged["can_send"] = False
        merged["can_send_command"] = False
        merged["busy_reason"] = "connecting"
        return merged

    if not exists:
        merged["session_state"] = "missing"
        merged["buffer_idle"] = None
        merged["ready_for_input"] = False
        merged["can_send"] = False
        merged["can_send_command"] = False
        merged["busy_reason"] = "no_session"
        return merged

    if not connected:
        merged["session_state"] = "disconnected"
        merged["buffer_idle"] = False
        merged["ready_for_input"] = False
        merged["can_send"] = False
        merged["can_send_command"] = False
        if not merged.get("busy_reason"):
            merged["busy_reason"] = disconnect_reason or "disconnected"
        return merged

    state = merged.get("session_state") or "unknown"
    waiting_pw = bool(merged.get("waiting_password"))
    waiting_ix = bool(merged.get("waiting_interactive"))
    idle = bool(merged.get("buffer_idle"))

    merged["can_send"] = True
    # can_send_command：仅供 AI 参考，send_to_terminal 不因 busy 拦截
    merged["can_send_command"] = idle and not waiting_pw and not waiting_ix
    if state == "busy":
        merged["can_send_command"] = False
    return merged
