"""每用户多组 AI 模型配置（Profile）与当前激活项。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("edgeops.ai_model_profiles")

MAX_PROFILES_PER_USER = 20
DEFAULT_PROFILE_NAME = "默认配置"


async def _has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return any(r[1] == column for r in rows)


async def ensure_profiles_schema(db) -> None:
    """建表并补齐 active_profile_id；将旧 user_ai_config 单行迁移为首个「默认配置」Profile。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_ai_model_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            base_url TEXT DEFAULT '',
            model TEXT DEFAULT '',
            system_prompt TEXT DEFAULT '',
            auto_approve TEXT DEFAULT 'false',
            assistant_enabled TEXT DEFAULT 'false',
            context_size TEXT DEFAULT '0',
            agent_max_steps TEXT DEFAULT '',
            assistant_max_rounds TEXT DEFAULT '',
            provider TEXT DEFAULT '',
            vision_enabled TEXT DEFAULT 'true',
            ai_output_locale TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_ai_model_profiles_user ON user_ai_model_profiles(user_id)"
    )
    if not await _has_column(db, "user_ai_config", "active_profile_id"):
        await db.execute(
            "ALTER TABLE user_ai_config ADD COLUMN active_profile_id INTEGER REFERENCES user_ai_model_profiles(id)"
        )
    await db.commit()
    await _backfill_legacy_configs(db)
    await normalize_default_profile_names(db)


async def normalize_default_profile_names(db) -> None:
    """将历史 Profile 名「默认」统一为「默认配置」，并处理与已有「默认配置」的冲突。"""
    rows = await db.execute_fetchall(
        "SELECT id, user_id FROM user_ai_model_profiles WHERE name = ?",
        ("默认",),
    )
    if not rows:
        return
    for row in rows:
        rid = int(row["id"])
        uid = int(row["user_id"])
        dup = await db.execute_fetchall(
            "SELECT id FROM user_ai_model_profiles WHERE user_id = ? AND name = ?",
            (uid, DEFAULT_PROFILE_NAME),
        )
        if dup:
            target_id = int(dup[0]["id"])
            cfg = await db.execute_fetchall(
                "SELECT active_profile_id FROM user_ai_config WHERE user_id = ?",
                (uid,),
            )
            active_id = cfg[0]["active_profile_id"] if cfg else None
            if active_id is not None and int(active_id) == rid:
                await db.execute(
                    "UPDATE user_ai_config SET active_profile_id = ? WHERE user_id = ?",
                    (target_id, uid),
                )
            await db.execute(
                "DELETE FROM user_ai_model_profiles WHERE id = ? AND user_id = ?",
                (rid, uid),
            )
        else:
            await db.execute(
                "UPDATE user_ai_model_profiles SET name = ? WHERE id = ? AND user_id = ?",
                (DEFAULT_PROFILE_NAME, rid, uid),
            )
    await db.commit()


