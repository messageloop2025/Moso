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
    "目录：`SKILL.md`，可选 `reference.md`、`examples.md`、`scripts/`、`hooks.json`、`commands/*.md`。\n"
    "**文件操作须用 Skill 专用工具**（勿用 fs_write_file / fs_mkdir / fs_delete）：\n"
    "- `save_user_skill`：写 SKILL.md，并可同时配置 `slash_name` / Hook / `allowed_tools` / `hooks_json`\n"
    "- `write_user_skill_file`：附属文件，含 `hooks.json`、`commands/<alias>.md`、`reference.md`、`scripts/*`\n"
    "- `read_user_skill_file` / `list_user_skill_files` / `delete_user_skill_file`；"
    "`list_user_skills`、`get_user_skill`、`delete_user_skill`、`scan_user_skills`、导入导出；"
    "分组：`list/create/update/delete_user_skill_group`、`assign_user_skills_to_group`\n\n"
    "**frontmatter**：`name` 小写；`description` 第三人称 WHAT+WHEN；"
    "默认 `disable-model-invocation: true`（须 `get_user_skill`）；"
    "`false` 或 `always-apply: true` 则内联正文。\n\n"
    "**斜杠 Command（须会创建）**：\n"
    "- 用户聊天输入 `/` 可选命令；消息以 `/name 参数` 开头时平台强制加载全文。\n"
    "- `save_user_skill(..., slash_name=\"my-cmd\")` 设置斜杠名（默认等于 name）。\n"
    "- 正文用 `{{arg}}` / `$ARGUMENTS` / `{{arg1}}` 接收用户参数；例：`检查主机 {{arg}} 的磁盘`。\n"
    "- 子命令：`write_user_skill_file(name, path=\"commands/check-disk.md\", content=\"...\")`，"
    "用户可用 `/check-disk host1` 唤起（同样支持占位符）。\n"
    "- 可选 `allowed_tools`：仅当用户**本轮斜杠唤起**该 Skill 时强制白名单（如 `ssh_execute,list_hosts`）。\n\n"
    "**Hook（须会创建）**：\n"
    "- 作用：在 AI 调工具前/后由平台拦截（与聊天「严格模式」正交；`ask` 弹独立确认框）。\n"
    "- 方式 A：`save_user_skill(..., hooks_enabled=true, pre_tool_use_matcher=\"ssh_execute,send_to_terminal\",\n"
    "  pre_tool_use_decision=\"ask\"|\"deny\"|\"allow\")`。\n"
    "- 方式 B：`hooks_json` 传入 JSON 字符串或对象，或 `write_user_skill_file(..., path=\"hooks.json\", content=...)`；"
    "事件：`preToolUse` / `postToolUse` / `postToolUseFailure` / `sessionStart` / `sessionEnd` / `beforeMCPExecution`；"
    "decision=`allow`|`deny`|`ask`。有 hooks.json 规则时优先于 DB matcher。\n"
    "- `postToolUse`/`postToolUseFailure` 若 `deny`：结果视为失败且正文省略，不得继续采信。\n"
    "- 解析失败默认放行（fail-open）。多 Skill：deny > ask > allow。\n\n"
    "**创建示例（用户说「做一个确认后才允许 SSH 的运维 Skill」时按此调用）**：\n"
    "1) `save_user_skill(name=\"safe-ssh\", description=\"...\", slash_name=\"safe-ssh\",\n"
    "   hooks_enabled=true, pre_tool_use_matcher=\"ssh_execute\", pre_tool_use_decision=\"ask\",\n"
    "   allowed_tools=\"ssh_execute,get_terminal_buffer,list_hosts\",\n"
    "   body=\"# Safe SSH\\n\\n目标：`{{arg}}`\\n\\n步骤…\")`\n"
    "2) 需要细规则时再 `write_user_skill_file` 写 `hooks.json` 或 `commands/*.md`。\n"
    "**当用户要求创建/编写 Skill**：必须调用工具落地，勿只口头描述；"
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
    *,
    inject_user_skills: bool = False,
) -> str:
    uid = int(user["id"])
    db = await get_db()
    if not await user_skills_feature_enabled(db, uid):
        return _USER_SKILLS_DISABLED_HINT
    rows = await list_chat_enabled_skills_for_context(
        db,
        uid,
        user,
        session_scope,
        session_host_id,
        inject_user_skills=inject_user_skills,
    )
    org_extra = ""
    try:
        org_rows = await db.execute_fetchall(
            "SELECT name, display_name, description, content FROM org_skills WHERE enabled=1 ORDER BY name ASC LIMIT 40"
        )
        if org_rows:
            parts = ["\n\n## 组织 Skills（只读，须遵守）"]
            for orow in org_rows:
                title = (orow["display_name"] or orow["name"] or "").strip()
                desc = (orow["description"] or "").strip()[:200]
                body = (orow["content"] or "").strip()[:2000]
                parts.append(f"\n### {title}\n{desc}\n{body}")
            org_extra = "".join(parts)
    except Exception:
        org_extra = ""
    if not rows:
        return _USER_SKILLS_HINT + org_extra
    per_max = max(1000, int(config.USER_SKILLS_BODY_MAX_CHARS))
    total_max = max(per_max, int(config.USER_SKILLS_TOTAL_MAX_CHARS))
    if getattr(config, "USER_SKILLS_PROGRESSIVE_DISCLOSURE", True):
        base = await _build_progressive_section(
            user, rows, per_max=per_max, total_max=total_max
        )
    else:
        base = await _build_eager_section(
            user, rows, per_max=per_max, total_max=total_max
        )
    return base + org_extra
