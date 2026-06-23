"""SSH Channel 管理器：TTY 通道、行缓冲（标准行宽与软换行）、超时与自动关闭

- 每个 channel 对应一个 Paramiko PTY 会话与一个读线程，输出按行缓冲（FIFO，默认 1000 行）。
- 每行不超过标准长度（默认 120），超长软换行并标记 is_soft_wrap。
- 支持输入/输出/空闲超时自动关闭。
"""
import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import paramiko

from services.paramiko_banner_fix import patch_banner_encoding, unpatch_banner_encoding
from services.ssh_key_loader import load_private_key_pem
from services.ssh_shell import open_shell_session
from services.terminal_input import expand_control_keys, is_control_only

logger = logging.getLogger("edgeops.ssh_channel_manager")

DEFAULT_LINE_WIDTH = 120
DEFAULT_MAX_LINES = 1000


def _wrap_line(text: str, width: int) -> list[tuple[str, bool]]:
    """将一行按 width 软换行，返回 [(content, is_soft_wrap), ...]。"""
    if width <= 0:
        return [(text, False)] if text else []
    out = []
    while text:
        if len(text) <= width:
            out.append((text, False))
            break
        out.append((text[:width], True))
        text = text[width:]
    return out


@dataclass
class LineEntry:
    line_no: int
    content: str
    is_soft_wrap: bool


@dataclass
class ChannelState:
    channel_id: int
    client: paramiko.SSHClient
    channel: paramiko.Channel
    lock: threading.Lock = field(default_factory=threading.Lock)
    lines: deque = field(default_factory=deque)  # deque of LineEntry
    next_line_no: int = 1
    max_lines: int = DEFAULT_MAX_LINES
    line_width: int = DEFAULT_LINE_WIDTH
    _recv_buffer: str = ""  # 未满一行的剩余数据
    last_input_time: float = field(default_factory=time.monotonic)
    last_output_time: float = field(default_factory=time.monotonic)
    idle_close_sec: Optional[int] = 1800
    input_timeout_sec: Optional[int] = None
    output_timeout_sec: Optional[int] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    reader_thread: Optional[threading.Thread] = None
    _closed: bool = False

    @property
    def oldest_line_no(self) -> int:
        with self.lock:
            if not self.lines:
                return 0
            return self.lines[0].line_no

    @property
    def latest_line_no(self) -> int:
        with self.lock:
            if not self.lines:
                return 0
            return self.lines[-1].line_no

    def append_output(self, text: str) -> None:
        if not text:
            return
        self.last_output_time = time.monotonic()
        self._recv_buffer += text
        with self.lock:
            while "\n" in self._recv_buffer or "\r" in self._recv_buffer:
                line, sep, rest = self._recv_buffer.partition("\n")
                if not sep:
                    line, sep, rest = self._recv_buffer.partition("\r")
                if not sep:
                    break
                self._recv_buffer = rest
                line = line.rstrip("\n\r")
                for content, is_soft in _wrap_line(line, self.line_width):
                    self.lines.append(LineEntry(self.next_line_no, content, is_soft))
                    self.next_line_no += 1
            while len(self.lines) > self.max_lines:
                self.lines.popleft()

    def get_lines(
        self,
        from_line: Optional[int] = None,
        to_line: Optional[int] = None,
        last_n: Optional[int] = None,
        since_line: Optional[int] = None,
    ) -> tuple[list[dict], int, int]:
        with self.lock:
            if not self.lines:
                return [], 0, 0
            oldest = self.lines[0].line_no
            latest = self.lines[-1].line_no
            if last_n is not None:
                n = max(1, min(last_n, len(self.lines)))
                selected = list(self.lines)[-n:]
            elif since_line is not None:
                selected = [e for e in self.lines if e.line_no > since_line]
            elif from_line is not None or to_line is not None:
                f = from_line if from_line is not None else oldest
                t = to_line if to_line is not None else latest
                selected = [e for e in self.lines if f <= e.line_no <= t]
            else:
                selected = list(self.lines)
            result = [{"line_no": e.line_no, "content": e.content, "is_soft_wrap": e.is_soft_wrap} for e in selected]
            return result, oldest, latest

    def get_content_length(self, max_chars: int) -> tuple[str, int, int]:
        with self.lock:
            if not self.lines:
                return "", 0, 0
            oldest = self.lines[0].line_no
            latest = self.lines[-1].line_no
            total = "".join(e.content for e in self.lines)
            if len(total) > max_chars:
                total = total[-max_chars:]
            return total, oldest, latest

    def has_new(self, after_line: int) -> tuple[bool, int]:
        with self.lock:
            latest = self.lines[-1].line_no if self.lines else 0
            return latest > after_line, latest

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        try:
            self.channel.close()
        except Exception as e:
            logger.debug("channel close: %s", e)
        try:
            self.client.close()
        except Exception as e:
            logger.debug("client close: %s", e)