async def _backfill_legacy_configs(db) -> None:
    rows = await db.execute_fetchall("SELECT * FROM user_ai_config")
    for row in rows:
        r = dict(row)
        uid = int(r["user_id"])
        existing = await db.execute_fetchall(
            "SELECT id FROM user_ai_model_profiles WHERE user_id = ? LIMIT 1", (uid,)
        )
        if existing:
            if not r.get("active_profile_id"):
                pid = int(existing[0]["id"])
                await db.execute(
                    "UPDATE user_ai_config SET active_profile_id = ? WHERE user_id = ?",
                    (pid, uid),
                )
            continue
        cur = await db.execute(
            """
            INSERT INTO user_ai_model_profiles (
                user_id, name, api_key, base_url, model, system_prompt,
                auto_approve, assistant_enabled, context_size,
                agent_max_steps, assistant_max_rounds, provider,
                vision_enabled, ai_output_locale, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                uid,
                DEFAULT_PROFILE_NAME,
                (r.get("api_key") or "").strip(),
                (r.get("base_url") or "").strip().rstrip("/"),
                (r.get("model") or "").strip(),
                (r.get("system_prompt") or "").strip(),
                (r.get("auto_approve") or "false").strip().lower(),
                (r.get("assistant_enabled") or "false").strip().lower(),
                (r.get("context_size") or "0").strip(),
                (r.get("agent_max_steps") or "").strip(),
                (r.get("assistant_max_rounds") or "").strip(),
                (r.get("provider") or "").strip(),
                (r.get("vision_enabled") or "true").strip().lower(),
                (r.get("ai_output_locale") or "").strip(),
            ),
        )
        pid = int(cur.lastrowid)
        await db.execute(
            "UPDATE user_ai_config SET active_profile_id = ? WHERE user_id = ?",
            (pid, uid),
        )
    await db.commit()


def profile_row_to_settings(row: dict) -> dict[str, str]:
    """Profile 行 → _get_user_ai_settings 兼容的 ai_* 字典（不含全局 fallback）。"""
    vision_raw = row.get("vision_enabled")
    return {
        "ai_api_key": (row.get("api_key") or "").strip(),
        "ai_base_url": (row.get("base_url") or "").strip().rstrip("/"),
        "ai_model": (row.get("model") or "").strip(),
        "ai_system_prompt": (row.get("system_prompt") or "").strip(),
        "ai_auto_approve": (row.get("auto_approve") or "false").strip().lower(),
        "ai_assistant_enabled": (row.get("assistant_enabled") or "false").strip().lower(),
        "ai_context_size": (row.get("context_size") or "0").strip(),
        "ai_agent_max_steps": (row.get("agent_max_steps") or "").strip(),
        "ai_assistant_max_rounds": (row.get("assistant_max_rounds") or "").strip(),
        "ai_provider": (row.get("provider") or "").strip(),
        "ai_vision_enabled": ((vision_raw or "true").strip().lower() or "true"),
        "ai_output_locale": (row.get("ai_output_locale") or "").strip(),
    }


def public_profile_summary(row: dict, *, active_id: int | None) -> dict[str, Any]:
    rid = int(row["id"])
    return {
        "id": rid,
        "name": (row.get("name") or "").strip() or DEFAULT_PROFILE_NAME,
        "model": (row.get("model") or "").strip(),
        "base_url": (row.get("base_url") or "").strip().rstrip("/"),
        "provider": (row.get("provider") or "").strip(),
        "api_key_set": bool((row.get("api_key") or "").strip()),
        "is_active": active_id is not None and rid == int(active_id),
        "updated_at": row.get("updated_at"),
    }


async def get_active_profile_id(db, user_id: int) -> int | None:
    await ensure_profiles_schema(db)
    rows = await db.execute_fetchall(
        "SELECT active_profile_id FROM user_ai_config WHERE user_id = ?", (user_id,)
    )
    if not rows or rows[0]["active_profile_id"] is None:
        return None
    try:
        return int(rows[0]["active_profile_id"])
    except (TypeError, ValueError):
        return None


async def get_active_profile_row(db, user_id: int) -> dict | None:
    pid = await get_active_profile_id(db, user_id)
    if pid is None:
        return None
    rows = await db.execute_fetchall(
        "SELECT * FROM user_ai_model_profiles WHERE id = ? AND user_id = ?",
        (pid, user_id),
    )
    return dict(rows[0]) if rows else None


async def list_profiles(db, user_id: int) -> tuple[list[dict], int | None]:
    await ensure_profiles_schema(db)
    active_id = await get_active_profile_id(db, user_id)
    rows = await db.execute_fetchall(
        "SELECT * FROM user_ai_model_profiles WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    )
    items = [public_profile_summary(dict(r), active_id=active_id) for r in rows]
    return items, active_id


async def get_profile_row_by_name(db, user_id: int, name: str) -> dict | None:
    await ensure_profiles_schema(db)
    n = (name or "").strip()
    if not n:
        return None
    rows = await db.execute_fetchall(
        "SELECT * FROM user_ai_model_profiles WHERE user_id = ? AND name = ?",
        (user_id, n),
    )
    return dict(rows[0]) if rows else None


async def get_resolved_user_ai_settings(db, user_id: int) -> dict[str, str]:
    """获取用户 AI 配置：优先当前激活 Profile，缺项用全局 settings 补全。返回 ai_* 键字典。"""
    keys = [
        "ai_api_key", "ai_base_url", "ai_model", "ai_system_prompt",
        "ai_auto_approve", "ai_assistant_enabled", "ai_context_size",
        "ai_agent_max_steps", "ai_assistant_max_rounds", "ai_provider",
        "ai_vision_enabled", "ai_output_locale",
    ]
    out: dict[str, str] = {}
    await ensure_profiles_schema(db)
    prof = await get_active_profile_row(db, user_id)
    if prof:
        out = profile_row_to_settings(prof)
    else:
        row = await db.execute_fetchall("SELECT * FROM user_ai_config WHERE user_id = ?", (user_id,))
        if row:
            r = dict(row[0])
            out["ai_api_key"] = (r.get("api_key") or "").strip()
            out["ai_base_url"] = (r.get("base_url") or "").strip()
            out["ai_model"] = (r.get("model") or "").strip()
            out["ai_system_prompt"] = (r.get("system_prompt") or "").strip()
            out["ai_auto_approve"] = (r.get("auto_approve") or "false").strip().lower()
            out["ai_assistant_enabled"] = (r.get("assistant_enabled") or "false").strip().lower()
            out["ai_context_size"] = (r.get("context_size") or "0").strip()
            out["ai_agent_max_steps"] = (r.get("agent_max_steps") or "").strip()
            out["ai_assistant_max_rounds"] = (r.get("assistant_max_rounds") or "").strip()
            out["ai_provider"] = (r.get("provider") or "").strip()
            _vision_raw = r.get("vision_enabled") if "vision_enabled" in r else None
            out["ai_vision_enabled"] = ((_vision_raw or "true").strip().lower() or "true")
            out["ai_output_locale"] = (r.get("ai_output_locale") or "").strip()
    for k in keys:
        if k not in out or out[k] == "":
            if k == "ai_provider":
                out[k] = ""
                continue
            if k == "ai_vision_enabled":
                out[k] = "true"
                continue
            if k == "ai_output_locale":
                out[k] = out.get(k) or ""
                continue
            rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (k,))
            val = (rows[0]["value"] if rows else "") or (
                "true" if k == "ai_auto_approve" else ("0" if k == "ai_context_size" else "")
            )
            if k == "ai_api_key":
                val = ""
            out[k] = val
    return out


async def sync_legacy_user_ai_config_from_profile(db, user_id: int, profile_row: dict) -> None:
    """将 Profile 同步到 user_ai_config 遗留列（兼容旧读取路径）。"""
    s = profile_row_to_settings(profile_row)
    await _ensure_user_config_row(db, user_id)
    await db.execute(
        """INSERT INTO user_ai_config (
               user_id, api_key, base_url, model, system_prompt, auto_approve, assistant_enabled,
               context_size, agent_max_steps, assistant_max_rounds, provider, vision_enabled,
               ai_output_locale, active_profile_id, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE SET
           api_key=excluded.api_key, base_url=excluded.base_url, model=excluded.model,
           system_prompt=excluded.system_prompt, auto_approve=excluded.auto_approve,
           assistant_enabled=excluded.assistant_enabled, context_size=excluded.context_size,
           agent_max_steps=excluded.agent_max_steps, assistant_max_rounds=excluded.assistant_max_rounds,
           provider=excluded.provider, vision_enabled=excluded.vision_enabled,
           ai_output_locale=excluded.ai_output_locale, active_profile_id=excluded.active_profile_id,
           updated_at=CURRENT_TIMESTAMP""",
        (
            user_id,
            s.get("ai_api_key") or "",
            s.get("ai_base_url") or "",
            s.get("ai_model") or "",
            s.get("ai_system_prompt") or "",
            s.get("ai_auto_approve") or "false",
            s.get("ai_assistant_enabled") or "false",
            s.get("ai_context_size") or "0",
            s.get("ai_agent_max_steps") or "",
            s.get("ai_assistant_max_rounds") or "",
            s.get("ai_provider") or "",
            s.get("ai_vision_enabled") or "true",
            s.get("ai_output_locale") or "",
            int(profile_row["id"]),
        ),
    )


def profile_row_to_tool_config(row: dict) -> dict[str, Any]:
    """Profile 行 → AI 工具返回的配置字典（api_key 脱敏）。"""
    s = profile_row_to_settings(row)
    ak = (s.get("ai_api_key") or "").strip()
    return {
        "id": int(row["id"]),
        "name": (row.get("name") or "").strip() or DEFAULT_PROFILE_NAME,
        "api_key": "***" if ak else "",
        "api_key_set": bool(ak),
        "base_url": s.get("ai_base_url") or "",
        "model": s.get("ai_model") or "",
        "system_prompt": s.get("ai_system_prompt") or "",
        "auto_approve": (s.get("ai_auto_approve") or "false").lower() == "true",
        "assistant_enabled": (s.get("ai_assistant_enabled") or "false").lower() == "true",
        "context_size": int(s.get("ai_context_size") or "0"),
        "provider": s.get("ai_provider") or "",
        "agent_max_steps": int(s.get("ai_agent_max_steps") or "0") if (s.get("ai_agent_max_steps") or "").strip() else 0,
        "assistant_max_rounds": int(s.get("ai_assistant_max_rounds") or "0") if (s.get("ai_assistant_max_rounds") or "").strip() else 0,
        "vision_enabled": (s.get("ai_vision_enabled") or "true").lower() != "false",
        "output_locale": s.get("ai_output_locale") or "",
        "updated_at": row.get("updated_at"),
    }


async def get_profile_row(db, user_id: int, profile_id: int) -> dict | None:
    await ensure_profiles_schema(db)
    rows = await db.execute_fetchall(
        "SELECT * FROM user_ai_model_profiles WHERE id = ? AND user_id = ?",
        (profile_id, user_id),
    )
    return dict(rows[0]) if rows else None


async def _ensure_user_config_row(db, user_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO user_ai_config (user_id) VALUES (?)",
        (user_id,),
    )


async def create_profile(db, user_id: int, name: str, fields: dict) -> dict:
    await ensure_profiles_schema(db)
    name = (name or "").strip() or DEFAULT_PROFILE_NAME
    cnt_rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM user_ai_model_profiles WHERE user_id = ?", (user_id,)
    )
    if cnt_rows and int(cnt_rows[0]["c"] or 0) >= MAX_PROFILES_PER_USER:
        raise ValueError(f"最多 {MAX_PROFILES_PER_USER} 组模型配置")
    dup = await db.execute_fetchall(
        "SELECT id FROM user_ai_model_profiles WHERE user_id = ? AND name = ?",
        (user_id, name),
    )
    if dup:
        raise ValueError(f"已存在名为「{name}」的配置")
    cur = await db.execute(
        """
        INSERT INTO user_ai_model_profiles (
            user_id, name, api_key, base_url, model, system_prompt,
            auto_approve, assistant_enabled, context_size,
            agent_max_steps, assistant_max_rounds, provider,
            vision_enabled, ai_output_locale, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        _profile_insert_tuple(user_id, name, fields),
    )
    pid = int(cur.lastrowid)
    await _ensure_user_config_row(db, user_id)
    cnt = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM user_ai_model_profiles WHERE user_id = ?", (user_id,)
    )
    if cnt and int(cnt[0]["c"] or 0) == 1:
        await db.execute(
            "UPDATE user_ai_config SET active_profile_id = ? WHERE user_id = ?",
            (pid, user_id),
        )
    await db.commit()
    row = await get_profile_row(db, user_id, pid)
    assert row is not None
    return row


def _profile_insert_tuple(user_id: int, name: str, fields: dict) -> tuple:
    provider = (fields.get("provider") or "").strip()
    if provider not in ("aliyun", "ollama", "openai"):
        provider = ""
    out_loc = (fields.get("output_locale") or fields.get("ai_output_locale") or "").strip()
    if out_loc not in ("", "en", "zh-CN"):
        out_loc = ""
    return (
        user_id,
        name,
        (fields.get("api_key") or "").strip(),
        (fields.get("base_url") or "").strip().rstrip("/"),
        (fields.get("model") or "").strip(),
        (fields.get("system_prompt") or "").strip(),
        "true" if fields.get("auto_approve") else "false",
        "true" if fields.get("assistant_enabled") else "false",
        str(max(0, int(fields.get("context_size") or 0))),
        str(fields.get("agent_max_steps") or "").strip() if int(fields.get("agent_max_steps") or 0) > 0 else "",
        str(fields.get("assistant_max_rounds") or "").strip() if int(fields.get("assistant_max_rounds") or 0) > 0 else "",
        provider,
        "true" if fields.get("vision_enabled", True) else "false",
        out_loc,
    )


async def update_profile(db, user_id: int, profile_id: int, fields: dict, *, name: str | None = None) -> dict:
    await ensure_profiles_schema(db)
    row = await get_profile_row(db, user_id, profile_id)
    if not row:
        raise ValueError("配置不存在")
    api_key = (fields.get("api_key") if "api_key" in fields else row.get("api_key") or "").strip()
    if api_key in ("", "***"):
        api_key = (row.get("api_key") or "").strip()
    new_name = (name if name is not None else row.get("name") or "").strip() or DEFAULT_PROFILE_NAME
    if new_name != (row.get("name") or "").strip():
        dup = await db.execute_fetchall(
            "SELECT id FROM user_ai_model_profiles WHERE user_id = ? AND name = ? AND id != ?",
            (user_id, new_name, profile_id),
        )
        if dup:
            raise ValueError(f"已存在名为「{new_name}」的配置")
    merged = dict(row)
    merged.update(fields)
    merged["api_key"] = api_key
    merged["name"] = new_name
    await db.execute(
        """
        UPDATE user_ai_model_profiles SET
            name = ?, api_key = ?, base_url = ?, model = ?, system_prompt = ?,
            auto_approve = ?, assistant_enabled = ?, context_size = ?,
            agent_max_steps = ?, assistant_max_rounds = ?, provider = ?,
            vision_enabled = ?, ai_output_locale = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND user_id = ?
        """,
        (
            new_name,
            api_key,
            (merged.get("base_url") or "").strip().rstrip("/"),
            (merged.get("model") or "").strip(),
            (merged.get("system_prompt") or "").strip(),
            "true" if merged.get("auto_approve") in (True, "true", "1") else "false",
            "true" if merged.get("assistant_enabled") in (True, "true", "1") else "false",
            str(max(0, int(merged.get("context_size") or row.get("context_size") or 0))),
            str(merged.get("agent_max_steps") or "").strip() if str(merged.get("agent_max_steps") or "").strip() else "",
            str(merged.get("assistant_max_rounds") or "").strip() if str(merged.get("assistant_max_rounds") or "").strip() else "",
            (merged.get("provider") or "").strip() if (merged.get("provider") or "").strip() in ("aliyun", "ollama", "openai") else "",
            "true" if merged.get("vision_enabled", True) not in (False, "false", "0") else "false",
            (merged.get("output_locale") or merged.get("ai_output_locale") or "").strip()
            if (merged.get("output_locale") or merged.get("ai_output_locale") or "").strip() in ("", "en", "zh-CN")
            else "",
            profile_id,
            user_id,
        ),
    )
    await db.commit()
    updated = await get_profile_row(db, user_id, profile_id)
    assert updated is not None
    return updated


async def delete_profile(db, user_id: int, profile_id: int) -> None:
    await ensure_profiles_schema(db)
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM user_ai_model_profiles WHERE user_id = ?", (user_id,)
    )
    if rows and int(rows[0]["c"] or 0) <= 1:
        raise ValueError("至少保留一组模型配置")
    row = await get_profile_row(db, user_id, profile_id)
    if not row:
        raise ValueError("配置不存在")
    active_id = await get_active_profile_id(db, user_id)
    await db.execute(
        "DELETE FROM user_ai_model_profiles WHERE id = ? AND user_id = ?",
        (profile_id, user_id),
    )
    if active_id == profile_id:
        fallback = await db.execute_fetchall(
            "SELECT id FROM user_ai_model_profiles WHERE user_id = ? ORDER BY id ASC LIMIT 1",
            (user_id,),
        )
        new_active = int(fallback[0]["id"]) if fallback else None
        await db.execute(
            "UPDATE user_ai_config SET active_profile_id = ? WHERE user_id = ?",
            (new_active, user_id),
        )
    await db.commit()


