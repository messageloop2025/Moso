"""系统共享 API Key 每用户试用次数（settings + config 默认值）。"""
from __future__ import annotations

SETTINGS_KEY_SYSTEM_AI_USAGE_LIMIT = "system_ai_usage_limit"
MAX_SYSTEM_AI_USAGE_LIMIT = 1_000_000


def default_system_ai_usage_limit() -> int:
    import config as cfg

    return max(0, int(getattr(cfg, "SYSTEM_AI_USAGE_LIMIT", 2000)))


def parse_system_ai_usage_limit_value(raw: object) -> tuple[bool, str | int]:
    """校验 settings 值。成功返回 (True, int)；失败返回 (False, 错误信息)。"""
    s = str(raw or "").strip()
    if not s:
        return False, "系统共享 Key 试用次数不能为空"
    try:
        n = int(s)
    except ValueError:
        return False, "系统共享 Key 试用次数须为非负整数"
    if n < 0 or n > MAX_SYSTEM_AI_USAGE_LIMIT:
        return False, f"系统共享 Key 试用次数须在 0～{MAX_SYSTEM_AI_USAGE_LIMIT} 之间"
    return True, n


async def get_system_ai_usage_limit(db) -> int:
    """读取当前生效的每用户系统共享 Key 调用上限（管理员 settings 优先于 config 默认）。"""
    rows = await db.execute_fetchall(
        "SELECT value FROM settings WHERE key = ? LIMIT 1",
        (SETTINGS_KEY_SYSTEM_AI_USAGE_LIMIT,),
    )
    if rows:
        ok, parsed = parse_system_ai_usage_limit_value(rows[0]["value"])
        if ok:
            return int(parsed)
    return default_system_ai_usage_limit()
