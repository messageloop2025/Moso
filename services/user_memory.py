"""用户 Memory 空间：web/fs/<user>/memory/ 下的长期记忆、索引与多文件搜索。

记忆用于提高检查优先级、减少盲目探索；可能过时，重要操作须以实机为准。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from api.filesystem import (
    coerce_fs_relative_path,
    fs_read_file_async,
    fs_write_file_async,
    get_user_fs_root,
    resolve_fs_path,
)
from services.markdown_sections import (
    get_markdown_section,
    list_markdown_sections,
    search_markdown_corpus,
)

MEMORY_ROOT = (getattr(config, "USER_MEMORY_SUBDIR", None) or "memory").strip("/\\") or "memory"
_META_RE = re.compile(
    r"<!--\s*edgeops-memory\s*\n(?P<body>.*?)\n\s*-->",
    re.I | re.S,
)
_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def memory_rel(*parts: str) -> str:
    bits = [MEMORY_ROOT]
    for p in parts:
        s = (p or "").replace("\\", "/").strip("/")
        if s:
            bits.append(s)
    return "/".join(bits)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_slug(text: str, *, fallback: str = "item") -> str:
    s = _SAFE_SLUG_RE.sub("-", (text or "").strip()).strip("-._")
    s = s[:48] if s else fallback
    return s or fallback


def parse_memory_meta(text: str) -> dict[str, Any]:
    """解析文件头 <!-- edgeops-memory ... --> 键值。"""
    m = _META_RE.search(text or "")
    if not m:
        return {}
    meta: dict[str, Any] = {}
    for line in (m.group("body") or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip().lower()
        val = v.strip()
        if key == "host_id":
            try:
                meta[key] = int(val)
            except ValueError:
                meta[key] = val
        elif key == "tags":
            meta[key] = [t.strip() for t in val.split(",") if t.strip()]
        else:
            meta[key] = val
    return meta


def strip_memory_meta(text: str) -> str:
    return _META_RE.sub("", text or "", count=1).lstrip("\n")


def build_memory_meta_block(
    *,
    kind: str,
    title: str,
    summary: str,
    host_id: int | None = None,
    tags: list[str] | None = None,
    path: str = "",
    updated: str | None = None,
) -> str:
    lines = [
        "<!-- edgeops-memory",
        f"kind: {(kind or 'note').strip()}",
        f"title: {(title or '').strip() or 'untitled'}",
        f"summary: {(summary or '').strip()[:240]}",
        f"updated: {updated or _utc_now_iso()}",
    ]
    if path:
        lines.append(f"path: {path}")
    if host_id is not None:
        lines.append(f"host_id: {int(host_id)}")
    if tags:
        lines.append("tags: " + ",".join(t.strip() for t in tags if t and str(t).strip()))
    lines.append("-->")
    return "\n".join(lines) + "\n\n"


def infer_summary_from_body(body: str, *, max_len: int = 160) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--"):
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        return s[:max_len]
    # fallback: first non-empty after stripping headings
    plain = re.sub(r"^#+\s*", "", text, flags=re.M).strip()
    return plain[:max_len]


def host_memory_rel_path(host_id: int, host_name: str | None = None) -> str:
    slug = _safe_slug(host_name or f"host-{host_id}", fallback=f"host-{host_id}")
    return memory_rel("hosts", f"h{int(host_id)}_{slug}.md")


def _coerce_host_id(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _guide_md() -> str:
    return f"""# Memory 空间说明

本目录是你在毛竹（Moso）中的**长期记忆工作区**（相对路径 `{MEMORY_ROOT}/`）。

## 用途
- 关联历史：路径/位置、规则、未再提及但必要的参考、用户对同类目标的习惯操作
- 主机环境与状态、主题笔记（topics/）、月度流水（journal/）
- 目标：减少摸索、提高后续执行效率；**不是**替代实机事实

## 使用边界
- **检查 / 确认 / 缩小探测范围**：可自行使用记忆决定先查什么。
- **用记忆补全用户未给出的目标/参数/路径并执行**：须先向用户说明拟采用的记忆内容并确认，禁止擅自执行。
- 记忆可能过时；重要操作以实机检查为准。