async def activate_profile(db, user_id: int, profile_id: int) -> None:
    await ensure_profiles_schema(db)
    row = await get_profile_row(db, user_id, profile_id)
    if not row:
        raise ValueError("配置不存在")
    await _ensure_user_config_row(db, user_id)
    await db.execute(
        "UPDATE user_ai_config SET active_profile_id = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
        (profile_id, user_id),
    )
    await db.commit()


async def upsert_active_profile_from_config(db, user_id: int, fields: dict) -> None:
    """POST /ai/config：写入当前激活 Profile（无则创建默认）。"""
    await ensure_profiles_schema(db)
    active_id = await get_active_profile_id(db, user_id)
    if active_id is None:
        row = await create_profile(db, user_id, DEFAULT_PROFILE_NAME, fields)
        await activate_profile(db, user_id, int(row["id"]))
        return
    await update_profile(db, user_id, active_id, fields)


def profile_row_to_export_item(row: dict) -> dict[str, Any]:
    """导出 JSON 单条（含 api_key，不含内部 id）。"""
    s = profile_row_to_settings(row)
    return {
        "name": (row.get("name") or "").strip() or DEFAULT_PROFILE_NAME,
        "api_key": s.get("ai_api_key") or "",
        "base_url": s.get("ai_base_url") or "",
        "model": s.get("ai_model") or "",
        "system_prompt": s.get("ai_system_prompt") or "",
        "auto_approve": (s.get("ai_auto_approve") or "false").lower() == "true",
        "assistant_enabled": (s.get("ai_assistant_enabled") or "false").lower() == "true",
        "context_size": int(s.get("ai_context_size") or "0"),
        "provider": s.get("ai_provider") or "",
        "agent_max_steps": int(s.get("ai_agent_max_steps") or "0") if (s.get("ai_agent_max_steps") or "").strip() else 0,
        "assistant_max_rounds": int(s.get("ai_assistant_max_rounds") or "0") if (s.get("ai_assistant_max_rounds") or "").strip() else 0,
        "vision_enabled": (s.get("ai_vision_enabled") or "true").lower() != "false",
        "output_locale": s.get("ai_output_locale") or "",
    }


