"""移除离线版本申请：删表并清理相关 settings 键。

从 schema 30 升级到 31。历史库若经 016 建过表，本脚本会 DROP；
新装库不应再包含这些表（见 SCHEMA_SQL / fresh_install.sql）。
"""


async def upgrade(db):
    await db.execute("DROP TABLE IF EXISTS offline_request_replies")
    await db.execute("DROP TABLE IF EXISTS offline_request_applications")
    await db.execute(
        "DELETE FROM settings WHERE key IN (?, ?)",
        (
            "notify_admin_on_offline_request",
            "login_widget_offline_request_enabled",
        ),
    )
    await db.commit()
