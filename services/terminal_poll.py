"""终端长任务 / 输出轮询：在模型未传 next_poll_in_seconds 时自动推断等待秒数。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import config as _config

# 模型显式传入时优先；否则按命令 / buffer 启发式
_POLL_MIN = 1
_POLL_MAX = 3600
_POLL_SHORT = max(1, min(30, int(getattr(_config, "AGENT_TERMINAL_POLL_SHORT", 1))))
_POLL_DEFAULT = max(_POLL_SHORT, min(600, int(getattr(_config, "AGENT_TERMINAL_POLL_DEFAULT", 2))))
_POLL_PROGRESS = max(_POLL_DEFAULT, min(600, int(getattr(_config, "AGENT_TERMINAL_POLL_PROGRESS", 3))))
_POLL_MAX_AUTO = max(_POLL_PROGRESS, min(_POLL_MAX, int(getattr(_config, "AGENT_TERMINAL_POLL_MAX", 8))))

# 发命令后常见长耗时关键词（一行或多行命令）
_LONG_CMD_RE = re.compile(
    r"(?:^|[;&|]\s*|\n)\s*(?:sudo\s+)?(?:"
    r"apt(?:-get)?\s+(?:install|upgrade|dist-upgrade|full-upgrade|remove|purge)|"
    r"dnf\s+(?:install|upgrade|groupinstall)|yum\s+(?:install|update|groupinstall)|"
    r"pacman\s+-S|brew\s+install|"
    r"pip3?\s+install|pip3?\s+download|"
    r"npm\s+(?:install|ci|run\s+build)|yarn\s+(?:install|build)|pnpm\s+(?:install|build)|"
    r"(?:make|cmake|ninja|meson|cargo\s+build|go\s+build|gradle|mvn|ant)\b|"
    r"docker\s+(?:pull|build|compose\s+(?:up|build|pull))|"
    r"(?:curl|wget|aria2c)\s+[^\s]+|"
    r"rsync\s+|scp\s+-r|"
    r"git\s+clone|"
    r"(?:tar|unzip|7z)\s+(?:x|xf|xzf)|"
    r"composer\s+install|"
    r"systemctl\s+(?:restart|start|stop|reload)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_SUDO_RE = re.compile(r"(?:^|[;&|]\s*|\n)\s*sudo\s+", re.IGNORECASE | re.MULTILINE)

# 仅控制键、空输入 — 不等待
_CTRL_ONLY_RE = re.compile(
    r"^\s*(?:<Ctrl\+[A-Za-z]>)\s*$",
    re.IGNORECASE,
)

# 明显瞬时命令（单行、无管道到重任务）
_QUICK_CMD_RE = re.compile(
    r"^\s*(?:"
    r"(?:cd|pwd|ls|ll|dir|cat|head|tail|grep|egrep|fgrep|find|which|whereis|type|echo|printf|"
    r"export|set|unset|alias|history|clear|whoami|id|date|uname|hostname|df|du|free|uptime|"
    r"ps|top|htop|ss|netstat|ip\s|ifconfig|ping|dig|nslookup|wc|sort|uniq|tr|cut|awk|sed)\b"
    r"|true|false|:)\b",
    re.IGNORECASE,
)

_PROGRESS_RE = re.compile(
    r"(?:"
    r"\b\d{1,3}%\b|"
    r"\b(?:Downloading|Installing|Building|Compiling|Unpacking|Extracting|Configuring|"
    r"Preparing|Processing|Reading state|Get:\d|Fetched|Receiving objects|"
    r"Building wheel|Running setup\.py|npm WARN|added \d+ packages)\b|"
    r"\bETA\b|\bTime Left\b|"
    r"(?:\||/|\\)\s*[#=]{1,}|"
    r"\brpm\s*:\s*.*%|\bdpkg\b.*\.\.\."
    r")",
    re.IGNORECASE,
)

# 末尾像已回到 shell 提示符或明确完成
_COMPLETE_TAIL_RE = re.compile(
    r"(?:"
    r"[\r\n][^\r\n]{0,120}[$#]\s*$|"
    r"\b(?:done|Done|SUCCESS|Successfully|Complete|completed|Build succeeded|"
    r"100%\s*\||Finished|All done|exit code:?\s*0)\b"
    r")",
    re.IGNORECASE,
)


def _clamp_poll(seconds: int) -> int:
    return max(_POLL_MIN, min(_POLL_MAX, int(seconds)))


def infer_poll_from_send_text(text: str | None) -> int:
    """根据 send_to_terminal 文本推断等待秒数；无需等待则返回 0。"""
    raw = (text or "").strip()
    if not raw or _CTRL_ONLY_RE.match(raw):
        return 0
    if _SUDO_RE.search(raw) and not _LONG_CMD_RE.search(raw):
        return _clamp_poll(_POLL_SHORT)
    if _LONG_CMD_RE.search(raw):
        return _clamp_poll(_POLL_DEFAULT)
    # 单行极短查询类
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1 and _QUICK_CMD_RE.match(lines[0]) and "|" not in lines[0]:
        return 0
    # 含管道且像会跑一阵（未命中 LONG 但含 install/build 等）
    if re.search(r"\b(install|build|pull|upgrade|download|clone)\b", raw, re.I):
        return _clamp_poll(_POLL_DEFAULT)
    return 0


def infer_poll_from_buffer(buffer: str | None) -> int:
    """根据 buffer 末尾是否仍在进行推断等待；已完成则 0。"""
    buf = buffer or ""
    if not buf.strip():
        return 0
    tail = buf[-4000:] if len(buf) > 4000 else buf
    if _COMPLETE_TAIL_RE.search(tail):
        return 0
    if _PROGRESS_RE.search(tail):
        return _clamp_poll(_POLL_PROGRESS)
    return 0


def resolve_terminal_poll_seconds(
    *,
    explicit: int | None,
    send_hint: int = 0,
    buffer: str | None = None,
) -> int:
    """
    合并模型显式值、发命令启发式、buffer 启发式。
    buffer 已回到提示符/完成态时强制 0（忽略模型显式 next_poll，避免空等）。
    未完成时：显式 > 0 与启发式取 max（模型可拉长）。
    """
    exp = 0
    if explicit is not None:
        try:
            exp = _clamp_poll(int(explicit))
        except (TypeError, ValueError):
            exp = 0

    buf_poll = infer_poll_from_buffer(buffer)
    send_poll = max(0, int(send_hint or 0))
    tail = (buffer or "")[-2000:]

    if buf_poll == 0 and _COMPLETE_TAIL_RE.search(tail):
        # 输出已稳定：强制 0，不再被模型显式 N 拉长
        return 0

    auto = max(send_poll, buf_poll)
    if exp > 0:
        return min(_POLL_MAX_AUTO, max(exp, auto))
    return min(auto, _POLL_MAX_AUTO) if auto > 0 else 0


@dataclass
class TerminalPollBatchState:
    """单轮 tool_calls 批次内的终端轮询状态。"""

    send_hint: int = 0
    had_get_buffer_after_send: bool = False
    last_send_text: str = ""

    def on_send_to_terminal(self, text: str, *, success: bool) -> None:
        if not success:
            return
        self.last_send_text = text or ""
        hint = infer_poll_from_send_text(text)
        if hint > 0:
            self.send_hint = max(self.send_hint, hint)
        self.had_get_buffer_after_send = False

    def on_get_terminal_buffer(
        self,
        fn_args: dict,
        result_obj: dict,
    ) -> tuple[int, dict]:
        """
        更新 result_obj（可能写入 next_poll_in_seconds / auto_poll_in_seconds），
        返回本轮应安排的等待秒数。
        """
        self.had_get_buffer_after_send = True
        explicit = fn_args.get("next_poll_in_seconds")
        if explicit is None:
            explicit = result_obj.get("next_poll_in_seconds")
        try:
            explicit_int = int(explicit) if explicit is not None else None
        except (TypeError, ValueError):
            explicit_int = None

        buf = result_obj.get("buffer") if isinstance(result_obj.get("buffer"), str) else ""
        poll = resolve_terminal_poll_seconds(
            explicit=explicit_int,
            send_hint=self.send_hint,
            buffer=buf,
        )
        if poll > 0 and explicit_int is None:
            result_obj["next_poll_in_seconds"] = poll
            result_obj["auto_poll_in_seconds"] = poll
        elif poll > 0 and explicit_int is not None and poll > explicit_int:
            result_obj["auto_poll_in_seconds"] = poll
        return poll, result_obj

    def trailing_send_poll(self) -> int:
        """本批只有 send、没有 get_terminal_buffer 时，在批次末尾补等待。"""
        if self.had_get_buffer_after_send or self.send_hint <= 0:
            return 0
        # 长命令 hint 上限压到 DEFAULT，避免只 send 就空等过久
        return min(self.send_hint, _POLL_DEFAULT)


def infer_poll_from_ssh_result(result_obj: dict) -> int:
    """ssh_execute detach / poll_log 结果推断下一轮等待秒数。"""
    if not result_obj.get("success"):
        return 0
    if result_obj.get("detached"):
        return _clamp_poll(_POLL_DEFAULT)
    if result_obj.get("poll_log") and result_obj.get("job_running"):
        tail = (result_obj.get("log_tail") or result_obj.get("stdout") or "")
        buf_poll = infer_poll_from_buffer(tail)
        return buf_poll if buf_poll > 0 else _clamp_poll(_POLL_PROGRESS)
    return 0


# ssh_channel 读/轮询：显式短等待（不做 send/buffer 自动推断）
SSH_CHANNEL_WAIT_MAX = 30
_SSH_CHANNEL_WAIT_TOOLS = frozenset(
    {
        "ssh_channel_read_lines",
        "ssh_channel_read_length",
        "ssh_channel_has_new",
    }
)


def clamp_ssh_channel_wait_seconds(value) -> int:
    """钳制 ssh_channel wait_seconds：非法/缺失→0，范围 0～30。"""
    if value is None or value is False:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(SSH_CHANNEL_WAIT_MAX, n))


def attach_ssh_channel_wait_fields(payload: dict, arguments: dict | None) -> dict:
    """成功读通道后写入 wait_seconds / next_poll_in_seconds（仅 >0）。"""
    wait = clamp_ssh_channel_wait_seconds((arguments or {}).get("wait_seconds"))
    if wait > 0:
        payload["wait_seconds"] = wait
        payload["next_poll_in_seconds"] = wait
    return payload


def apply_terminal_poll_tool_result(
    state: TerminalPollBatchState,
    fn_name: str,
    fn_args: dict | None,
    result_obj: dict,
    *,
    success: bool,
) -> tuple[int, dict]:
    """处理单个工具结果，返回 (poll_seconds, mutated_result_obj)。"""
    args = fn_args or {}
    if fn_name == "send_to_terminal" and success:
        state.on_send_to_terminal(str(args.get("text") or ""), success=True)
        return 0, result_obj
    if fn_name == "get_terminal_buffer" and success:
        # 工具内已按 until_contains 等到结束 → 不再 batch 末二次 sleep
        if result_obj.get("until_wait_done") or result_obj.get("wait_done_in_tool"):
            result_obj.pop("next_poll_in_seconds", None)
            return 0, result_obj
        poll, result_obj = state.on_get_terminal_buffer(args, result_obj)
        return poll, result_obj
    if fn_name == "ssh_execute" and success:
        poll = infer_poll_from_ssh_result(result_obj)
        if poll > 0 and not result_obj.get("next_poll_in_seconds"):
            result_obj["auto_poll_in_seconds"] = poll
            result_obj["next_poll_in_seconds"] = poll
        return poll, result_obj
    if fn_name in _SSH_CHANNEL_WAIT_TOOLS and success:
        if result_obj.get("until_wait_done") or result_obj.get("wait_done_in_tool"):
            result_obj.pop("next_poll_in_seconds", None)
            result_obj.pop("wait_seconds", None)
            return 0, result_obj
        wait = clamp_ssh_channel_wait_seconds(
            args.get("wait_seconds")
            if args.get("wait_seconds") is not None
            else result_obj.get("wait_seconds")
        )
        if wait > 0:
            result_obj["wait_seconds"] = wait
            result_obj["next_poll_in_seconds"] = wait
        return wait, result_obj
    return 0, result_obj