async def export_profiles_payload(
    db,
    user_id: int,
    *,
    profile_id: int | None = None,
) -> dict[str, Any]:
    """导出全部或单条 Profile 为 JSON 结构。"""
    await ensure_profiles_schema(db)
    active_id = await get_active_profile_id(db, user_id)
    active_name: str | None = None
    if profile_id is not None:
        row = await get_profile_row(db, user_id, profile_id)
        if not row:
            raise ValueError("配置不存在")
        profiles = [profile_row_to_export_item(row)]
        if active_id is not None and int(active_id) == int(profile_id):
            active_name = profiles[0]["name"]
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM user_ai_model_profiles WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        )
        profiles = [profile_row_to_export_item(dict(r)) for r in rows]
        if active_id is not None:
            for r in rows:
                if int(r["id"]) == int(active_id):
                    active_name = (r["name"] or "").strip() or DEFAULT_PROFILE_NAME
                    break
    return {
        "_edgeops": {
            "version": 1,
            "type": "ai_model_profiles",
            "active_profile_name": active_name,
        },
        "profiles": profiles,
    }


def _normalize_import_fields(item: dict) -> dict[str, Any]:
    provider = (item.get("provider") or "").strip()
    if provider not in ("aliyun", "ollama", "openai"):
        provider = ""
    out_loc = (item.get("output_locale") or item.get("ai_output_locale") or "").strip()
    if out_loc not in ("", "en", "zh-CN"):
        out_loc = ""
    ctx = max(0, int(item.get("context_size") or 0))
    steps = max(0, int(item.get("agent_max_steps") or 0))
    rounds = max(0, int(item.get("assistant_max_rounds") or 0))
    return {
        "api_key": (item.get("api_key") or "").strip(),
        "base_url": (item.get("base_url") or "").strip().rstrip("/"),
        "model": (item.get("model") or "").strip(),
        "system_prompt": (item.get("system_prompt") or "").strip(),
        "auto_approve": item.get("auto_approve") in (True, "true", "1", 1),
        "assistant_enabled": item.get("assistant_enabled") in (True, "true", "1", 1),
        "context_size": ctx,
        "provider": provider,
        "agent_max_steps": steps,
        "assistant_max_rounds": rounds,
        "vision_enabled": item.get("vision_enabled", True) not in (False, "false", "0", 0),
        "output_locale": out_loc,
    }


