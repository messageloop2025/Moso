"""输出条件等待：在超时内轮询，直到命中 until_contains，或超时/唤醒/中止。"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

# 首次检查时附带的「近期尾部」窗口，覆盖调用时已出现在屏上的 password:/标记
_INITIAL_TAIL_CHARS = 8192
_DEFAULT_INTERVAL = 0.5
_MIN_INTERVAL = 0.2
_MAX_INTERVAL = 2.0


def normalize_until_contains(value: Any) -> str:
    """规范化 until_contains；空串视为未启用。"""
    if value is None:
        return ""
    return str(value).strip()


def clamp_until_wait_seconds(value: Any, *, default: int, max_sec: int) -> int:
    """until 等待超时秒数：非法则用 default，范围 1～max_sec。"""
    if value is None:
        return max(1, min(max_sec, int(default)))
    try:
        n = int(value)
    except (TypeError, ValueError):
        return max(1, min(max_sec, int(default)))
    return max(1, min(max_sec, n))


def find_literal_match(haystack: str, needle: str) -> str | None:
    """字面量子串匹配；命中时返回不超过 120 字符的片段。"""
    if not needle or not haystack:
        return None
    idx = haystack.find(needle)
    if idx < 0:
        return None
    start = max(0, idx - 20)
    end = min(len(haystack), idx + len(needle) + 40)
    snippet = haystack[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(haystack):
        snippet = snippet + "…"
    return snippet


async def _check_runtime_abort(session_id: int | None) -> str | None:
    """返回 abort reason，或 None 表示继续。wake→woken；stop/pause→对应 reason。"""
    if session_id is None:
        return None
    try:
        from api.ai_agent import _pull_runtime_control_nowait

        ctrl = await _pull_runtime_control_nowait(session_id)
    except Exception:
        return None
    if not isinstance(ctrl, dict):
        return None
    act = (ctrl.get("action") or "").strip().lower()
    if act == "wake":
        return "woken"
    if act == "stop":
        return "user_stop"
    if act == "pause":
        return "user_pause"
    if act == "supplement":
        return "supplement"
    return None


async def poll_until_contains(
    *,
    fetch_raw: Callable[[], Awaitable[tuple[str, dict]]],
    needle: str,
    timeout_sec: float,
    interval_sec: float = _DEFAULT_INTERVAL,
    session_id: int | None = None,
    match_mode: str = "delta",
) -> tuple[str, str | None, str, dict]:
    """
    轮询直到 needle 出现，或超时/被 runtime 打断。

    match_mode:
      - delta: 仅匹配自调用起新增文本（适合增长的终端 buffer）
      - full: 每次在返回的整段文本中匹配（适合通道滑动 tail+pending）
    """
    needle = normalize_until_contains(needle)
    if not needle:
        text, meta = await fetch_raw()
        return "timeout", None, text or "", meta or {}

    timeout_sec = max(0.2, float(timeout_sec))
    interval_sec = max(_MIN_INTERVAL, min(_MAX_INTERVAL, float(interval_sec)))
    use_full = (match_mode or "delta").strip().lower() == "full"
    t0 = time.monotonic()

    text0, meta0 = await fetch_raw()
    text0 = text0 or ""
    meta0 = meta0 or {}
    baseline = len(text0)

    initial_window = text0 if use_full else (text0[-_INITIAL_TAIL_CHARS:] if text0 else "")
    hit = find_literal_match(initial_window, needle)
    if hit:
        return "matched", hit, text0, meta0

    last_text, last_meta = text0, meta0
    while True:
        abort = await _check_runtime_abort(session_id)
        if abort:
            return abort, None, last_text, last_meta

        elapsed = time.monotonic() - t0
        if elapsed >= timeout_sec:
            return "timeout", None, last_text, last_meta

        await asyncio.sleep(min(interval_sec, max(0.05, timeout_sec - elapsed)))

        abort = await _check_runtime_abort(session_id)
        if abort:
            return abort, None, last_text, last_meta

        text, meta = await fetch_raw()
        text = text or ""
        meta = meta or {}
        last_text, last_meta = text, meta

        if use_full:
            search = text
        elif len(text) >= baseline:
            search = text[baseline:]
        else:
            search = text
            baseline = 0

        hit = find_literal_match(search, needle)
        if hit:
            return "matched", hit, text, meta
