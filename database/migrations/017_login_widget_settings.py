"""为登录页公开浮窗 / 公开留言展示区注入默认开关到 settings 表。

设计：
- 三个开关均默认开启（'true'），与升级前行为一致；管理员可在
  「反馈管理 → 登录页留言板」顶部的"显示设置"卡里关闭。

约定的 settings.key：
- login_widget_message_board_enabled    右下角"留言板"浮窗按钮（写留言入口）是否展示
- login_widget_public_messages_enabled  登录页右侧公开留言展示区是否展示
"""


async def upgrade(db):
    defaults = [
        ("login_widget_message_board_enabled", "true"),
        ("login_widget_public_messages_enabled", "true"),
    ]
    for key, value in defaults:
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    await db.commit()
