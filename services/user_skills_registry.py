"""每用户 Agent Skills：web/fs/<user>/skills/<name>/SKILL.md + DB 元数据。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

import config
from api.filesystem import get_user_fs_root
from database import get_db

logger = logging.getLogger("edgeops.user_skills.registry")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
# Cursor Agent Skills 默认：disable-model-invocation=true → 仅目录披露，按需 get_user_skill
_DEFAULT_DISABLE_MODEL_INVOCATION = True

DEFAULT_SKILL_BODY = """# {title}

## Quick start

（核心步骤，保持简洁；详细内容放同目录 reference.md）

## Additional resources

- 详细参考：`reference.md`（需要时用 `read_user_skill_file` 加载；写入用 `write_user_skill_file`）
- 示例：`examples.md`（可选）
- 可执行脚本放 `scripts/`（用 ssh_execute / fs 工具运行，勿用 fs_write_file 写 Skill 文件）
"""

DEFAULT_SKILL_TEMPLATE = """---
name: {name}
description: >-
  （第三人称）简述能力与触发场景。Include WHAT it does and WHEN to use it
  (trigger terms). Use when the user mentions …
disable-model-invocation: true
---

""" + DEFAULT_SKILL_BODY

_PLACEHOLDER_DESC_MARKERS = (
    "在此简述",
    "简述能力与触发",
    "Use when the user mentions …",
    "Use when the user mentions ...",
)


def is_placeholder_description(desc: str) -> bool:
    d = (desc or "").strip()
    if not d:
        return True
    return any(m in d for m in _PLACEHOLDER_DESC_MARKERS)


def collect_skill_name_warnings(slug: str) -> list[str]:
    if "_" in slug:
        return [f"标识「{slug}」含下划线，Cursor 惯例建议使用连字符 -"]
    return []


def collect_description_warnings(slug: str, desc: str) -> list[str]:
    warnings = collect_skill_name_warnings(slug)
    d = (desc or "").strip()
    if not d:
        warnings.append(f"Skill「{slug}」缺少 description，请补充 frontmatter")
    elif is_placeholder_description(d):
        warnings.append(f"Skill「{slug}」description 仍为模板占位，请填写 WHAT+WHEN")
    return warnings


def default_skill_template_content(name: str = "my-skill", description: str = "") -> str:
    slug = (name or "my-skill").strip().lower()
    try:
        slug = normalize_skill_name(slug)
    except ValueError:
        slug = "my-skill"
    return render_skill_markdown(name=slug, description=description or "")


def normalize_skill_name(name: str) -> str:
    raw = (name or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
    if not raw or not _NAME_RE.match(raw):
        raise ValueError("标识须为小写字母开头，仅含 a-z、0-9、-、_，最长 64 字符")
    return raw


def get_user_skills_root(user: dict) -> Path:
    root = get_user_fs_root(user) / config.USER_SKILLS_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def skill_dir_relative(name: str) -> str:
    slug = normalize_skill_name(name)
    return f"{config.USER_SKILLS_SUBDIR}/{slug}"


def skill_md_path(user: dict, name: str) -> Path:
    slug = normalize_skill_name(name)
    return get_user_skills_root(user) / slug / "SKILL.md"


def normalize_pre_tool_use_decision(raw: str | None, default: str = "ask") -> str:
    d = str(raw or default).strip().lower()
    if d not in ("allow", "deny", "ask"):
        return default
    return d


def iter_skill_command_files(user: dict, skill_name: str) -> list[dict[str, Any]]:
    """列出 skills/<name>/commands/*.{md,txt}，供斜杠菜单与 resolve 共用。"""
    try:
        cmd_dir = skill_md_path(user, skill_name).parent / "commands"
    except Exception:
        return []
    if not cmd_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(cmd_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".md", ".txt", ""):
            continue
        alias = p.stem.strip().lstrip("/").lower()
        if not alias or not _NAME_RE.match(alias):
            continue
        out.append(
            {
                "alias": alias,
                "slash": f"/{alias}",
                "path": p,
                "rel": f"commands/{p.name}",
                "filename": p.name,
            }
        )
    return out


def read_skill_command_file(user: dict, skill_name: str, alias: str) -> str | None:
    want = (alias or "").strip().lstrip("/").lower()
    if not want:
        return None
    for item in iter_skill_command_files(user, skill_name):
        if item.get("alias") == want:
            try:
                return Path(item["path"]).read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("读取 command 文件失败 %s: %s", item.get("path"), e)
                return None
    return None


def detect_slash_params_hint(text: str) -> str:
    """从正文检测参数占位提示（用于斜杠菜单）。"""
    s = text or ""
    for token in ("{{arg}}", "$ARGUMENTS", "${ARGUMENTS}", "{{args}}", "{{arg1}}"):
        if token in s:
            return "{{arg}}" if token in ("{{arg}}", "{{args}}", "$ARGUMENTS", "${ARGUMENTS}") else token
    if re.search(r"\{\{arg\d+\}\}", s) or re.search(r"\$ARG\d+\b", s):
        return "{{arg1}} …"
    return ""


def hooks_json_path(user: dict, skill_name: str) -> Path:
    return skill_dir_path(user, skill_name) / "hooks.json"


def read_hooks_json_text(user: dict, skill_name: str) -> str:
    p = hooks_json_path(user, skill_name)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("读取 hooks.json 失败 %s: %s", p, e)
        return ""


def write_hooks_json_text(user: dict, skill_name: str, raw: str | None) -> None:
    """写入或删除 hooks.json。raw 为空字符串则删除文件。"""
    import json

    slug = normalize_skill_name(skill_name)
    p = hooks_json_path(user, slug)
    text = "" if raw is None else str(raw).strip()
    if not text:
        if p.is_file():
            p.unlink(missing_ok=True)
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"hooks.json 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("hooks.json 根节点必须是对象")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_skill_markdown(content: str) -> tuple[dict[str, Any], str]:
    text = content or ""
    meta: dict[str, Any] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            parsed = yaml.safe_load(m.group(1))
            if isinstance(parsed, dict):
                meta = parsed
        except Exception:
            meta = {}
        body = text[m.end() :]
    name = str(meta.get("name") or "").strip()
    desc = str(meta.get("description") or "").strip()
    return meta, body.lstrip("\n")


def _yaml_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


def skill_should_always_apply(meta: dict[str, Any]) -> bool:
    """是否内联注入 SKILL.md 正文（对齐 Cursor：disable-model-invocation=false 或 always-apply=true）。"""
    aa = _yaml_bool(meta.get("always-apply"))
    if aa is True:
        return True
    dmi = _yaml_bool(meta.get("disable-model-invocation"))
    if dmi is False:
        return True
    if dmi is True:
        return False
    return not _DEFAULT_DISABLE_MODEL_INVOCATION


def extract_display_title(meta: dict[str, Any], body: str, slug: str) -> str:
    if meta.get("title"):
        return str(meta["title"]).strip()[:120]
    if meta.get("displayName"):
        return str(meta["displayName"]).strip()[:120]
    m = re.search(r"^#\s+(.+)$", (body or "").strip(), re.MULTILINE)
    if m:
        return m.group(1).strip()[:120]
    return slug


def trim_skill_description(desc: str) -> str:
    limit = max(200, int(config.USER_SKILLS_DESC_MAX_CHARS))
    d = (desc or "").strip()
    return d[:limit] if len(d) > limit else d


def normalize_skill_file_content(
    content: str,
    slug: str,
    *,
    fallback_description: str = "",
) -> str:
    """写入磁盘前规范化 frontmatter（name、description 长度、保留 Cursor 扩展字段）。"""
    meta, body = parse_skill_markdown(content or "")
    slug = normalize_skill_name(slug)
    meta["name"] = slug
    desc = trim_skill_description(str(meta.get("description") or fallback_description or ""))
    lines = ["---", f"name: {slug}"]
    if desc:
        if "\n" in desc or len(desc) > 80:
            lines.append("description: >-")
            for part in desc.splitlines() or [desc]:
                lines.append(f"  {part.strip()}")
        else:
            lines.append(f"description: {desc}")
    for key in ("disable-model-invocation", "always-apply"):
        if key not in meta:
            continue
        vb = _yaml_bool(meta[key])
        if vb is None:
            lines.append(f"{key}: {meta[key]}")
        else:
            lines.append(f"{key}: {str(vb).lower()}")
    lines.extend(["---", "", (body or "").strip()])
    return "\n".join(lines).rstrip() + "\n"


def skill_dir_path(user: dict, name: str) -> Path:
    slug = normalize_skill_name(name)
    return get_user_skills_root(user) / slug


def list_skill_resource_files(user: dict, name: str) -> list[str]:
    d = skill_dir_path(user, name)
    if not d.is_dir():
        return []
    out: list[str] = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(d).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        out.append(rel)
    return out


def read_skill_resource_file(
    user: dict,
    name: str,
    relative_path: str,
    *,
    max_chars: int | None = None,
) -> str:
    rel = normalize_skill_resource_relpath(relative_path)
    base = skill_dir_path(user, name).resolve()
    path = (base / rel).resolve()
    try:
        path.relative_to(base)
    except ValueError as e:
        raise ValueError("非法路径") from e
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {rel}")
    limit = max_chars if max_chars is not None else int(config.USER_SKILLS_RESOURCE_MAX_CHARS)
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n\n…（文件已截断）"
    return text


def normalize_skill_resource_relpath(relative_path: str) -> str:
    rel = (relative_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise ValueError("非法路径")
    return rel


def skill_fs_display_path(name: str, relative_path: str) -> str:
    slug = normalize_skill_name(name)
    rel = normalize_skill_resource_relpath(relative_path)
    return f"{config.USER_SKILLS_SUBDIR}/{slug}/{rel}"


def write_skill_resource_file(
    user: dict,
    name: str,
    relative_path: str,
    content: str,
    *,
    append: bool = False,
) -> dict:
    slug = normalize_skill_name(name)
    rel = normalize_skill_resource_relpath(relative_path)
    if rel.lower() == "skill.md":
        raise ValueError("SKILL.md 须用 save_user_skill 写入，勿用 write_user_skill_file")
    base = skill_dir_path(user, slug)
    path = (base / rel).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as e:
        raise ValueError("非法路径") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if content is not None else ""
    if append and path.is_file():
        old = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(old + text, encoding="utf-8")
        mode = "append"
    else:
        path.write_text(text, encoding="utf-8")
        mode = "overwrite"
    return {
        "success": True,
        "skill": slug,
        "path": rel,
        "fs_path": skill_fs_display_path(slug, rel),
        "mode": mode,
        "size": path.stat().st_size,
    }


def delete_skill_resource_file(user: dict, name: str, relative_path: str) -> dict:
    slug = normalize_skill_name(name)
    rel = normalize_skill_resource_relpath(relative_path)
    if rel.lower() == "skill.md":
        raise ValueError("删除整个 Skill 请用 delete_user_skill；修改 SKILL.md 请用 save_user_skill")
    base = skill_dir_path(user, slug)
    path = (base / rel).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as e:
        raise ValueError("非法路径") from e
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {rel}")
    path.unlink()
    return {"success": True, "skill": slug, "path": rel, "fs_path": skill_fs_display_path(slug, rel)}


def list_skill_files_detail(user: dict, name: str) -> list[dict]:
    slug = normalize_skill_name(name)
    d = skill_dir_path(user, slug)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(d).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        try:
            st = p.stat()
            out.append(
                {
                    "path": rel,
                    "fs_path": skill_fs_display_path(slug, rel),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        except OSError:
            continue
    return out


def looks_like_agent_skill_fs_path(relative_path: str) -> bool:
    """是否为 Agent Skills 专用路径（须走 skill 工具，禁止 fs_write 归位到 chats/）。"""
    raw = (relative_path or "").strip().replace("\\", "/").lstrip("/").lower()
    if not raw:
        return False
    if raw == "skills" or raw.startswith("skills/"):
        return True
    # 误写在 chats/.../skills/ 下
    if "/skills/" in raw or raw.endswith("/skills"):
        parts = raw.split("/")
        if "skills" in parts:
            idx = parts.index("skills")
            if idx > 0 and parts[0] == "chats":
                return True
    return False


def render_skill_markdown(
    *,
    name: str,
    description: str = "",
    body: str = "",
    disable_model_invocation: bool | None = True,
) -> str:
    slug = normalize_skill_name(name)
    desc = trim_skill_description(description or "")
    body = (body or "").strip()
    if not body:
        title = slug.replace("-", " ").replace("_", " ").title()
        body = DEFAULT_SKILL_BODY.format(title=title)
    lines = ["---", f"name: {slug}"]
    if desc:
        if "\n" in desc or len(desc) > 80:
            lines.append("description: >-")
            for part in desc.splitlines():
                lines.append(f"  {part.strip()}")
        else:
            lines.append(f"description: {desc}")
    dmi = disable_model_invocation if disable_model_invocation is not None else _DEFAULT_DISABLE_MODEL_INVOCATION
    lines.append(f"disable-model-invocation: {str(dmi).lower()}")
    lines.extend(["---", "", body])
    return "\n".join(lines).rstrip() + "\n"


def resolve_skill_content_for_save(
    name: str,
    *,
    content: str | None = None,
    description: str = "",
    display_name: str = "",
    body: str = "",
    existing_content: str = "",
) -> str | None:
    """组装待写入的 SKILL.md；无正文/描述变更时返回 None。"""
    if content is not None and str(content).strip():
        text = str(content).strip()
        return text if text.endswith("\n") else text + "\n"
    body = (body or "").strip()
    desc = (description or display_name or "").strip()
    if body:
        return render_skill_markdown(name=name, description=desc, body=body)
    if desc:
        if existing_content:
            meta, old_body = parse_skill_markdown(existing_content)
            merged_desc = desc or str(meta.get("description") or "").strip()
            return render_skill_markdown(name=name, description=merged_desc, body=old_body)
        return render_skill_markdown(name=name, description=desc)
    return None


def public_skill_row(row: dict, *, fs_exists: bool | None = None, group_name: str = "") -> dict:
    gid = row.get("group_id")
    name = row.get("name") or ""
    slash = (row.get("slash_name") or "").strip().lstrip("/") or name
    return {
        "id": row["id"],
        "name": name,
        "display_name": row.get("display_name") or row.get("name") or "",
        "description": row.get("description") or "",
        "skill_path": row.get("skill_path") or "",
        "enabled": bool(row.get("enabled", 1)),
        "chat_enabled": bool(row.get("chat_enabled", 1)),
        "chat_scope_web": bool(row.get("chat_scope_web", 1)),
        "chat_scope_host": bool(row.get("chat_scope_host", 1)),
        "chat_scope_integration": bool(row.get("chat_scope_integration", 0)),
        "slash_name": slash,
        "slash_command": f"/{slash}" if slash else "",
        "hooks_enabled": bool(row.get("hooks_enabled", 0)),
        "pre_tool_use_matcher": row.get("pre_tool_use_matcher") or "",
        "pre_tool_use_decision": normalize_pre_tool_use_decision(
            row.get("pre_tool_use_decision"), "ask"
        ),
        "allowed_tools": row.get("allowed_tools") or "",
        "group_id": int(gid) if gid is not None else None,
        "group_name": (group_name or "").strip(),
        "file_exists": fs_exists if fs_exists is not None else True,
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
    }


async def user_skills_feature_enabled(db, user_id: int) -> bool:
    rows = await db.execute_fetchall(
        "SELECT skills_enabled FROM users WHERE id=?",
        (user_id,),
    )
    if not rows:
        return False
    return bool(dict(rows[0]).get("skills_enabled", 0))


async def require_user_skills_access(db, user: dict) -> None:
    if not await user_skills_feature_enabled(db, int(user["id"])):
        raise PermissionError("管理员尚未为您开启 Skills 功能")


async def read_skill_content(user: dict, name: str) -> str:
    path = skill_md_path(user, name)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


async def write_skill_content(user: dict, name: str, content: str) -> Path:
    slug = normalize_skill_name(name)
    normalized = normalize_skill_file_content(content, slug)
    path = skill_md_path(user, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized or "", encoding="utf-8")
    return path


async def _group_name_map(db, user_id: int) -> dict[int, str]:
    rows = await db.execute_fetchall(
        "SELECT id, name FROM user_skill_groups WHERE user_id=?",
        (user_id,),
    )
    return {int(dict(r)["id"]): str(dict(r).get("name") or "") for r in rows}


def normalize_skill_group_name(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        raise ValueError("分组名称不能为空")
    if len(raw) > 64:
        raise ValueError("分组名称最长 64 字符")
    return raw


async def list_user_skill_groups_summary(db, user_id: int) -> list[dict]:
    """返回分组摘要；首项为虚拟「未分组」。"""
    rows = await db.execute_fetchall(
        """SELECT id, name, sort_order, created_at, updated_at
           FROM user_skill_groups WHERE user_id=?
           ORDER BY sort_order ASC, name ASC""",
        (user_id,),
    )
    out: list[dict] = []
    ung = await db.execute_fetchall(
        """SELECT COUNT(*) AS skill_count,
                  SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_count
           FROM user_skills WHERE user_id=? AND group_id IS NULL""",
        (user_id,),
    )
    u = dict(ung[0]) if ung else {}
    out.append(
        {
            "id": None,
            "name": "",
            "is_default": True,
            "sort_order": -1,
            "skill_count": int(u.get("skill_count") or 0),
            "enabled_count": int(u.get("enabled_count") or 0),
        }
    )
    for r in rows:
        row = dict(r)
        gid = int(row["id"])
        cnt = await db.execute_fetchall(
            """SELECT COUNT(*) AS skill_count,
                      SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_count
               FROM user_skills WHERE user_id=? AND group_id=?""",
            (user_id, gid),
        )
        c = dict(cnt[0]) if cnt else {}
        out.append(
            {
                "id": gid,
                "name": row.get("name") or "",
                "is_default": False,
                "sort_order": int(row.get("sort_order") or 0),
                "skill_count": int(c.get("skill_count") or 0),
                "enabled_count": int(c.get("enabled_count") or 0),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return out


async def create_user_skill_group(db, user_id: int, *, name: str, sort_order: int = 0) -> dict:
    gname = normalize_skill_group_name(name)
    existing = await db.execute_fetchall(
        "SELECT id FROM user_skill_groups WHERE user_id=? AND name=?",
        (user_id, gname),
    )
    if existing:
        raise ValueError(f"分组「{gname}」已存在")
    cur = await db.execute(
        """INSERT INTO user_skill_groups (user_id, name, sort_order)
           VALUES (?, ?, ?)""",
        (user_id, gname, int(sort_order or 0)),
    )
    await db.commit()
    gid = int(cur.lastrowid)
    rows = await db.execute_fetchall(
        "SELECT * FROM user_skill_groups WHERE id=? AND user_id=?",
        (gid, user_id),
    )
    row = dict(rows[0]) if rows else {}
    return {
        "id": gid,
        "name": row.get("name") or gname,
        "is_default": False,
        "sort_order": int(row.get("sort_order") or 0),
        "skill_count": 0,
        "enabled_count": 0,
    }


async def update_user_skill_group(db, user_id: int, group_id: int, *, name: str) -> dict:
    gname = normalize_skill_group_name(name)
    rows = await db.execute_fetchall(
        "SELECT id FROM user_skill_groups WHERE id=? AND user_id=?",
        (group_id, user_id),
    )
    if not rows:
        raise LookupError("分组不存在")
    dup = await db.execute_fetchall(
        "SELECT id FROM user_skill_groups WHERE user_id=? AND name=? AND id<>?",
        (user_id, gname, group_id),
    )
    if dup:
        raise ValueError(f"分组「{gname}」已存在")
    await db.execute(
        """UPDATE user_skill_groups SET name=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (gname, group_id, user_id),
    )
    await db.commit()
    summary = await list_user_skill_groups_summary(db, user_id)
    for g in summary:
        if g.get("id") == group_id:
            return g
    return {"id": group_id, "name": gname, "is_default": False}


async def delete_user_skill_group(db, user_id: int, group_id: int) -> bool:
    rows = await db.execute_fetchall(
        "SELECT id FROM user_skill_groups WHERE id=? AND user_id=?",
        (group_id, user_id),
    )
    if not rows:
        return False
    await db.execute(
        "UPDATE user_skills SET group_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND group_id=?",
        (user_id, group_id),
    )
    cur = await db.execute(
        "DELETE FROM user_skill_groups WHERE id=? AND user_id=?",
        (group_id, user_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def bulk_set_group_skills_enabled(
    db,
    user_id: int,
    *,
    group_id: int | None,
    enabled: bool,
) -> dict:
    val = 1 if enabled else 0
    if group_id is None:
        cur = await db.execute(
            """UPDATE user_skills SET enabled=?, updated_at=CURRENT_TIMESTAMP
               WHERE user_id=? AND group_id IS NULL""",
            (val, user_id),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id FROM user_skill_groups WHERE id=? AND user_id=?",
            (group_id, user_id),
        )
        if not rows:
            raise LookupError("分组不存在")
        cur = await db.execute(
            """UPDATE user_skills SET enabled=?, updated_at=CURRENT_TIMESTAMP
               WHERE user_id=? AND group_id=?""",
            (val, user_id, group_id),
        )
    await db.commit()
    return {"updated": int(cur.rowcount or 0), "enabled": bool(enabled)}


async def bulk_assign_skills_to_group(
    db,
    user_id: int,
    *,
    group_id: int | None,
    skill_ids: list[int] | None = None,
    all_ungrouped: bool = False,
) -> dict:
    """批量调整 Skill 所属分组（仅当前用户自己的 Skill）。"""
    if group_id is not None:
        await resolve_skill_group_id(db, user_id, group_id)
    if not all_ungrouped and not skill_ids:
        raise ValueError("请指定 skill_ids 或 all_ungrouped")
    if all_ungrouped and group_id is None:
        raise ValueError("all_ungrouped 需指定目标分组")
    if all_ungrouped:
        cur = await db.execute(
            """UPDATE user_skills SET group_id=?, updated_at=CURRENT_TIMESTAMP
               WHERE user_id=? AND group_id IS NULL""",
            (group_id, user_id),
        )
    else:
        ids = [int(x) for x in (skill_ids or [])]
        if not ids:
            raise ValueError("skill_ids 不能为空")
        placeholders = ",".join("?" * len(ids))
        params: list[Any] = [group_id, user_id, *ids]
        cur = await db.execute(
            f"""UPDATE user_skills SET group_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND id IN ({placeholders})""",
            tuple(params),
        )
    await db.commit()
    return {"updated": int(cur.rowcount or 0), "group_id": group_id}


async def resolve_skill_group_id(
    db, user_id: int, group_id: int | None | str
) -> int | None:
    if group_id is None or group_id == "" or group_id == "none":
        return None
    try:
        gid = int(group_id)
    except (TypeError, ValueError) as e:
        raise ValueError("无效的分组 id") from e
    rows = await db.execute_fetchall(
        "SELECT id FROM user_skill_groups WHERE id=? AND user_id=?",
        (gid, user_id),
    )
    if not rows:
        raise ValueError("分组不存在")
    return gid


async def resolve_skill_group_ref(
    db,
    user_id: int,
    *,
    group_id: int | None | str = None,
    group_name: str | None = None,
) -> int | None:
    """按 id 或名称解析 Skill 分组（仅当前用户）。"""
    if group_id is not None and group_id != "":
        return await resolve_skill_group_id(db, user_id, group_id)
    gname = (group_name or "").strip()
    if not gname:
        return None
    gname = normalize_skill_group_name(gname)
    rows = await db.execute_fetchall(
        "SELECT id FROM user_skill_groups WHERE user_id=? AND name=?",
        (user_id, gname),
    )
    if not rows:
        raise ValueError(f"分组「{gname}」不存在")
    return int(dict(rows[0])["id"])


async def list_user_skills(
    db,
    user_id: int,
    user: dict | None = None,
    *,
    enabled: bool | None = None,
    group_id: str | int | None = "all",
) -> list[dict]:
    sql = "SELECT * FROM user_skills WHERE user_id=?"
    params: list[Any] = [user_id]
    if enabled is not None:
        sql += " AND enabled=?"
        params.append(1 if enabled else 0)
    if group_id != "all":
        if group_id is None or group_id == "" or group_id == "none":
            sql += " AND group_id IS NULL"
        else:
            gid = int(group_id)
            sql += " AND group_id=?"
            params.append(gid)
    sql += " ORDER BY name ASC"
    rows = await db.execute_fetchall(sql, tuple(params))
    gmap = await _group_name_map(db, user_id)
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        gname = ""
        if row.get("group_id") is not None:
            gname = gmap.get(int(row["group_id"]), "")
        if user:
            out.append(await enrich_skill_list_item(db, user_id, user, row, group_name=gname))
        else:
            out.append(public_skill_row(row, group_name=gname))
    return out


async def enrich_skill_list_item(_db, user_id: int, user: dict, row: dict, *, group_name: str = "") -> dict:
    slug = row.get("name") or ""
    fs_ok = skill_md_path(user, slug).is_file() if slug else False
    pub = public_skill_row(row, fs_exists=fs_ok, group_name=group_name)
    resources = list_skill_resource_files(user, slug) if slug else []
    pub["resources"] = [r for r in resources if r != "SKILL.md"]
    pub["resources_count"] = len(pub["resources"])
    pub["always_apply"] = False
    pub["disable_model_invocation"] = True
    pub["has_hooks_json"] = False
    pub["slash_only"] = False
    if fs_ok:
        try:
            content = await read_skill_content(user, slug)
            meta, _ = parse_skill_markdown(content)
            pub["always_apply"] = skill_should_always_apply(meta)
            dmi = _yaml_bool(meta.get("disable-model-invocation"))
            pub["disable_model_invocation"] = dmi if dmi is not None else True
            pub["slash_only"] = bool(pub["disable_model_invocation"]) and not pub["always_apply"]
            hooks_path = get_user_skills_root(user) / slug / "hooks.json"
            pub["has_hooks_json"] = hooks_path.is_file()
            if pub.get("hooks_enabled") and not pub["has_hooks_json"] and not (pub.get("pre_tool_use_matcher") or "").strip():
                pub["hooks_warning"] = "已启用 Hook 但缺少 hooks.json 且未配置 preToolUse matcher"
        except Exception:
            pass
    hints = collect_skill_name_warnings(slug)
    if hints:
        pub["name_hint"] = hints[0]
    return pub


async def maybe_sync_skill_from_disk(db, user_id: int, user: dict, raw_row: dict) -> None:
    slug = raw_row.get("name") or ""
    if not slug:
        return
    path = skill_md_path(user, slug)
    if not path.is_file():
        return
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    db_mtime = raw_row.get("file_mtime")
    if db_mtime is not None and abs(float(db_mtime) - float(mtime)) < 0.001:
        return
    await _upsert_row_from_file(db, user_id, user, slug, path)


async def get_user_skill(db, user_id: int, skill_id: int, user: dict | None = None) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM user_skills WHERE id=? AND user_id=?",
        (skill_id, user_id),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if user:
        db = await get_db()
        await maybe_sync_skill_from_disk(db, user_id, user, row)
        rows = await db.execute_fetchall(
            "SELECT * FROM user_skills WHERE id=? AND user_id=?",
            (skill_id, user_id),
        )
        if rows:
            row = dict(rows[0])
        gname = ""
        if row.get("group_id") is not None:
            gmap = await _group_name_map(db, user_id)
            gname = gmap.get(int(row["group_id"]), "")
        pub = await enrich_skill_list_item(db, user_id, user, row, group_name=gname)
        pub["content"] = await read_skill_content(user, row["name"])
        pub["hooks_json"] = read_hooks_json_text(user, row["name"])
        pub["command_files"] = [
            {"alias": c["alias"], "slash": c["slash"], "rel": c["rel"]}
            for c in iter_skill_command_files(user, row["name"])
        ]
        return pub
    return public_skill_row(row, fs_exists=None)


async def get_user_skill_raw(db, user_id: int, skill_id: int) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM user_skills WHERE id=? AND user_id=?",
        (skill_id, user_id),
    )
    return dict(rows[0]) if rows else None


async def get_user_skill_raw_by_name(db, user_id: int, name: str) -> dict | None:
    slug = normalize_skill_name(name)
    rows = await db.execute_fetchall(
        "SELECT * FROM user_skills WHERE user_id=? AND name=?",
        (user_id, slug),
    )
    return dict(rows[0]) if rows else None


def _scan_file_warnings(slug: str, meta: dict[str, Any], desc: str) -> list[str]:
    warnings: list[str] = []
    meta_name = str(meta.get("name") or "").strip()
    if meta_name:
        try:
            if normalize_skill_name(meta_name) != slug:
                warnings.append(
                    f"Skill「{slug}」: frontmatter name「{meta_name}」与目录名不一致，已按目录同步"
                )
        except ValueError:
            warnings.append(f"Skill「{slug}」: frontmatter name「{meta_name}」无效")
    warnings.extend(collect_description_warnings(slug, desc))
    return warnings


async def _upsert_row_from_file(
    db, user_id: int, user: dict, slug: str, path: Path
) -> tuple[str, list[str]]:
    content = path.read_text(encoding="utf-8", errors="replace")
    meta, _body = parse_skill_markdown(content)
    desc = trim_skill_description(str(meta.get("description") or ""))
    display = extract_display_title(meta, _body, slug)[:120]
    mtime = path.stat().st_mtime
    rel = skill_dir_relative(slug)
    warnings = _scan_file_warnings(slug, meta, desc)
    existing = await get_user_skill_raw_by_name(db, user_id, slug)
    if existing:
        await db.execute(
            """UPDATE user_skills SET display_name=?, description=?, skill_path=?,
               file_mtime=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?""",
            (display, desc[:2000], rel, mtime, int(existing["id"]), user_id),
        )
        await db.commit()
        return "updated", warnings
    cur = await db.execute(
        """INSERT INTO user_skills
           (user_id, name, display_name, description, skill_path, file_mtime)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, slug, display, desc[:2000], rel, mtime),
    )
    await db.commit()
    return "imported", warnings


async def prune_user_skills_missing_on_disk(
    db,
    user_id: int,
    user: dict,
    *,
    found_slugs: set[str] | None = None,
) -> dict:
    """删除磁盘上已无 SKILL.md 的数据库行（改名视为删旧增新，由 scan 分别处理）。"""
    rows = await db.execute_fetchall(
        "SELECT id, name FROM user_skills WHERE user_id=?",
        (user_id,),
    )
    removed: list[str] = []
    for r in rows:
        slug = str(r["name"] or "").strip()
        if not slug:
            continue
        if found_slugs is not None and slug in found_slugs:
            continue
        if skill_md_path(user, slug).is_file():
            continue
        await db.execute(
            "DELETE FROM user_skills WHERE id=? AND user_id=?",
            (int(r["id"]), user_id),
        )
        removed.append(slug)
    if removed:
        await db.commit()
    return {"removed": len(removed), "removed_names": removed}


async def scan_user_skills_from_disk(db, user_id: int, user: dict) -> dict:
    """以磁盘 skills/<name>/SKILL.md 为准双向同步：导入/更新 + 清理库中孤儿行。"""
    root = get_user_skills_root(user)
    found: list[str] = []
    found_set: set[str] = set()
    imported = updated = skipped = invalid = 0
    warnings: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        md = child / "SKILL.md"
        if not md.is_file():
            continue
        try:
            slug = normalize_skill_name(child.name)
        except ValueError:
            invalid += 1
            skipped += 1
            warnings.append(f"跳过无效目录名「{child.name}」（须小写字母开头，a-z、0-9、-、_）")
            continue
        action, w = await _upsert_row_from_file(db, user_id, user, slug, md)
        warnings.extend(w)
        if action == "imported":
            imported += 1
        elif action == "updated":
            updated += 1
        else:
            skipped += 1
        found.append(slug)
        found_set.add(slug)
    prune = await prune_user_skills_missing_on_disk(
        db, user_id, user, found_slugs=found_set
    )
    if prune["removed_names"]:
        for slug in prune["removed_names"]:
            warnings.append(f"磁盘已不存在 Skill「{slug}」，已从库中移除")
    return {
        "success": True,
        "scanned": found,
        "count": len(found),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "invalid": invalid,
        "removed": prune["removed"],
        "removed_names": prune["removed_names"],
        "warnings": warnings,
    }


async def create_user_skill(
    db,
    user_id: int,
    user: dict,
    *,
    name: str,
    display_name: str = "",
    description: str = "",
    content: str | None = None,
    enabled: bool = True,
    chat_enabled: bool = True,
    chat_scope_web: bool = True,
    chat_scope_host: bool = True,
    chat_scope_integration: bool = False,
    group_id: int | None = None,
    slash_name: str = "",
    hooks_enabled: bool = False,
    pre_tool_use_matcher: str = "",
    pre_tool_use_decision: str = "ask",
    allowed_tools: str = "",
    hooks_json: str | None = None,
) -> dict:
    slug = normalize_skill_name(name)
    existing = await get_user_skill_raw_by_name(db, user_id, slug)
    if existing:
        raise ValueError(f"Skill「{slug}」已存在")
    path = skill_md_path(user, slug)
    if path.is_file():
        raise ValueError(f"目录 {skill_dir_relative(slug)} 已存在 SKILL.md")
    md = (
        content
        if content is not None and str(content).strip()
        else render_skill_markdown(
            name=slug,
            description=description or display_name,
        )
    )
    path = await write_skill_content(user, slug, md)
    meta, body = parse_skill_markdown(md)
    desc = trim_skill_description(description or str(meta.get("description") or ""))
    disp = (display_name or extract_display_title(meta, body, slug))[:120]
    rel = skill_dir_relative(slug)
    mtime = path.stat().st_mtime
    if group_id is not None:
        rows_g = await db.execute_fetchall(
            "SELECT id FROM user_skill_groups WHERE id=? AND user_id=?",
            (group_id, user_id),
        )
        if not rows_g:
            raise ValueError("分组不存在")
    slash = (slash_name or "").strip().lstrip("/") or slug
    try:
        slash = normalize_skill_name(slash)
    except ValueError:
        slash = slug
    decision = normalize_pre_tool_use_decision(pre_tool_use_decision, "ask")
    cur = await db.execute(
        """INSERT INTO user_skills
           (user_id, name, display_name, description, skill_path, enabled, chat_enabled,
            chat_scope_web, chat_scope_host, chat_scope_integration, file_mtime, group_id,
            slash_name, hooks_enabled, pre_tool_use_matcher, pre_tool_use_decision, allowed_tools)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            slug,
            disp,
            desc,
            rel,
            1 if enabled else 0,
            1 if chat_enabled else 0,
            1 if chat_scope_web else 0,
            1 if chat_scope_host else 0,
            1 if chat_scope_integration else 0,
            mtime,
            group_id,
            slash,
            1 if hooks_enabled else 0,
            (pre_tool_use_matcher or "").strip()[:500],
            decision,
            (allowed_tools or "").strip()[:2000],
        ),
    )
    await db.commit()
    if hooks_json is not None:
        write_hooks_json_text(user, slug, hooks_json)
    row = await get_user_skill(db, user_id, int(cur.lastrowid), user)
    return row or {}


async def update_user_skill(
    db,
    user_id: int,
    user: dict,
    skill_id: int,
    *,
    display_name: str | None = None,
    description: str | None = None,
    content: str | None = None,
    enabled: bool | None = None,
    chat_enabled: bool | None = None,
    chat_scope_web: bool | None = None,
    chat_scope_host: bool | None = None,
    chat_scope_integration: bool | None = None,
    group_id: int | None | str = ...,
    slash_name: str | None = None,
    hooks_enabled: bool | None = None,
    pre_tool_use_matcher: str | None = None,
    pre_tool_use_decision: str | None = None,
    allowed_tools: str | None = None,
    hooks_json: str | None = ...,
) -> dict:
    raw = await get_user_skill_raw(db, user_id, skill_id)
    if not raw:
        raise LookupError("Skill 不存在")
    slug = raw["name"]
    if content is not None:
        path = await write_skill_content(user, slug, content)
        meta, body = parse_skill_markdown(content)
        if description is None:
            description = trim_skill_description(str(meta.get("description") or ""))
        if display_name is None:
            display_name = extract_display_title(meta, body, slug)[:120]
        mtime = path.stat().st_mtime
    else:
        mtime = None
    gid_sql = None
    if group_id is not ...:
        if group_id is None or group_id == "":
            gid_sql = None
        else:
            gid_sql = await resolve_skill_group_id(db, user_id, group_id)
    slash_val = None
    if slash_name is not None:
        s = (slash_name or "").strip().lstrip("/") or slug
        try:
            slash_val = normalize_skill_name(s)
        except ValueError:
            slash_val = slug
    decision_val = None
    if pre_tool_use_decision is not None:
        decision_val = normalize_pre_tool_use_decision(pre_tool_use_decision, "ask")
    await db.execute(
        """UPDATE user_skills SET
           display_name=COALESCE(?, display_name),
           description=COALESCE(?, description),
           enabled=COALESCE(?, enabled),
           chat_enabled=COALESCE(?, chat_enabled),
           chat_scope_web=COALESCE(?, chat_scope_web),
           chat_scope_host=COALESCE(?, chat_scope_host),
           chat_scope_integration=COALESCE(?, chat_scope_integration),
           slash_name=COALESCE(?, slash_name),
           hooks_enabled=COALESCE(?, hooks_enabled),
           pre_tool_use_matcher=COALESCE(?, pre_tool_use_matcher),
           pre_tool_use_decision=COALESCE(?, pre_tool_use_decision),
           allowed_tools=COALESCE(?, allowed_tools),
           group_id=CASE WHEN ? THEN group_id ELSE ? END,
           file_mtime=COALESCE(?, file_mtime),
           updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (
            display_name,
            description,
            None if enabled is None else (1 if enabled else 0),
            None if chat_enabled is None else (1 if chat_enabled else 0),
            None if chat_scope_web is None else (1 if chat_scope_web else 0),
            None if chat_scope_host is None else (1 if chat_scope_host else 0),
            None if chat_scope_integration is None else (1 if chat_scope_integration else 0),
            slash_val,
            None if hooks_enabled is None else (1 if hooks_enabled else 0),
            None if pre_tool_use_matcher is None else (pre_tool_use_matcher or "").strip()[:500],
            decision_val,
            None if allowed_tools is None else (allowed_tools or "").strip()[:2000],
            group_id is ...,
            gid_sql,
            mtime,
            skill_id,
            user_id,
        ),
    )
    await db.commit()
    if hooks_json is not ...:
        write_hooks_json_text(user, slug, hooks_json)
    row = await get_user_skill(db, user_id, skill_id, user)
    return row or {}


async def delete_user_skill(db, user_id: int, user: dict, skill_id: int, *, remove_files: bool = False) -> bool:
    raw = await get_user_skill_raw(db, user_id, skill_id)
    if not raw:
        return False
    slug = raw["name"]
    cur = await db.execute("DELETE FROM user_skills WHERE id=? AND user_id=?", (skill_id, user_id))
    await db.commit()
    if remove_files:
        import shutil
        d = get_user_skills_root(user) / slug
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    return cur.rowcount > 0


async def list_chat_enabled_skills_for_context(
    db,
    user_id: int,
    user: dict,
    session_scope: str | None,
    session_host_id: int | None = None,
    *,
    inject_user_skills: bool = False,
) -> list[dict]:
    if not await user_skills_feature_enabled(db, user_id):
        return []
    scope = (session_scope or "default").strip().lower() or "default"
    # task 默认不注入；任务表 inject_user_skills=1 时允许注入 prompt（仍不暴露 CRUD 工具）
    if scope == "task" and not inject_user_skills:
        return []
    rows = await db.execute_fetchall(
        """SELECT * FROM user_skills
           WHERE user_id=? AND enabled=1 AND chat_enabled=1
           ORDER BY name ASC""",
        (user_id,),
    )
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        if scope == "task":
            pass  # task 注入时不做 web/host 场景过滤
        elif scope in ("integration", "mcp_orchestrate", "mcp_runtime"):
            if not bool(row.get("chat_scope_integration", 0)):
                continue
        elif session_host_id:
            if not bool(row.get("chat_scope_host", 1)):
                continue
        else:
            if not bool(row.get("chat_scope_web", 1)):
                continue
        path = skill_md_path(user, row["name"])
        if not path.is_file():
            continue
        out.append(row)
    return out