## 与其它存储的分工
| 内容 | 应写入 |
|------|--------|
| 本会话临时约束 | 会话提示词 `update_session_prompt` |
| 某机可展示规则/工具链 | 主机级提示词 + 可同步摘要到 `memory/hosts/` |
| 密码/Token 等机密 | 主机知识库（勿写入 Memory） |
| 可复用运维流程 | 最佳实践 |
| 大文件/报告 | `fs_*` / artifacts |

## 维护
- 新主题或验证过的习惯/路径：适时 `memory_write`；状态变化时更新
- 写入后默认刷新 `INDEX.md`；文件头用 `<!-- edgeops-memory ... -->`
"""


def _seed_index_md() -> str:
    return f"""# Memory Index

> rebuilt: {_utc_now_iso()}
> 记忆可能过时；重要操作以实机检查为准。本索引供快速定位。

## hosts

（暂无主机记忆）

## topics

（暂无主题记忆）

## journal

（暂无流水）
"""


async def ensure_memory_workspace(user: dict) -> dict[str, Any]:
    """确保 memory/ 结构存在；已存在则不覆盖 GUIDE/INDEX 正文（INDEX 缺失才建）。"""
    base = get_user_fs_root(user)
    created: list[str] = []

    async def _ensure_file(rel: str, content: str, *, overwrite: bool = False) -> None:
        try:
            target = resolve_fs_path(coerce_fs_relative_path(rel, base), base)
        except ValueError as e:
            raise ValueError(str(e)) from e
        if target.exists() and not overwrite:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        await fs_write_file_async(rel, content, base, mode="overwrite")
        created.append(rel)

    for sub in ("hosts", "topics", "journal"):
        rel_dir = memory_rel(sub)
        p = resolve_fs_path(coerce_fs_relative_path(rel_dir, base), base)
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(rel_dir + "/")

    await _ensure_file(memory_rel("GUIDE.md"), _guide_md())
    await _ensure_file(memory_rel("INDEX.md"), _seed_index_md())
    return {
        "success": True,
        "root": MEMORY_ROOT,
        "created": created,
        "paths": {
            "index": memory_rel("INDEX.md"),
            "guide": memory_rel("GUIDE.md"),
            "hosts": memory_rel("hosts"),
            "topics": memory_rel("topics"),
            "journal": memory_rel("journal"),
        },
        "note": "记忆可能过时；重要操作以实机检查为准。",
    }


def _iter_memory_md_files(user: dict) -> list[str]:
    base = get_user_fs_root(user)
    root = resolve_fs_path(coerce_fs_relative_path(MEMORY_ROOT, base), base)
    if not root.exists():
        return []
    max_files = int(getattr(config, "MARKDOWN_SECTIONS_SEARCH_MAX_FILES", 100))
    max_chars = int(getattr(config, "MARKDOWN_SECTIONS_MAX_FILE_CHARS", 2_000_000))
    out: list[str] = []
    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        try:
            if p.stat().st_size > max_chars:
                continue
        except OSError:
            continue
        rel = p.relative_to(base).as_posix()
        name = p.name.lower()
        if name in ("guide.md",):
            continue
        out.append(rel)
        if len(out) >= max_files:
            break
    return out


async def _load_md_pairs(user: dict, paths: list[str] | None = None) -> list[tuple[str, str]]:
    base = get_user_fs_root(user)
    rels = paths if paths is not None else _iter_memory_md_files(user)
    pairs: list[tuple[str, str]] = []
    for rel in rels:
        if rel.replace("\\", "/").endswith("/INDEX.md"):
            continue
        try:
            out = await fs_read_file_async(rel, base, offset=0, size=None)
            if out.get("success"):
                pairs.append((rel, out.get("content") or ""))
        except Exception:
            continue
    return pairs


async def list_memory_entries(user: dict, *, kind: str | None = None) -> dict[str, Any]:
    await ensure_memory_workspace(user)
    pairs = await _load_md_pairs(user)
    entries: list[dict[str, Any]] = []
    for rel, text in pairs:
        meta = parse_memory_meta(text)
        k = (meta.get("kind") or _kind_from_path(rel)).strip().lower()
        if kind and k != kind.strip().lower():
            continue
        body = strip_memory_meta(text)
        summary = (meta.get("summary") or infer_summary_from_body(body)).strip()
        title = (meta.get("title") or Path(rel).stem).strip()
        entries.append(
            {
                "path": rel,
                "kind": k,
                "title": title,
                "summary": summary,
                "host_id": meta.get("host_id"),
                "tags": meta.get("tags") or [],
                "updated": meta.get("updated") or "",
            }
        )
    entries.sort(key=lambda e: (e.get("kind") or "", e.get("title") or "", e.get("path") or ""))
    return {
        "success": True,
        "root": MEMORY_ROOT,
        "count": len(entries),
        "entries": entries,
        "note": "记忆可能过时；重要操作以实机检查为准。",
    }


def _kind_from_path(rel: str) -> str:
    low = rel.replace("\\", "/").lower()
    if f"/{MEMORY_ROOT}/hosts/" in f"/{low}" or low.startswith(f"{MEMORY_ROOT}/hosts/"):
        return "host"
    if f"/{MEMORY_ROOT}/topics/" in f"/{low}" or low.startswith(f"{MEMORY_ROOT}/topics/"):
        return "topic"
    if f"/{MEMORY_ROOT}/journal/" in f"/{low}" or low.startswith(f"{MEMORY_ROOT}/journal/"):
        return "journal"
    return "note"


async def rebuild_memory_index(user: dict) -> dict[str, Any]:
    listed = await list_memory_entries(user)
    entries = listed.get("entries") or []
    by_kind: dict[str, list[dict]] = {"host": [], "topic": [], "journal": [], "note": []}
    for e in entries:
        k = e.get("kind") or "note"
        by_kind.setdefault(k, []).append(e)

    def _table(rows: list[dict]) -> str:
        if not rows:
            return "（暂无）\n"
        lines = [
            "| path | title | summary | updated | tags |",
            "|------|-------|---------|---------|------|",
        ]
        for r in rows:
            tags = ",".join(r.get("tags") or [])
            hid = r.get("host_id")
            title = r.get("title") or ""
            if hid is not None:
                title = f"{title} (host_id={hid})"
            lines.append(
                "| `{path}` | {title} | {summary} | {updated} | {tags} |".format(
                    path=r.get("path") or "",
                    title=(title or "").replace("|", "/"),
                    summary=(r.get("summary") or "").replace("|", "/")[:120],
                    updated=r.get("updated") or "",
                    tags=tags.replace("|", "/"),
                )
            )
        return "\n".join(lines) + "\n"

    content = (
        f"# Memory Index\n\n"
        f"> rebuilt: {_utc_now_iso()}\n"
        f"> 记忆可能过时；重要操作以实机检查为准。本索引供快速定位。\n\n"
        f"## hosts\n\n{_table(by_kind.get('host') or [])}\n"
        f"## topics\n\n{_table(by_kind.get('topic') or [])}\n"
        f"## journal\n\n{_table(by_kind.get('journal') or [])}\n"
        f"## other\n\n{_table(by_kind.get('note') or [])}\n"
    )
    base = get_user_fs_root(user)
    idx = memory_rel("INDEX.md")
    await fs_write_file_async(idx, content, base, mode="overwrite")
    return {
        "success": True,
        "path": idx,
        "entry_count": len(entries),
        "message": "已重建 INDEX.md",
        "note": "记忆可能过时；重要操作以实机检查为准。",
    }


async def read_memory_file(
    user: dict,
    path: str,
    *,
    section_path: list | None = None,
    heading: str | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    await ensure_memory_workspace(user)
    rel = coerce_fs_relative_path(path or "", get_user_fs_root(user))
    if not rel.lower().startswith(MEMORY_ROOT.lower() + "/") and rel.lower() != MEMORY_ROOT.lower():
        # allow shorthand: hosts/xxx.md → memory/hosts/xxx.md
        if not rel.lower().startswith(MEMORY_ROOT.lower()):
            rel = memory_rel(rel)
    base = get_user_fs_root(user)
    out = await fs_read_file_async(rel, base, offset=0, size=None)
    if not out.get("success"):
        return {"success": False, "error": "读取失败或不存在", "path": rel}
    text = out.get("content") or ""
    meta = parse_memory_meta(text)
    result: dict[str, Any] = {
        "success": True,
        "path": rel,
        "meta": meta,
        "note": "记忆可能过时；重要操作以实机检查为准。",
    }
    if section_path or heading:
        sec = get_markdown_section(
            text,
            section_path=section_path,
            heading=heading,
            max_chars=max_chars,
        )
        result["section"] = sec
        result["content"] = sec.get("content")
    else:
        body = text
        if max_chars is not None:
            try:
                mc = int(max_chars)
                if mc > 0 and len(body) > mc:
                    result["truncated"] = True
                    body = body[:mc]
            except (TypeError, ValueError):
                pass
        result["content"] = body
        result["sections"] = list_markdown_sections(text, max_level=3).get("sections")
    return result


async def write_memory_file(
    user: dict,
    *,
    path: str | None = None,
    content: str,
    kind: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    host_id: int | None = None,
    host_name: str | None = None,
    tags: list[str] | None = None,
    append: bool = False,
    rebuild_index: bool = True,
) -> dict[str, Any]:
    await ensure_memory_workspace(user)
    base = get_user_fs_root(user)
    body_in = content if content is not None else ""

    if host_id is not None and not path:
        path = host_memory_rel_path(int(host_id), host_name)
        kind = kind or "host"
        title = title or host_name or f"host-{host_id}"

    if not path:
        # topics default
        slug = _safe_slug(title or "note")
        path = memory_rel("topics", f"{slug}.md")
        kind = kind or "topic"

    rel = coerce_fs_relative_path(path, base)
    if not rel.lower().startswith(MEMORY_ROOT.lower() + "/") and rel.lower() != MEMORY_ROOT.lower():
        rel = memory_rel(rel)

    kind_val = (kind or _kind_from_path(rel)).strip().lower()
    existing = ""
    try:
        prev = await fs_read_file_async(rel, base, offset=0, size=None)
        if prev.get("success"):
            existing = prev.get("content") or ""
    except Exception:
        existing = ""

    if append and existing:
        # append to body after meta
        old_meta = parse_memory_meta(existing)
        old_body = strip_memory_meta(existing)
        merged_body = (old_body.rstrip() + "\n\n" + strip_memory_meta(body_in).strip()).strip() + "\n"
        title_f = (title or old_meta.get("title") or Path(rel).stem).strip()
        summary_f = (summary or old_meta.get("summary") or infer_summary_from_body(merged_body)).strip()
        tags_f = tags if tags is not None else (old_meta.get("tags") or [])
        hid = _coerce_host_id(host_id if host_id is not None else old_meta.get("host_id"))
        final = build_memory_meta_block(
            kind=kind_val,
            title=title_f,
            summary=summary_f,
            host_id=hid,
            tags=list(tags_f) if tags_f else None,
            path=rel,
        ) + merged_body
    else:
        body_only = strip_memory_meta(body_in).strip() + "\n"
        title_f = (title or parse_memory_meta(body_in).get("title") or Path(rel).stem).strip()
        summary_f = (
            summary
            or parse_memory_meta(body_in).get("summary")
            or infer_summary_from_body(body_only)
        ).strip()
        tags_f = tags if tags is not None else (parse_memory_meta(body_in).get("tags") or [])
        hid = _coerce_host_id(host_id if host_id is not None else parse_memory_meta(body_in).get("host_id"))
        final = build_memory_meta_block(
            kind=kind_val,
            title=title_f,
            summary=summary_f,
            host_id=hid,
            tags=list(tags_f) if tags_f else None,
            path=rel,
        ) + (body_only if body_only.strip() else f"# {title_f}\n")

    parent = resolve_fs_path(coerce_fs_relative_path(str(Path(rel).parent.as_posix()), base), base)
    parent.mkdir(parents=True, exist_ok=True)
    await fs_write_file_async(rel, final, base, mode="overwrite")

    result: dict[str, Any] = {
        "success": True,
        "path": rel,
        "kind": kind_val,
        "title": title_f,
        "summary": summary_f,
        "message": "已写入 Memory",
        "note": "记忆可能过时；重要操作以实机检查为准。写入后请在状态变化时更新对应条目。",
    }
    if rebuild_index:
        idx = await rebuild_memory_index(user)
        result["index"] = {"path": idx.get("path"), "entry_count": idx.get("entry_count")}
    return result


async def search_memory(
    user: dict,
    query: str,
    *,
    scope: str = "all",
    regex: bool = False,
    case_insensitive: bool = True,
    max_hits: int = 30,
    kind: str | None = None,
    host_id: int | None = None,
) -> dict[str, Any]:
    await ensure_memory_workspace(user)
    pairs = await _load_md_pairs(user)
    if kind or host_id is not None:
        filtered: list[tuple[str, str]] = []
        for rel, text in pairs:
            meta = parse_memory_meta(text)
            k = (meta.get("kind") or _kind_from_path(rel)).lower()
            if kind and k != kind.strip().lower():
                continue
            if host_id is not None:
                try:
                    if int(meta.get("host_id") or -1) != int(host_id):
                        # also match by filename h{id}_
                        if f"/h{int(host_id)}_" not in f"/{rel.replace(chr(92), '/')}":
                            continue
                except (TypeError, ValueError):
                    continue
            filtered.append((rel, text))
        pairs = filtered

    out = search_markdown_corpus(
        pairs,
        query,
        scope=scope,
        regex=regex,
        case_insensitive=case_insensitive,
        max_hits=max_hits,
    )
    out["success"] = True
    out["root"] = MEMORY_ROOT
    out["note"] = "记忆命中仅供优先检查；重要操作以实机为准。"
    return out


async def list_fs_markdown_under(user: dict, relative_dir: str) -> list[tuple[str, str]]:
    """列出用户 fs 某目录下全部 .md 并读取（供 markdown 多文件搜索）。"""
    base = get_user_fs_root(user)
    rel_root = coerce_fs_relative_path(relative_dir or "", base)
    root = resolve_fs_path(rel_root, base) if rel_root else base.resolve()
    if not root.exists():
        return []
    max_files = int(getattr(config, "MARKDOWN_SECTIONS_SEARCH_MAX_FILES", 100))
    max_chars = int(getattr(config, "MARKDOWN_SECTIONS_MAX_FILE_CHARS", 2_000_000))
    paths: list[str] = []
    if root.is_file() and root.suffix.lower() == ".md":
        paths = [root.relative_to(base).as_posix()]
    else:
        for p in sorted(root.rglob("*.md")):
            if not p.is_file():
                continue
            try:
                if p.stat().st_size > max_chars:
                    continue
            except OSError:
                continue
            paths.append(p.relative_to(base).as_posix())
            if len(paths) >= max_files:
                break
    pairs: list[tuple[str, str]] = []
    for rel in paths:
        try:
            out = await fs_read_file_async(rel, base, offset=0, size=None)
            if out.get("success"):
                pairs.append((rel, out.get("content") or ""))
        except Exception:
            continue
    return pairs


def build_memory_map_prompt_section() -> str:
    """注入 system：记忆地图 + Memory 用途/边界（短、可执行）。"""
    return f"""
