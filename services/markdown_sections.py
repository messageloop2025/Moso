"""Markdown 章节解析：目录清单、按节读取（可限长）、定点替换。

供 AI 在有限上下文中渐进加载 web/fs、aihelp、Skill 等 .md 文件。
仅识别 ATX 标题（# … ######），并跳过 fenced code block 内的伪标题行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import config

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+\s*)?$")
_FENCE_OPEN_RE = re.compile(r"^(```+|~~~+)")


@dataclass(frozen=True)
class MarkdownHeading:
    """文档中的一个 ATX 标题节点。"""

    index: int
    level: int
    title: str
    line: int  # 0-based
    path: tuple[str, ...]


def _max_file_chars() -> int:
    return int(getattr(config, "MARKDOWN_SECTIONS_MAX_FILE_CHARS", 2_000_000))


def _default_max_chars() -> int:
    return int(getattr(config, "MARKDOWN_SECTIONS_MAX_CHARS", 32_000))


def _clamp_max_chars(value: int | None) -> int:
    default = _default_max_chars()
    hard_max = int(getattr(config, "MARKDOWN_SECTIONS_MAX_CHARS_HARD", 200_000))
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(64, min(n, hard_max))


def _line_offsets(text: str) -> list[int]:
    """每行起始字符偏移（0-based line index）。"""
    offsets = [0]
    for m in re.finditer("\n", text):
        offsets.append(m.end())
    return offsets


def parse_headings(text: str) -> list[MarkdownHeading]:
    """解析全文标题（跳过代码围栏内行）。"""
    if len(text) > _max_file_chars():
        raise ValueError(f"文档超过章节解析上限（{_max_file_chars()} 字符）")
    lines = text.split("\n")
    in_fence = False
    fence_marker = ""
    raw: list[tuple[int, int, str]] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        fm = _FENCE_OPEN_RE.match(stripped)
        if fm:
            marker = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[:3]
            elif stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        hm = _HEADING_RE.match(line)
        if not hm:
            continue
        level = len(hm.group(1))
        title = hm.group(2).strip()
        if title:
            raw.append((i, level, title))

    stack: list[str] = []
    out: list[MarkdownHeading] = []
    for idx, (line_no, level, title) in enumerate(raw):
        while stack and len(stack) >= level:
            stack.pop()
        stack.append(title)
        out.append(
            MarkdownHeading(
                index=idx,
                level=level,
                title=title,
                line=line_no,
                path=tuple(stack),
            )
        )
    return out


def _section_end_line(headings: list[MarkdownHeading], pos: int, lines_count: int) -> int:
    """章节含所有子节：到下一同级或更高级标题行（不含该行）。"""
    level = headings[pos].level
    for j in range(pos + 1, len(headings)):
        if headings[j].level <= level:
            return headings[j].line
    return lines_count


def _section_body_end_line(headings: list[MarkdownHeading], pos: int, lines_count: int) -> int:
    """章节仅直属正文：到第一个子标题或同级/更高级标题（不含该行）。"""
    level = headings[pos].level
    for j in range(pos + 1, len(headings)):
        if headings[j].level > level or headings[j].level <= level:
            return headings[j].line
    return lines_count


def _slice_by_lines(text: str, start_line: int, end_line: int) -> str:
    lines = text.split("\n")
    chunk = lines[start_line:end_line]
    if not chunk:
        return ""
    return "\n".join(chunk)


def _truncate_text(s: str, max_chars: int) -> tuple[str, bool, int]:
    n = len(s)
    if n <= max_chars:
        return s, False, n
    return s[:max_chars], True, n


def list_markdown_sections(
    text: str,
    *,
    max_level: int = 6,
    include_preamble: bool = False,
) -> dict[str, Any]:
    """返回章节清单。max_level 限制展示到的标题级别（1–6）。"""
    try:
        max_level = int(max_level)
    except (TypeError, ValueError):
        max_level = 6
    max_level = max(1, min(6, max_level))

    headings = parse_headings(text)
    lines_count = len(text.split("\n"))
    offsets = _line_offsets(text)
    sections: list[dict[str, Any]] = []

    if include_preamble and headings:
        first_line = headings[0].line
        if first_line > 0:
            end_line = first_line
            start_char = 0
            end_char = offsets[end_line] if end_line < len(offsets) else len(text)
            sections.append(
                {
                    "index": -1,
                    "level": 0,
                    "title": "(preamble)",
                    "path": [],
                    "line": 1,
                    "line_end": end_line,
                    "char_start": start_char,
                    "char_end": end_char,
                }
            )
    elif include_preamble and not headings and text.strip():
        sections.append(
            {
                "index": -1,
                "level": 0,
                "title": "(preamble)",
                "path": [],
                "line": 1,
                "line_end": lines_count,
                "char_start": 0,
                "char_end": len(text),
            }
        )

    for h in headings:
        if h.level > max_level:
            continue
        end_line = _section_end_line(headings, h.index, lines_count)
        start_char = offsets[h.line] if h.line < len(offsets) else len(text)
        end_char = offsets[end_line] if end_line < len(offsets) else len(text)
        sections.append(
            {
                "index": h.index,
                "level": h.level,
                "title": h.title,
                "path": list(h.path),
                "line": h.line + 1,
                "line_end": end_line,
                "char_start": start_char,
                "char_end": end_char,
                "has_children": any(
                    headings[j].level > h.level
                    for j in range(h.index + 1, len(headings))
                    if headings[j].line < end_line
                ),
            }
        )

    return {
        "section_count": len([h for h in headings if h.level <= max_level]),
        "heading_count": len(headings),
        "max_level": max_level,
        "sections": sections,
    }


def _resolve_heading(
    headings: list[MarkdownHeading],
    *,
    section_index: int | None = None,
    section_path: list[str] | None = None,
    heading: str | None = None,
    case_insensitive: bool = False,
) -> MarkdownHeading:
    if not headings and section_index != -1:
        raise ValueError("文档中没有 ATX 标题（# …）")

    if section_index is not None:
        try:
            idx = int(section_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("section_index 须为整数") from exc
        if idx == -1:
            raise ValueError("section_index=-1 仅用于 preamble，请设 include_preamble 后按 char 范围读取")
        if idx < 0 or idx >= len(headings):
            raise ValueError(f"section_index 越界（0..{len(headings) - 1}）")
        return headings[idx]

    if section_path:
        path = [str(p).strip() for p in section_path if str(p).strip()]
        if not path:
            raise ValueError("section_path 不能为空")
        matches = [h for h in headings if list(h.path) == path]
        if not matches:
            raise ValueError(f"未找到路径 {' / '.join(path)}")
        if len(matches) > 1:
            raise ValueError(f"路径 {' / '.join(path)} 不唯一，请改用 section_index")
        return matches[0]

    if heading is not None:
        key = heading.strip()
        if not key:
            raise ValueError("heading 不能为空")
        if case_insensitive:
            key_l = key.lower()
            matches = [h for h in headings if h.title.lower() == key_l]
        else:
            matches = [h for h in headings if h.title == key]
        if not matches:
            raise ValueError(f"未找到标题「{key}」")
        if len(matches) > 1:
            raise ValueError(f"标题「{key}」出现 {len(matches)} 次，请用 section_path 或 section_index")
        return matches[0]

    raise ValueError("须指定 section_index、section_path 或 heading 之一")


def get_markdown_section(
    text: str,
    *,
    section_index: int | None = None,
    section_path: list[str] | None = None,
    heading: str | None = None,
    case_insensitive: bool = False,
    max_chars: int | None = None,
    include_heading: bool = True,
    include_children: bool = True,
) -> dict[str, Any]:
    """读取指定章节正文。include_children=false 时仅到下一同级/更高级标题前。"""
    max_chars = _clamp_max_chars(max_chars)
    headings = parse_headings(text)
    target = _resolve_heading(
        headings,
        section_index=section_index,
        section_path=section_path,
        heading=heading,
        case_insensitive=case_insensitive,
    )
    lines = text.split("\n")
    lines_count = len(lines)
    start_line = target.line
    if include_children:
        end_line = _section_end_line(headings, target.index, lines_count)
    else:
        end_line = _section_body_end_line(headings, target.index, lines_count)

    body_start = start_line if include_heading else min(start_line + 1, lines_count)
    chunk = _slice_by_lines(text, body_start, end_line)
    content, truncated, total_chars = _truncate_text(chunk, max_chars)

    return {
        "index": target.index,
        "level": target.level,
        "title": target.title,
        "path": list(target.path),
        "line_start": start_line + 1,
        "line_end": end_line,
        "include_heading": include_heading,
        "include_children": include_children,
        "content": content,
        "truncated": truncated,
        "total_chars": total_chars,
        "returned_chars": len(content),
    }


def replace_markdown_section(
    text: str,
    new_content: str,
    *,
    section_index: int | None = None,
    section_path: list[str] | None = None,
    heading: str | None = None,
    case_insensitive: bool = False,
    mode: str = "replace_body",
) -> dict[str, Any]:
    """定点替换章节。replace_body：保留原标题行，替换其下正文；replace_all：替换标题+正文整段。"""
    mode_val = (mode or "replace_body").strip().lower()
    if mode_val not in ("replace_body", "replace_all"):
        raise ValueError("mode 须为 replace_body 或 replace_all")

    headings = parse_headings(text)
    target = _resolve_heading(
        headings,
        section_index=section_index,
        section_path=section_path,
        heading=heading,
        case_insensitive=case_insensitive,
    )
    lines_count = len(text.split("\n"))
    if mode_val == "replace_body":
        end_line = _section_body_end_line(headings, target.index, lines_count)
    else:
        end_line = _section_end_line(headings, target.index, lines_count)
    lines = text.split("\n")

    new_body = (new_content or "").replace("\r\n", "\n").replace("\r", "\n")
    new_lines = new_body.split("\n") if new_body else []

    if mode_val == "replace_body":
        merged_lines = lines[: target.line + 1] + new_lines + lines[end_line:]
    else:
        merged_lines = lines[:target.line] + new_lines + lines[end_line:]

    new_text = "\n".join(merged_lines)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"

    return {
        "index": target.index,
        "title": target.title,
        "path": list(target.path),
        "mode": mode_val,
        "line_start": target.line + 1,
        "line_end": end_line,
        "replaced_line_count": end_line - (target.line + (0 if mode_val == "replace_all" else 1)),
        "content": new_text,
        "char_length": len(new_text),
    }


def _section_query_specified(
    *,
    section_index: int | None,
    section_path: list[str] | None,
    heading: str | None,
) -> bool:
    return (
        section_index is not None
        or bool(section_path)
        or (heading is not None and str(heading).strip() != "")
    )


def read_markdown_document(
    text: str,
    *,
    sections_only: bool = False,
    max_level: int = 6,
    include_preamble: bool = False,
    section_index: int | None = None,
    section_path: list[str] | None = None,
    heading: str | None = None,
    case_insensitive: bool = False,
    max_chars: int | None = None,
    include_heading: bool = True,
    include_children: bool = True,
) -> dict[str, Any]:
    """统一读取：仅目录 / 指定章节 / 全文（可截断）。"""
    if _section_query_specified(
        section_index=section_index,
        section_path=section_path,
        heading=heading,
    ):
        out = get_markdown_section(
            text,
            section_index=section_index,
            section_path=section_path,
            heading=heading,
            case_insensitive=case_insensitive,
            max_chars=max_chars,
            include_heading=include_heading,
            include_children=include_children,
        )
        out["mode"] = "section"
        return out
    if sections_only:
        out = list_markdown_sections(
            text,
            max_level=max_level,
            include_preamble=include_preamble,
        )
        out["mode"] = "sections"
        return out
    limit = _clamp_max_chars(max_chars)
    content, truncated, total_chars = _truncate_text(text, limit)
    return {
        "mode": "full",
        "content": content,
        "truncated": truncated,
        "total_chars": total_chars,
        "returned_chars": len(content),
    }


def _compile_query_matcher(
    query: str,
    *,
    regex: bool,
    case_insensitive: bool,
):
    q = (query or "").strip()
    if not q:
        raise ValueError("query 不能为空")
    if regex:
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            pat = re.compile(q, flags)
        except re.error as exc:
            raise ValueError(f"regex 非法: {exc}") from exc

        def matches(s: str) -> bool:
            return pat.search(s or "") is not None

        def find_positions(s: str, limit: int) -> list[int]:
            return [m.start() for m in pat.finditer(s or "")][:limit]

    else:
        key = q.lower() if case_insensitive else q

        def matches(s: str) -> bool:
            hay = (s or "")
            if case_insensitive:
                return key in hay.lower()
            return key in hay

        def find_positions(s: str, limit: int) -> list[int]:
            hay = s or ""
            if case_insensitive:
                hay_l = hay.lower()
                key_l = key
                pos = 0
                found: list[int] = []
                while len(found) < limit:
                    i = hay_l.find(key_l, pos)
                    if i < 0:
                        break
                    found.append(i)
                    pos = i + max(1, len(key_l))
                return found
            pos = 0
            found = []
            while len(found) < limit:
                i = hay.find(key, pos)
                if i < 0:
                    break
                found.append(i)
                pos = i + max(1, len(key))
            return found

    return matches, find_positions


def _extract_snippets(text: str, positions: list[int], *, half: int) -> list[str]:
    half = max(40, min(int(half or 0), 2000))
    snippets: list[str] = []
    for pos in positions:
        start = max(0, pos - half)
        end = min(len(text), pos + half)
        chunk = text[start:end].replace("\n", " ")
        if start > 0:
            chunk = "…" + chunk
        if end < len(text):
            chunk = chunk + "…"
        snippets.append(chunk.strip())
    return snippets


def search_markdown_sections(
    text: str,
    query: str,
    *,
    scope: str = "all",
    regex: bool = False,
    case_insensitive: bool = True,
    max_level: int = 6,
    max_hits: int = 30,
    snippet_chars: int = 200,
    max_snippets_per_section: int = 2,
) -> dict[str, Any]:
    """在章节内搜索。scope: titles（仅标题）| content（仅正文）| all。"""
    scope_val = (scope or "all").strip().lower()
    if scope_val not in ("titles", "content", "all"):
        raise ValueError("scope 须为 titles、content 或 all")
    try:
        max_level = int(max_level)
    except (TypeError, ValueError):
        max_level = 6
    max_level = max(1, min(6, max_level))
    try:
        max_hits = int(max_hits)
    except (TypeError, ValueError):
        max_hits = 30
    max_hits = max(1, min(max_hits, int(getattr(config, "MARKDOWN_SECTIONS_SEARCH_MAX_HITS", 50))))
    max_snippets_per_section = max(1, min(int(max_snippets_per_section or 2), 5))

    matches_fn, find_positions = _compile_query_matcher(
        query, regex=regex, case_insensitive=case_insensitive
    )
    headings = parse_headings(text)
    lines = text.split("\n")
    lines_count = len(lines)
    hits: list[dict[str, Any]] = []

    for h in headings:
        if h.level > max_level:
            continue
        if len(hits) >= max_hits:
            break
        end_line = _section_end_line(headings, h.index, lines_count)
        title_hit = matches_fn(h.title)
        body_text = _slice_by_lines(text, h.line + 1, end_line)
        body_hit = matches_fn(body_text) if scope_val in ("content", "all") else False

        if scope_val == "titles" and not title_hit:
            continue
        if scope_val == "content" and not body_hit:
            continue
        if scope_val == "all" and not (title_hit or body_hit):
            continue

        snippets: list[str] = []
        if body_hit and scope_val in ("content", "all"):
            positions = find_positions(body_text, max_snippets_per_section)
            snippets = _extract_snippets(body_text, positions, half=snippet_chars)
        hit_in = []
        if title_hit:
            hit_in.append("title")
        if body_hit:
            hit_in.append("body")

        hits.append(
            {
                "index": h.index,
                "level": h.level,
                "title": h.title,
                "path": list(h.path),
                "line": h.line + 1,
                "line_end": end_line,
                "match_in": hit_in,
                "snippets": snippets,
            }
        )

    return {
        "query": query,
        "scope": scope_val,
        "max_level": max_level,
        "hit_count": len(hits),
        "truncated": len(hits) >= max_hits,
        "hits": hits,
    }


def search_markdown_corpus(
    files: list[tuple[str, str]],
    query: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """多文件搜索；files 为 [(path, text), ...]。"""
    all_hits: list[dict[str, Any]] = []
    max_hits = int(kwargs.get("max_hits") or 30)
    per_file: list[dict[str, Any]] = []

    for path, text in files:
        if len(all_hits) >= max_hits:
            break
        if not (text or "").strip():
            continue
        try:
            one = search_markdown_sections(
                text,
                query,
                max_hits=max(1, max_hits - len(all_hits)),
                **{k: v for k, v in kwargs.items() if k != "max_hits"},
            )
        except ValueError:
            raise
        except Exception:
            continue
        file_hits = one.get("hits") or []
        for h in file_hits:
            h["file"] = path
            all_hits.append(h)
            if len(all_hits) >= max_hits:
                break
        if file_hits:
            per_file.append({"path": path, "hit_count": len(file_hits)})

    return {
        "query": query,
        "scope": kwargs.get("scope") or "all",
        "file_count": len(files),
        "files_with_hits": len(per_file),
        "hit_count": len(all_hits),
        "truncated": len(all_hits) >= max_hits,
        "hits": all_hits,
        "by_file": per_file,
    }
