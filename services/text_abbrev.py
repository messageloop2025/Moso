"""文本省略策略：终端/命令行/日志以末尾为主；文件/配置类内容以开头为主。"""

from __future__ import annotations


def abbreviate_lines_tail_only(
    text: str,
    *,
    max_lines: int = 40,
    full_output: bool = False,
) -> tuple[str, bool, int]:
    """仅保留最后 max_lines 行（不保留开头）。返回 (文本, 是否已省略, 总行数)。"""
    if not text:
        return text, False, 0
    lines = text.splitlines()
    total = len(lines)
    if full_output or total <= max_lines:
        return text, False, total
    tail_part = lines[-max_lines:]
    omitted = total - len(tail_part)
    prefix = (
        f"... (已省略最早 {omitted} 行，共 {total} 行；仅显示最后 {len(tail_part)} 行。"
        f" 若需完整内容或开头上下文请传 full_output=true 或 tail_only=false) ...\n\n"
    )
    return prefix + "\n".join(tail_part), True, total


def abbreviate_lines_tail_focus(
    text: str,
    *,
    max_lines: int = 35,
    head_lines: int = 2,
    full_output: bool = False,
    omit_hint: str = "终端/日志以末尾为准",
) -> tuple[str, bool, int]:
    """按行省略：保留少量开头 + 大量末尾。返回 (文本, 是否已省略, 总行数)。"""
    if not text:
        return text, False, 0
    lines = text.splitlines()
    total = len(lines)
    if full_output or total <= max_lines:
        return text, False, total
    head_n = max(0, min(head_lines, total))
    tail_n = max(1, max_lines - head_n)
    if head_n + tail_n >= total:
        return text, False, total
    head_part = lines[:head_n]
    tail_part = lines[-tail_n:]
    omitted = total - head_n - tail_n
    mid = (
        f"\n\n... (中间省略 {omitted} 行，共 {total} 行；{omit_hint}。"
        f" 若需完整内容请传 full_output=true) ...\n\n"
    )
    body = "\n".join(head_part) + mid + "\n".join(tail_part)
    return body, True, total


def abbreviate_terminal_buffer(
    text: str,
    *,
    full_output: bool = False,
    tail_only: bool = True,
    max_lines: int = 40,
) -> tuple[str, bool, int, str]:
    """终端 buffer 省略入口。返回 (文本, 是否已省略, 总行数, 省略说明)。"""
    max_lines = max(10, min(200, int(max_lines or 40)))
    if tail_only:
        body, abbr, total = abbreviate_lines_tail_only(text, max_lines=max_lines, full_output=full_output)
        note = (
            f"输出共 {total} 行，仅返回最后 {min(max_lines, total)} 行（tail_only）。"
            " full_output=true 取全量；tail_only=false 则保留前 2 行 + 后 33 行。"
        )
        return body, abbr, total, note
    body, abbr, total = abbreviate_lines_tail_focus(text, full_output=full_output)
    note = (
        f"输出共 {total} 行，已省略中间，保留前 2 行与后 33 行（tail_only=false）。"
        " full_output=true 取全量；日常轮询建议 tail_only=true（默认）。"
    )
    return body, abbr, total, note


def abbreviate_text_tail_focus(text: str, max_chars: int, *, head_ratio: float = 0.1) -> str:
    """字符级省略：终端/日志/stdout/stderr，优先保留末尾。"""
    if not text or len(text) <= max_chars:
        return text
    head_ratio = max(0.05, min(0.25, head_ratio))
    omit_tpl = "\n…（中间省略 {n} 字符；终端/命令输出以末尾为准）…\n"
    omit_reserve = 64
    usable = max(256, max_chars - omit_reserve)
    head_keep = max(128, int(usable * head_ratio))
    tail_keep = max(256, usable - head_keep)
    if head_keep + tail_keep + omit_reserve > max_chars:
        tail_keep = max(256, max_chars - head_keep - omit_reserve)
    omitted = len(text) - head_keep - tail_keep
    if omitted <= 0:
        return text[:max_chars] + "…（已截断）"
    return text[:head_keep] + omit_tpl.format(n=omitted) + text[-tail_keep:]


def abbreviate_text_head_focus(text: str, max_chars: int, *, tail_ratio: float = 0.1) -> str:
    """字符级省略：文件/配置类内容，优先保留开头。"""
    if not text or len(text) <= max_chars:
        return text
    tail_ratio = max(0.05, min(0.25, tail_ratio))
    omit_tpl = "\n…（中间省略 {n} 字符；文件内容以开头为准，需看尾部请用 read_chat_data mode=tail 或 offset）…\n"
    omit_reserve = 96
    usable = max(256, max_chars - omit_reserve)
    tail_keep = max(128, int(usable * tail_ratio))
    head_keep = max(256, usable - tail_keep)
    if head_keep + tail_keep + omit_reserve > max_chars:
        head_keep = max(256, max_chars - tail_keep - omit_reserve)
    omitted = len(text) - head_keep - tail_keep
    if omitted <= 0:
        return text[:max_chars] + "…（已截断）"
    return text[:head_keep] + omit_tpl.format(n=omitted) + text[-tail_keep:]