## 信息存放地图（记忆与约定）
按用途选择存储；**不要**把机密写进 Memory 或主机提示词。

| 用途 | 存放 | 读写 |
|------|------|------|
| 本会话临时约束/风格 | 会话提示词 | `get_session_prompt` / `update_session_prompt`（有则已注入「会话级约束」） |
| 某机可展示规则/工具链/禁忌 | 主机级提示词 | `get/update/append_host_prompt`、`search_hosts_by_prompt`（焦点机可已注入） |
| 密码/Token/私钥等机密 | 主机知识库 | `get/update/append_host_knowledge`（严禁回复展示） |
| 跨会话：路径/环境/状态、历史参考、用户习惯操作 | **Memory** `{MEMORY_ROOT}/` | `memory_*`（见下） |
| 可复用运维流程 | 最佳实践 | `get_best_practices` / `add_best_practice` |
| 大文件、报告、脚本 | 用户工作区 / artifacts | `fs_*`、`create_chat_artifact` |
| 主机侧可复用脚本 | `.edgeops/scripts` | `edgeops_save_script` 等（规则优先主机提示词） |
| 全局/用户 AI 规则 | 用户 system 提示词（配置） | 消息靠前；管理员另有全局设置工具 |

**分流口诀**：只本会话 → 会话提示词；某机规则 → 主机提示词；机密 → 主机知识；跨会话可展示参考/习惯 → Memory；通用流程 → 最佳实践。