class SSHChannelManager:
    """单例：管理所有 TTY 通道的状态与行缓冲。"""

    _instance: Optional["SSHChannelManager"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._channels: dict[int, ChannelState] = {}
        self._channels_lock = threading.Lock()
        self._pending_db_close: set[int] = set()
        self._pending_lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()

    @classmethod
    def get_instance(cls) -> "SSHChannelManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _reader_loop(self, state: ChannelState) -> None:
        try:
            while not state.stop_event.is_set():
                try:
                    data = state.channel.recv(4096)
                except Exception as e:
                    if not state.stop_event.is_set():
                        logger.debug("channel recv: %s", e)
                    break
                if not data:
                    break
                try:
                    text = data.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                if text:
                    state.append_output(text)
                # 输出超时：若设置了 output_timeout_sec 且距上次输出已超时，则关闭
                if state.output_timeout_sec and (time.monotonic() - state.last_output_time) >= state.output_timeout_sec:
                    # 在 recv 阻塞期间无法检测；这里用“收到数据后”的 last_output_time 由 watchdog 统一检查
                    pass
        except Exception as e:
            logger.warning("SSH channel reader error channel_id=%s: %s", state.channel_id, e)
        finally:
            if not state._closed:
                state.close()
                with self._channels_lock:
                    self._channels.pop(state.channel_id, None)

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(10):
            now = time.monotonic()
            to_close = []
            with self._channels_lock:
                for cid, state in list(self._channels.items()):
                    if state._closed:
                        to_close.append(cid)
                        continue
                    last_activity = max(state.last_input_time, state.last_output_time)
                    if state.idle_close_sec and (now - last_activity) >= state.idle_close_sec:
                        to_close.append(cid)
                    elif state.input_timeout_sec and (now - state.last_input_time) >= state.input_timeout_sec:
                        to_close.append(cid)
                    elif state.output_timeout_sec and (now - state.last_output_time) >= state.output_timeout_sec:
                        to_close.append(cid)
            for cid in to_close:
                self.close_channel(cid)

    def _start_watchdog_if_needed(self) -> None:
        with self._channels_lock:
            if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
                self._watchdog_stop.clear()
                self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
                self._watchdog_thread.start()

    def drain_pending_db_close_ids(self) -> list[int]:
        with self._pending_lock:
            if not self._pending_db_close:
                return []
            ids = list(self._pending_db_close)
            self._pending_db_close.clear()
            return ids

    def _mark_pending_db_close(self, channel_id: int) -> None:
        with self._pending_lock:
            self._pending_db_close.add(channel_id)

    async def open_channel(
        self,
        channel_id: int,
        host: str,
        port: int,
        auth: dict,
        max_lines: int = DEFAULT_MAX_LINES,
        line_width: int = DEFAULT_LINE_WIDTH,
        idle_close_sec: Optional[int] = 1800,
        input_timeout_sec: Optional[int] = None,
        output_timeout_sec: Optional[int] = None,
    ) -> Optional[str]:
        """建立 TTY 连接并启动读线程。成功返回 None，失败返回错误信息。"""
        def _connect() -> Optional[ChannelState]:
            try:
                client, channel = open_shell_session(
                    host=host,
                    port=port,
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    timeout=30,
                )
            except Exception as e:
                logger.warning("SSH channel open failed channel_id=%s: %s", channel_id, e)
                return None
            state = ChannelState(
                channel_id=channel_id,
                client=client,
                channel=channel,
                max_lines=max_lines,
                line_width=line_width,
                idle_close_sec=idle_close_sec,
                input_timeout_sec=input_timeout_sec,
                output_timeout_sec=output_timeout_sec,
            )
            state.reader_thread = threading.Thread(
                target=self._reader_loop,
                args=(state,),
                daemon=True,
            )
            state.reader_thread.start()
            with self._channels_lock:
                self._channels[channel_id] = state
            self._start_watchdog_if_needed()
            return state

        try:
            state = await asyncio.to_thread(_connect)
        except Exception as e:
            return str(e)
        if state is None:
            return "SSH 连接失败"
        return None

    def send(self, channel_id: int, content: str) -> Optional[str]:
        """向通道发送内容（含控制字符）。成功返回 None，失败返回错误信息。"""
        with self._channels_lock:
            state = self._channels.get(channel_id)
        if not state or state._closed:
            return "通道不存在或已关闭"
        try:
            data = expand_control_keys(content)
            if not is_control_only(data) and not data.endswith("\n") and data.strip():
                data += "\n"
            state.channel.send(data.encode("utf-8", errors="replace"))
        except Exception as e:
            logger.warning("channel send error: %s", e)
            return str(e)
        state.last_input_time = time.monotonic()
        return None

    def get_lines(
        self,
        channel_id: int,
        from_line: Optional[int] = None,
        to_line: Optional[int] = None,
        last_n: Optional[int] = None,
        since_line: Optional[int] = None,
    ) -> Optional[tuple[list[dict], int, int]]:
        with self._channels_lock:
            state = self._channels.get(channel_id)
        if not state or state._closed:
            return None
        return state.get_lines(from_line=from_line, to_line=to_line, last_n=last_n, since_line=since_line)

    def get_content_length(self, channel_id: int, max_chars: int) -> Optional[tuple[str, int, int]]:
        with self._channels_lock:
            state = self._channels.get(channel_id)
        if not state or state._closed:
            return None
        return state.get_content_length(max_chars)

    def has_new(self, channel_id: int, after_line: int = 0) -> Optional[tuple[bool, int]]:
        with self._channels_lock:
            state = self._channels.get(channel_id)
        if not state or state._closed:
            return None
        return state.has_new(after_line)

    def get_line_range(self, channel_id: int) -> Optional[tuple[int, int]]:
        with self._channels_lock:
            state = self._channels.get(channel_id)
        if not state or state._closed:
            return None
        return state.oldest_line_no, state.latest_line_no

    def close_channel(self, channel_id: int) -> None:
        with self._channels_lock:
            state = self._channels.get(channel_id)
        if state:
            state.close()
            with self._channels_lock:
                self._channels.pop(channel_id, None)
        self._mark_pending_db_close(channel_id)

    def has_channel(self, channel_id: int) -> bool:
        with self._channels_lock:
            state = self._channels.get(channel_id)
        return state is not None and not state._closed
