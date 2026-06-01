"""用户搜索服务配置读写帮手。

存储模型：每用户每 provider 一行，user_search_config(user_id, provider, api_key, enabled, extra, updated_at)。
约定：
- api_key 与 user_mail_config.smtp_password 一样以明文 TEXT 保存（与现有项目风格一致）。
- HTTP 接口与 AI 工具均通过 public_config_for_api() 输出，不回显 api_key 原值，仅返回 api_key_set: bool。
- 写入帮手 upsert_user_search_config 支持 patch 语义：传 None / 空串 / "***" 表示「保持原值」。
"""
from __future__ import annotations

import json
import logging

import aiosqlite

from services.search_providers import get_provider, list_providers, provider_meta

logger = logging.getLogger("edgeops.search.config")


def _normalize_extra(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def public_config_for_api(row: aiosqlite.Row | dict | None, provider_name: str) -> dict:
    """把一条 user_search_config 行序列化成「不暴露 key」的对外结构。"""
    api_key = ""
    enabled = True
    extra: dict = {}
    updated_at = None
    if row is not None:
        d = dict(row)
        api_key = (d.get("api_key") or "").strip()
        enabled = bool(d.get("enabled", 1))
        extra = _normalize_extra(d.get("extra"))
        updated_at = d.get("updated_at")
    provider = get_provider(provider_name)
    return {
        "provider": provider_name,
        "display_name": provider.display_name if provider else provider_name,
        "requires_key": bool(provider.requires_key) if provider else True,
        "api_key_set": bool(api_key),
        "enabled": enabled,
        "extra": extra,
        "updated_at": updated_at,
        "available": (not provider.requires_key) if provider else False or bool(api_key),
    }


async def get_user_search_config(
    db: aiosqlite.Connection, user_id: int, provider: str
) -> dict | None:
    """取出原始行（含 api_key 明文，仅在内部需要时使用，绝不直接返回前端/AI）。"""
    rows = await db.execute_fetchall(
        "SELECT user_id, provider, api_key, enabled, extra, updated_at "
        "FROM user_search_config WHERE user_id = ? AND provider = ?",
        (int(user_id), provider.strip().lower()),
    )
    if not rows:
        return None
    d = dict(rows[0])
    d["extra"] = _normalize_extra(d.get("extra"))
    return d


async def list_user_search_configs(
    db: aiosqlite.Connection, user_id: int
) -> list[dict]:
    """列出当前用户在所有已注册 provider 下的配置（脱敏）。"""
    rows = await db.execute_fetchall(
        "SELECT user_id, provider, api_key, enabled, extra, updated_at "
        "FROM user_search_config WHERE user_id = ?",
        (int(user_id),),
    )
    by_provider = {(r["provider"] or "").strip().lower(): r for r in rows}
    out: list[dict] = []
    for prov in list_providers():
        out.append(public_config_for_api(by_provider.get(prov.name), prov.name))
    return out


async def upsert_user_search_config(
    db: aiosqlite.Connection,
    user_id: int,
    provider: str,
    *,
    api_key=None,
    enabled=None,
    extra=None,
) -> dict:
    """patch 语义的 upsert。返回脱敏后的最新配置。

    - api_key=None / "" / "***" → 不覆盖原值（与 settings._is_secret_key 模式一致）；传非空字符串才更新。
    - enabled=None → 不变；传 True/False 才更新。
    - extra=None → 不变；传 dict 才整体替换。

    新插入时 api_key 默认为空，enabled 默认 True，extra 默认 {}。
    """
    prov_name = (provider or "").strip().lower()
    if not prov_name:
        raise ValueError("provider 不能为空")
    if not get_provider(prov_name):
        raise ValueError(f"未知的搜索服务 provider：{provider}")

    existing = await get_user_search_config(db, user_id, prov_name)
    new_api_key = existing["api_key"] if existing else ""
    new_enabled = existing["enabled"] if existing else 1
    new_extra: dict = existing["extra"] if existing else {}

    if isinstance(api_key, str):
        s = api_key.strip()
        if s and s != "***":
            new_api_key = s
    if enabled is not None:
        new_enabled = 1 if bool(enabled) else 0
    if isinstance(extra, dict):
        new_extra = extra

    extra_json = json.dumps(new_extra, ensure_ascii=False)
    await db.execute(
        """INSERT INTO user_search_config (user_id, provider, api_key, enabled, extra, updated_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id, provider) DO UPDATE SET
             api_key = excluded.api_key,
             enabled = excluded.enabled,
             extra = excluded.extra,
             updated_at = CURRENT_TIMESTAMP""",
        (int(user_id), prov_name, new_api_key, new_enabled, extra_json),
    )
    await db.commit()

    fresh = await get_user_search_config(db, user_id, prov_name)
    return public_config_for_api(fresh, prov_name)


async def delete_user_search_config(
    db: aiosqlite.Connection, user_id: int, provider: str
) -> None:
    """彻底删除某 provider 的配置（清空 key 等价于此操作）。"""
    await db.execute(
        "DELETE FROM user_search_config WHERE user_id = ? AND provider = ?",
        (int(user_id), provider.strip().lower()),
    )
    await db.commit()


async def call_search(
    db: aiosqlite.Connection,
    user_id: int,
    provider: str,
    query: str,
    options: dict | None = None,
) -> dict:
    """统一调用入口：取用户配置 → 调 provider.search()。"""
    prov = get_provider(provider)
    if not prov:
        return {"success": False, "error": f"未知的搜索服务 provider：{provider}"}
    cfg = await get_user_search_config(db, user_id, prov.name) or {}
    if cfg and not cfg.get("enabled", 1):
        return {
            "success": False,
            "error": f"{prov.display_name} 已被你禁用；请到「设置 / 搜索服务」启用后再用",
        }
    api_key = (cfg.get("api_key") or "").strip()
    extra = cfg.get("extra") or {}
    if prov.requires_key and not api_key:
        return {
            "success": False,
            "error": f"尚未配置 {prov.display_name} 的 API Key；请到「设置 / 搜索服务」配置后再用",
        }
    return await prov.search(query, api_key=api_key, extra=extra, options=options or {})


def all_providers_meta() -> list[dict]:
    """供前端/AI 一次性拉取所有 provider 的元数据（不含任何用户密钥）。"""
    return [provider_meta(p) for p in list_providers()]