## Memory：用途与边界（必遵）
**目的**：操作主机或完成任务时关联历史——相关位置、规则、用户未再提及但必要的参考、对同类目标的习惯操作——**减少摸索、提高效率**。记忆可能过时；重要结论仍以实机检查为准。

**读（开工前）**：对涉及的主机/主题先 `memory_search` / `memory_list` / `memory_read`（并配合主机提示词），用结果安排检查顺序与候选路径，勿盲目全盘探测。

**写（建记）**：用户开展新内容，或你已验证路径/环境/习惯做法后，适时 `memory_write`（host_id 写入 `hosts/`；主题写入 `topics/`）。状态变化时更新对应条目并保持 INDEX。

**用记忆补全时的红线**：
- **检查 / 确认 / 缩小探测范围**：可自行依据记忆决定先查什么，不必事事请示。
- **执行类动作**（发命令、改配置、选路径/主机/参数作为操作目标）若依赖记忆补全、而用户本轮**未明确给出**该信息：须先说明「拟采用记忆中的 …」并用 `ask_user_choice`（或等价确认）征得同意后再执行；**禁止擅自按记忆执行**。
- 用户本轮已明确给出的目标/参数，以用户为准；记忆仅作对照，冲突时先核实或询问。

**工具**：`memory_ensure` → 读用 `memory_search`/`memory_list`/`memory_read` → 写用 `memory_write`（默认刷新索引）/`memory_rebuild_index`。亦可 `markdown_search_sections(file_root=fs, path="{MEMORY_ROOT}")`。**禁止**把密码/密钥写入 Memory。
"""


# —— AI tools registration helpers（由 ai_skills 合并 TOOL 定义）—— #

MEMORY_TOOL_NAMES = frozenset({
    "memory_ensure",
    "memory_list",
    "memory_search",
    "memory_read",
    "memory_write",
    "memory_rebuild_index",
    "get_session_prompt",
})


async def execute_memory_tool(name: str, arguments: dict, user: dict, **_kwargs) -> str | None:
    """若 name 为 memory_* / get_session_prompt 则处理并返回 JSON 字符串，否则返回 None。"""
    args = arguments if isinstance(arguments, dict) else {}

    if name == "memory_ensure":
        return json.dumps(await ensure_memory_workspace(user), ensure_ascii=False)

    if name == "memory_list":
        return json.dumps(
            await list_memory_entries(user, kind=args.get("kind")),
            ensure_ascii=False,
        )

    if name == "memory_search":
        q = (args.get("query") or args.get("q") or "").strip()
        if not q:
            return json.dumps({"success": False, "error": "缺少 query"}, ensure_ascii=False)
        return json.dumps(
            await search_memory(
                user,
                q,
                scope=args.get("scope") or "all",
                regex=bool(args.get("regex")),
                case_insensitive=args.get("case_insensitive") is not False,
                max_hits=int(args.get("max_hits") or 30),
                kind=args.get("kind"),
                host_id=args.get("host_id"),
            ),
            ensure_ascii=False,
        )

    if name == "memory_read":
        path = (args.get("path") or "").strip()
        if not path and args.get("host_id") is not None:
            path = host_memory_rel_path(int(args["host_id"]), args.get("host_name"))
        if not path:
            return json.dumps({"success": False, "error": "缺少 path 或 host_id"}, ensure_ascii=False)
        sp = args.get("section_path")
        if sp is not None and not isinstance(sp, list):
            sp = None
        return json.dumps(
            await read_memory_file(
                user,
                path,
                section_path=sp,
                heading=args.get("heading"),
                max_chars=args.get("max_chars"),
            ),
            ensure_ascii=False,
        )

    if name == "memory_write":
        content = args.get("content")
        if content is None:
            return json.dumps({"success": False, "error": "缺少 content"}, ensure_ascii=False)
        tags = args.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return json.dumps(
            await write_memory_file(
                user,
                path=args.get("path"),
                content=str(content),
                kind=args.get("kind"),
                title=args.get("title"),
                summary=args.get("summary"),
                host_id=args.get("host_id"),
                host_name=args.get("host_name"),
                tags=tags,
                append=bool(args.get("append")),
                rebuild_index=args.get("rebuild_index") is not False,
            ),
            ensure_ascii=False,
        )

    if name == "memory_rebuild_index":
        return json.dumps(await rebuild_memory_index(user), ensure_ascii=False)

    if name == "get_session_prompt":
        from database import get_db

        sid = args.get("session_id")
        if sid is None:
            return json.dumps({"success": False, "error": "缺少 session_id"}, ensure_ascii=False)
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT COALESCE(session_prompt, '') AS session_prompt FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
            (int(sid), user["id"]),
        )
        if not rows:
            return json.dumps({"success": False, "error": "会话不存在或无权操作"}, ensure_ascii=False)
        return json.dumps(
            {
                "success": True,
                "session_id": int(sid),
                "prompt": rows[0]["session_prompt"] or "",
            },
            ensure_ascii=False,
        )

    return None


def memory_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "memory_ensure",
                "description": (
                    "确保用户 Memory 空间存在（memory/hosts|topics|journal + GUIDE.md + INDEX.md）。"
                    "开始写入长期记忆前可调用一次。"
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_list",
                "description": (
                    "列出 Memory 条目（path/title/summary/host_id/tags）。"
                    "优先于盲目 fs_list；记忆可能过时，重要操作仍须实机检查。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["host", "topic", "journal", "note"],
                            "description": "可选筛选",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": (
                    "在 Memory 空间多文件搜索 Markdown 章节（标题/正文）。"
                    "用于快速定位主机环境/状态笔记；命中后 memory_read 精读，重要结论仍须实机核实。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "scope": {"type": "string", "enum": ["titles", "content", "all"]},
                        "regex": {"type": "boolean"},
                        "case_insensitive": {"type": "boolean"},
                        "max_hits": {"type": "integer"},
                        "kind": {"type": "string", "enum": ["host", "topic", "journal", "note"]},
                        "host_id": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_read",
                "description": "读取 Memory 文件全文或按章节精读。可用 path，或 host_id 读取该主机记忆文件。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对工作区，如 memory/hosts/h1_web.md"},
                        "host_id": {"type": "integer"},
                        "host_name": {"type": "string"},
                        "section_path": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "章节路径，如 [\"Status\"]",
                        },
                        "heading": {"type": "string"},
                        "max_chars": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_write",
                "description": (
                    "写入/更新 Memory（路径、环境/状态、历史参考、用户习惯操作等可展示信息）。"
                    "用户开展新内容或你已验证关键事实后应写入；状态变化时更新。"
                    "传 host_id 时默认写入 memory/hosts/h{id}_{name}.md。默认重建 INDEX.md。勿写入密码/密钥。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Markdown 正文（可含章节）"},
                        "path": {"type": "string"},
                        "kind": {"type": "string", "enum": ["host", "topic", "journal", "note"]},
                        "title": {"type": "string"},
                        "summary": {"type": "string", "description": "一句话摘要，写入元数据与索引"},
                        "host_id": {"type": "integer"},
                        "host_name": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "append": {"type": "boolean", "description": "true=追加正文；false=覆盖（保留/重写元数据）"},
                        "rebuild_index": {"type": "boolean", "description": "默认 true"},
                    },
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_rebuild_index",
                "description": "扫描 Memory 下 md 文件，根据元数据/摘要重建 memory/INDEX.md。",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_session_prompt",
                "description": "读取指定会话的会话级提示词全文。已注入「会话级约束」时可不必重复调用；修改前可用本工具核对。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "integer", "description": "当前会话 ID（system 中已提供）"},
                    },
                    "required": ["session_id"],
                },
            },
        },
    ]