def parse_profiles_import_blob(raw: str | dict | list) -> tuple[list[dict], str | None]:
    """解析导入 JSON，返回 (profiles, active_profile_name)。"""
    import json

    if isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = raw
    active_name: str | None = None
    if isinstance(data, list):
        profiles_raw = data
    elif isinstance(data, dict):
        meta = data.get("_edgeops") if isinstance(data.get("_edgeops"), dict) else {}
        active_name = (meta.get("active_profile_name") or "").strip() or None
        if isinstance(data.get("profiles"), list):
            profiles_raw = data["profiles"]
        elif "name" in data and (
            "model" in data or "base_url" in data or "api_key" in data or "provider" in data
        ):
            profiles_raw = [data]
        else:
            raise ValueError("未找到 profiles 数组")
    else:
        raise ValueError("JSON 根须为对象或数组")
    if not profiles_raw:
        raise ValueError("profiles 为空")
    out: list[dict] = []
    for i, item in enumerate(profiles_raw):
        if not isinstance(item, dict):
            raise ValueError(f"profiles[{i}] 须为对象")
        name = (item.get("name") or "").strip() or DEFAULT_PROFILE_NAME
        normalized = _normalize_import_fields(item)
        normalized["name"] = name
        out.append(normalized)
    return out, active_name


