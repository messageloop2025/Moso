"""将用户 Skills 注入 AI system prompt（渐进式披露）。"""

from __future__ import annotations

import logging

import config
from database import get_db
from services.user_skills_registry import (
    list_chat_enabled_skills_for_context,
    list_skill_resource_files,
    parse_skill_markdown,
    read_skill_content,
    skill_should_always_apply,
    user_skills_feature_enabled,
)

logger = logging.getLogger("edgeops.user_skills.runtime")

_USER_SKILLS_HINT = (
    "\n\n**个人 Agent Skills（Cursor 兼容 · 渐进式披露）**："
    "磁盘根路径固定为 `web/fs/<用户>/skills/<name>/`（**不是** `chats/<日期>/skills/`）。"
    "目录结构：`SKILL.md`（YAML frontmatter + Markdown），可选 `reference.md`、`examples.md`、`scripts/`。\n"
    "**文件操作须用 Skill 专用工具**（勿用 fs_write_file / fs_mkdir / fs_delete，否则会被错误归位到 chats/ 日期目录）：\n"
    "- `save_user_skill`：写 SKILL.md\n"
    "- `write_user_skill_file` / `read_user_skill_file` / `delete_user_skill_file`：附属文件\n"
    "- `list_user_skill_files`：列出目录内全部文件\n"
    "- `list_user_skills`、`get_user_skill`、`delete_user_skill`、`scan_user_skills`、"
    "`export_user_skills_config` / `import_user_skills_config`\n\n"
    "**frontmatter 约定**（与 Cursor Agent Skills 对齐）：\n"
    "- `name`：小写标识；`description`：第三人称，含 **做什么 + 何时触发**（≤1024 字符）\n"
    "- `disable-model-invocation: true`（**默认**）：仅目录披露，匹配时先 `get_user_skill`\n"
    "- `disable-model-invocation: false` 或 `always-apply: true`：正文内联进 system prompt\n\n"
    "**创建 Skill 时**：正文宜简洁（Quick start）；详细内容用 `write_user_skill_file` 写 `reference.md`，"
    "SKILL.md 中链接并注明用 `read_user_skill_file` 加载。\n"
    "**当用户要求创建/编写 Skill**：调用 `save_user_skill`；`description` 写清触发语；默认 `disable-model-invocation: true`；"
    "用户已明确要求时视为已授权，无需 ask_user_choice 二次确认。"
)


def _format_skill_body_block(title: str, name: str, desc: str, body: str, per_max: int) -> str:
    body = (body or "").strip()
    if len(body) > per_max:
        body = body[:per_max] + "\n\n…（Skill 正文已截断）"
    block = f"\n\n### Skill: {title} (`{name}`)"
    if desc:
        block += f"\n> {desc}"
    if body:
        block += f"\n\n{body}"
    return block


async def _build_progressive_section(
    user: dict,
    rows: list[dict],
    *,
    per_max: int,
    total_max: int,
) -> str:
    parts: list[str] = [
        _USER_SKILLS_HINT,
        "\n\n## 用户 Skills 目录（渐进式披露 · 高优先级）",
        "\n默认**仅**下列 `name` + `description`；任务匹配某 Skill 时，**须先** `get_user_skill(name=...)` 加载完整 SKILL.md，"
        "再执行；若正文指向 reference/examples，用 `read_user_skill_file` 按需读取。",
        "标注「已内联」者已注入正文，可直接遵循。",
    ]
    catalog: list[str] = []
    inline_blocks: list[str] = []
    total = len("".join(parts))

    for row in rows:
        name = row.get("name") or ""
        try:
            content = await read_skill_content(user, name)
        except Exception as e:
            logger.warning("read skill %s user=%s: %s", name, user.get("id"), e)
            continue
        meta, body = parse_skill_markdown(content)
        desc = (row.get("description") or meta.get("description") or "").strip()
        title = row.get("display_name") or name
        resources = [f for f in list_skill_resource_files(user, name) if f != "SKILL.md"]
        res_hint = ""
        if resources:
            shown = resources[:6]
            extra = len(resources) - len(shown)
            res_hint = "；附件: " + ", ".join(shown) + (f" 等{extra}个" if extra > 0 else "")

        always = skill_should_always_apply(meta)
        if always and (body or "").strip():
            block = _format_skill_body_block(title, name, desc, body, per_max)
            if total + len(block) <= total_max:
                catalog.append(f"- **`{name}`**（已内联）: {desc or '（无 description）'}{res_hint}")
                inline_blocks.append(block)
                total += len(block)
                continue
            catalog.append(f"- **`{name}`**（须 get_user_skill，内联预算已满）: {desc or '（无 description）'}{res_hint}")
        else:
            catalog.append(f"- **`{name}`**（按需 get_user_skill）: {desc or '（无 description，请补充 frontmatter description）'}{res_hint}")

    if catalog:
        parts.append("\n" + "\n".join(catalog))
    if inline_blocks:
        parts.append("\n\n### 已内联 Skills（always-apply 或 disable-model-invocation: false）")
        parts.extend(inline_blocks)
    return "".join(parts)


async def _build_eager_section(
    user: dict,
    rows: list[dict],
    *,
    per_max: int,
    total_max: int,
) -> str:
    parts: list[str] = [_USER_SKILLS_HINT, "\n\n## 用户 Skills 指令（高优先级，须遵守）"]
    total = 0
    for row in rows:
        name = row.get("name") or ""
        try:
            content = await read_skill_content(user, name)
        except Exception as e:
            logger.warning("read skill %s user=%s: %s", name, user.get("id"), e)
            continue
        meta, body = parse_skill_markdown(content)
        desc = (row.get("description") or meta.get("description") or "").strip()
        title = row.get("display_name") or name
        block = _format_skill_body_block(title, name, desc, body, per_max)
        if total + len(block) > total_max:
            parts.append("\n\n…（更多 Skills 因上下文预算未全部注入）")
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)


_USER_SKILLS_DISABLED_HINT = (
    "\n\n**个人 Agent Skills**：当前账号尚未开启 Skills 功能（须管理员在「用户管理」为您开启，含管理员本人）。"
    "用户若要求创建/使用 Skill，请说明需先联系管理员开启；开启后可在「Skills」页或对话中通过 "
    "`save_user_skill` 管理 `web/fs/<用户>/skills/<name>/SKILL.md`。"
)


async def build_user_skills_system_section(
    user: dict,
    session_scope: str | None,
    session_host_id: int | None = None,
) -> str:
    uid = int(user["id"])
    db = await get_db()
    if not await user_skills_feature_enabled(db, uid):
        return _USER_SKILLS_DISABLED_HINT
    rows = await list_chat_enabled_skills_for_context(
        db, uid, user, session_scope, session_host_id
    )
    if not rows:
        return _USER_SKILLS_HINT
    per_max = max(1000, int(config.USER_SKILLS_BODY_MAX_CHARS))
    total_max = max(per_max, int(config.USER_SKILLS_TOTAL_MAX_CHARS))
    if getattr(config, "USER_SKILLS_PROGRESSIVE_DISCLOSURE", True):
        return await _build_progressive_section(
            user, rows, per_max=per_max, total_max=total_max
        )
    return await _build_eager_section(
        user, rows, per_max=per_max, total_max=total_max
    )
