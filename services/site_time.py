"""站点显示时区：管理员在 settings.site_timezone 配置 IANA 名称，默认 Asia/Shanghai。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

SETTINGS_KEY_SITE_TZ = "site_timezone"
DEFAULT_SITE_TIMEZONE = "Asia/Shanghai"

# tzdata 未安装时 ZoneInfo 不可用；常用 IANA 名称可用固定 UTC 偏移勉强显示（精细 DST 仍须安装 tzdata）。
_KNOWN_OFFSETS = {
    "asia/shanghai": timedelta(hours=8),
    "utc": timedelta(0),
}


def _fallback_tzinfo(name: str):
    """tzdata 缺失时为少数常用区名返回 timezone(timedelta)，否则 None。"""
    key = (name or "").strip().lower()
    if key in _KNOWN_OFFSETS:
        return timezone(_KNOWN_OFFSETS[key])
    return None


def validate_iana_timezone(name: str) -> tuple[bool, str]:
    """校验时区标识；成功返回 (True, 规范化后的名称)，失败 (False, 错误说明)。"""
    s = (name or "").strip()
    if not s:
        return False, "时区不能为空"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(s)
    except Exception:  # noqa: BLE001 — 无效名或缺少 tzdata
        if _fallback_tzinfo(s) is not None:
            return True, s
        return False, (
            "无效的 IANA 时区名称，或当前 Python 环境未安装 tzdata。"
            "请在运行 毛竹 的虚拟环境中执行：pip install tzdata（或 pip install -r requirements.txt）。"
            "示例时区：Asia/Shanghai、UTC。"
        )
    return True, s


def effective_timezone_name(raw: str | None) -> str:
    """返回可用于 ZoneInfo 的名称；无效或空则用默认东八区。"""
    ok, s = validate_iana_timezone(raw or "")
    if ok:
        return s
    return DEFAULT_SITE_TIMEZONE


async def get_site_timezone_raw(db) -> str:
    """从 settings 读取原始配置值（可能无效或空）。"""
    rows = await db.execute_fetchall(
        "SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY_SITE_TZ,)
    )
    if not rows:
        return ""
    return (dict(rows[0]).get("value") or "").strip()


async def get_effective_site_timezone(db) -> str:
    raw = await get_site_timezone_raw(db)
    return effective_timezone_name(raw)


def _resolve_tzinfo(tz_name: str):
    """返回 tzinfo；优先 ZoneInfo，失败则用内置偏移回退。"""
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(tz_name), False
    except Exception:  # noqa: BLE001
        fb = _fallback_tzinfo(tz_name)
        if fb is not None:
            return fb, True
        try:
            return ZoneInfo(DEFAULT_SITE_TIMEZONE), False
        except Exception:  # noqa: BLE001
            fb2 = _fallback_tzinfo(DEFAULT_SITE_TIMEZONE)
            if fb2 is not None:
                return fb2, True
            return timezone.utc, True


def build_server_time_payload(tz_name: str) -> dict:
    """当前 UTC 与站点本地时刻，供 /auth/me 与 get_server_time 工具使用。"""
    ok, tz_norm = validate_iana_timezone(tz_name)
    use_tz = tz_norm if ok else DEFAULT_SITE_TIMEZONE
    tz, used_fallback = _resolve_tzinfo(use_tz)
    utc_now = datetime.now(timezone.utc)
    local_now = utc_now.astimezone(tz)
    out = {
        "site_timezone": use_tz,
        "server_time_utc": utc_now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "server_time_local": local_now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if used_fallback:
        out["timezone_note"] = (
            "当前使用内置 UTC 偏移显示时间（未加载完整 IANA 数据库）。"
            "请在服务所用虚拟环境中安装 tzdata：pip install tzdata"
        )
    return out