async def import_profiles(
    db,
    user_id: int,
    raw: str | dict | list,
    *,
    mode: str = "incremental",
) -> dict[str, Any]:
    """导入 Profile。mode=incremental 跳过同名；mode=overwrite 覆盖同名。"""
    if mode not in ("incremental", "overwrite"):
        raise ValueError("mode 须为 incremental 或 overwrite")
    profiles, active_name = parse_profiles_import_blob(raw)
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []
    for item in profiles:
        name = item.pop("name", DEFAULT_PROFILE_NAME) or DEFAULT_PROFILE_NAME
        fields = dict(item)
        try:
            existing = await db.execute_fetchall(
                "SELECT id FROM user_ai_model_profiles WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            if existing:
                if mode == "incremental":
                    skipped.append(name)
                    continue
                pid = int(existing[0]["id"])
                await update_profile(db, user_id, pid, fields, name=name)
                updated.append(name)
            else:
                await create_profile(db, user_id, name, fields)
                created.append(name)
        except ValueError as e:
            errors.append({"name": name, "error": str(e)})
    activated: str | None = None
    if active_name:
        row = await db.execute_fetchall(
            "SELECT id FROM user_ai_model_profiles WHERE user_id = ? AND name = ?",
            (user_id, active_name),
        )
        if row:
            try:
                await activate_profile(db, user_id, int(row[0]["id"]))
                activated = active_name
            except ValueError:
                pass
    items, active_id = await list_profiles(db, user_id)
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "activated": activated,
        "profiles": items,
        "active_profile_id": active_id,
    }
