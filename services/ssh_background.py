"""SSH 长任务：后台 detached + 日志文件轮询（配合 ssh_execute）。"""
from __future__ import annotations

import base64
import re
from typing import Any

_EDGEOPS_DETACHED_RE = re.compile(
    r"EDGEOPS_DETACHED\s+pid=(\d+)\s+log=(\S+)",
    re.IGNORECASE,
)
_EDGEOPS_RUNNING_RE = re.compile(r"\bEDGEOPS_RUNNING\b", re.IGNORECASE)
_EDGEOPS_FINISHED_RE = re.compile(r"\bEDGEOPS_FINISHED\b", re.IGNORECASE)
_EDGEOPS_EXIT_RE = re.compile(r"EDGEOPS_EXIT_CODE=(\d+)", re.IGNORECASE)

_DEFAULT_LOG_DIR = "~/.edgeops/runs"
_DEFAULT_LOG_TEMPLATE = "~/.edgeops/runs/edgeops-$(date +%Y%m%d%H%M%S)-$$.log"


def sanitize_remote_log_path(path: str | None) -> str | None:
    """远端日志路径白名单校验，降低注入风险。"""
    p = (path or "").strip()
    if not p or len(p) > 512:
        return None
    if any(c in p for c in ("\n", "\r", "\0", "$(", "`", ";", "|", "&", "<", ">", '"')):
        return None
    if ".." in p:
        return None
    if not re.match(r"^[~./a-zA-Z0-9_+-]+$", p):
        return None
    return p


def default_remote_log_path() -> str:
    return _DEFAULT_LOG_TEMPLATE


def _shell_single_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def build_ssh_detach_command(command: str, log_path: str | None) -> str:
    """构造在远端后台执行 command、stdout/stderr 写入 log_path 的 shell。"""
    log = sanitize_remote_log_path(log_path) or default_remote_log_path()
    b64 = base64.b64encode((command or "").encode("utf-8")).decode("ascii")
    log_sq = _shell_single_quote(log)
    b64_sq = _shell_single_quote(b64)
    return (
        f"LOG={log_sq}; CMD_B64={b64_sq}; "
        f'mkdir -p "$(dirname "$LOG")" 2>/dev/null; '
        "nohup env EDGEOPS_LOG=\"$LOG\" EDGEOPS_CMD_B64=\"$CMD_B64\" bash -c '"
        "LOG=\"$EDGEOPS_LOG\"; "
        "echo $$ > \"${LOG}.pid\"; "
        "( eval \"$(echo \"$EDGEOPS_CMD_B64\" | base64 -d)\"; echo $? > \"${LOG}.exit\" ) >> \"$LOG\" 2>&1"
        "' </dev/null >/dev/null 2>&1 & "
        "sleep 0.5; "
        "echo EDGEOPS_DETACHED pid=$(cat \"${LOG}.pid\" 2>/dev/null) log=$LOG"
    )


def build_ssh_poll_log_command(log_path: str, tail_lines: int) -> str:
    """读取日志尾部并判断后台任务是否仍在运行。"""
    log = sanitize_remote_log_path(log_path)
    if not log:
        raise ValueError("invalid log_path")
    n = max(10, min(200, int(tail_lines)))
    log_sq = _shell_single_quote(log)
    return (
        f"LOG={log_sq}; L={n}; "
        "echo '=== EDGEOPS_LOG_TAIL ==='; tail -n \"$L\" \"$LOG\" 2>/dev/null || true; "
        "echo '=== EDGEOPS_JOB_STATUS ==='; "
        "if [ -f \"${LOG}.pid\" ]; then "
        "  if kill -0 $(cat \"${LOG}.pid\") 2>/dev/null; then echo EDGEOPS_RUNNING; "
        "  else echo EDGEOPS_FINISHED; "
        "    [ -f \"${LOG}.exit\" ] && echo EDGEOPS_EXIT_CODE=$(cat \"${LOG}.exit\"); "
        "  fi; "
        "else echo EDGEOPS_NO_PID; fi"
    )


def parse_detach_stdout(stdout: str) -> dict[str, Any]:
    out: dict[str, Any] = {"detached": False}
    m = _EDGEOPS_DETACHED_RE.search(stdout or "")
    if m:
        out["detached"] = True
        out["pid"] = int(m.group(1))
        out["log_path"] = m.group(2)
    return out


def parse_poll_stdout(stdout: str, stderr: str = "") -> dict[str, Any]:
    """从 poll 命令输出解析任务状态与日志正文。"""
    combined = (stdout or "") + "\n" + (stderr or "")
    info: dict[str, Any] = {
        "poll_log": True,
        "job_running": bool(_EDGEOPS_RUNNING_RE.search(combined)),
        "job_finished": bool(_EDGEOPS_FINISHED_RE.search(combined)),
    }
    em = _EDGEOPS_EXIT_RE.search(combined)
    if em:
        try:
            info["exit_code"] = int(em.group(1))
        except ValueError:
            pass
    tail = ""
    if "=== EDGEOPS_LOG_TAIL ===" in (stdout or ""):
        parts = (stdout or "").split("=== EDGEOPS_LOG_TAIL ===", 1)
        rest = parts[1]
        if "=== EDGEOPS_JOB_STATUS ===" in rest:
            tail = rest.split("=== EDGEOPS_JOB_STATUS ===", 1)[0].strip()
        else:
            tail = rest.strip()
    info["log_tail"] = tail
    return info
