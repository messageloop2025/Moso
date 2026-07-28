"""为 settings 表注入 system_ai_usage_limit（未配置自有 Key 用户的系统共享 Key 试用次数上限）。"""


async def upgrade(db):
    import config as cfg

    default = str(int(getattr(cfg, "SYSTEM_AI_USAGE_LIMIT", 2000)))
    await db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("system_ai_usage_limit", default),
    )
    await db.commit()
