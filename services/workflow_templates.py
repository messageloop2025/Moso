"""AI 编排工作流模板：`delegate_chain` 的 payload 持久化 + 复用。

每条模板属于某个 owner（创建它的用户）；`payload` 是 JSON 化的 `delegate_chain`
参数字典（含 host_id / steps / stop_on_failure 等）。运行时可用 `variable_overrides`
在 payload 的字符串字段里做简单字符串替换（占位符 `${var}` 的风格，与 `{prev_*}`
区分开避免冲突）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from database import get_db


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# payload 中允许做 ${var} 替换的字符串字段白名单（避免误伤任何结构化字段）
_SUBSTITUTABLE_STEP_KEYS = {"task", "command", "workdir", "extra_args", "model", "output_format", "command_template"}


def _substitute_in_value(val: Any, vars_: dict[str, str]) -> Any:
    if isinstance(val, str):
        def _repl(m: re.Match) -> str:
            return vars_.get(m.group(1), m.group(0))
        return _VAR_RE.sub(_repl, val)
    if isinstance(val, list):
        return [_substitute_in_value(x, vars_) for x in val]
    if isinstance(val, dict):
        return {k: _substitute_in_value(v, vars_) for k, v in val.items()}
    return val


def apply_variables(payload: dict[str, Any], vars_: dict[str, str] | None) -> dict[str, Any]:
    """在 payload 里对白名单字段做 ${var} 替换；env 的值也会替换。"""
    if not vars_:
        return payload
    out = dict(payload)
    # 顶层 host_id 等结构化字段不碰
    steps = out.get("steps") or []
    new_steps = []
    for step in steps:
        if not isinstance(step, dict):
            new_steps.append(step)
            continue
        new_step = dict(step)
        for k in _SUBSTITUTABLE_STEP_KEYS:
            if k in new_step and isinstance(new_step[k], str):
                new_step[k] = _substitute_in_value(new_step[k], vars_)
        # env 的 value 也做替换
        if isinstance(new_step.get("env"), dict):
            new_step["env"] = {
                k: (_substitute_in_value(v, vars_) if isinstance(v, str) else v)
                for k, v in new_step["env"].items()
            }
        new_steps.append(new_step)
    if new_steps:
        out["steps"] = new_steps
    return out


def extract_declared_variables(payload: dict[str, Any]) -> list[str]:
    """扫描 payload 中的 `${var}` 占位符，返回去重后的变量名列表（供用户知道该传哪些）。"""
    seen: list[str] = []
    seen_set: set[str] = set()

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            for m in _VAR_RE.finditer(v):
                name = m.group(1)
                if name not in seen_set:
                    seen_set.add(name)
                    seen.append(name)
        elif isinstance(v, list):
            for x in v:
                _walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x)

    _walk(payload)
    return seen


async def save_template(
    *,
    owner_user_id: int,
    name: str,
    payload: dict[str, Any],
    description: str = "",
    kind: str = "delegate_chain",
    tags: str = "",
    visibility: str = "private",
    overwrite: bool = False,
) -> dict[str, Any]:
    """保存或更新一个模板，按 (owner_user_id, name) 唯一。"""
    if not name or not name.strip():
        raise ValueError("模板 name 不能为空")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 dict")
    if visibility not in ("private", "org"):
        raise ValueError("visibility 只能是 private / org")
    payload_json = json.dumps(payload, ensure_ascii=False)
    db = await get_db()
    existing = await db.execute_fetchall(
        "SELECT id FROM ai_workflow_templates WHERE owner_user_id = ? AND name = ?",
        (owner_user_id, name.strip()),
    )
    if existing and not overwrite:
        return {
            "success": False,
            "error": f"名为 {name!r} 的模板已存在（id={existing[0]['id']}）；如要覆盖请传 overwrite=true 或改名",
            "existing_id": existing[0]["id"],
        }
    if existing:
        tid = existing[0]["id"]
        await db.execute(
            """UPDATE ai_workflow_templates
               SET description = ?, kind = ?, payload = ?, tags = ?, visibility = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (description, kind, payload_json, tags, visibility, tid),
        )
        await db.commit()
        return {"success": True, "id": tid, "updated": True, "name": name.strip()}
    cur = await db.execute(
        """INSERT INTO ai_workflow_templates
           (owner_user_id, name, description, kind, payload, tags, visibility)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (owner_user_id, name.strip(), description, kind, payload_json, tags, visibility),
    )
    await db.commit()
    return {"success": True, "id": cur.lastrowid, "created": True, "name": name.strip()}


async def list_templates(
    *,
    owner_user_id: int,
    include_org: bool = True,
    query: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列模板：own 的 + 可见性为 org 的（by other users）。按 updated_at 倒序。"""
    db = await get_db()
    sql = """SELECT id, owner_user_id, name, description, kind, tags, visibility,
                    last_run_at, run_count, created_at, updated_at
             FROM ai_workflow_templates
             WHERE (owner_user_id = ?"""
    params: list[Any] = [owner_user_id]
    if include_org:
        sql += " OR visibility = 'org'"
    sql += ")"
    if query and query.strip():
        q = f"%{query.strip()}%"
        sql += " AND (name LIKE ? OR description LIKE ? OR tags LIKE ?)"
        params.extend([q, q, q])
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(200, int(limit or 50))))
    rows = await db.execute_fetchall(sql, tuple(params))
    return [dict(r) for r in rows]


async def get_template(*, template_id: int, user_id: int) -> dict[str, Any] | None:
    """取单个模板（含 payload）。owner 或 visibility=org 可读。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, owner_user_id, name, description, kind, payload, tags,
                  visibility, last_run_at, run_count, created_at, updated_at
           FROM ai_workflow_templates WHERE id = ?""",
        (template_id,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if row["owner_user_id"] != user_id and row["visibility"] != "org":
        return None
    try:
        row["payload_obj"] = json.loads(row["payload"])
    except Exception:
        row["payload_obj"] = {}
    return row


async def delete_template(*, template_id: int, user_id: int, is_admin: bool = False) -> bool:
    """删模板：owner 或管理员可删。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT owner_user_id FROM ai_workflow_templates WHERE id = ?",
        (template_id,),
    )
    if not rows:
        return False
    if rows[0]["owner_user_id"] != user_id and not is_admin:
        return False
    await db.execute("DELETE FROM ai_workflow_templates WHERE id = ?", (template_id,))
    await db.commit()
    return True


async def mark_run(*, template_id: int) -> None:
    """记录一次运行：run_count + 1，last_run_at = now。"""
    db = await get_db()
    await db.execute(
        """UPDATE ai_workflow_templates
           SET run_count = COALESCE(run_count, 0) + 1, last_run_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (template_id,),
    )
    await db.commit()
