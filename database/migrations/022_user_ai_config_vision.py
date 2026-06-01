"""给 user_ai_config 增加 `vision_enabled` 列：指示该用户所配模型是否支持图像识别。

背景：并非所有 OpenAI 兼容网关 / 本地部署模型都支持多模态视觉输入（list content 里
的 `image_url` 段），硬塞图片会被部分网关直接 400 或默默丢弃。因此单独加一个开关：

- `vision_enabled = 'true'`（默认）：后端会把用户本轮新上传的图片按 OpenAI 多模态
  格式内联到 user 消息的 `content` 数组里，让视觉模型真正看到图。
- `vision_enabled = 'false'`：只以文本形式挂一份 📎 附件清单，让 AI 按需用
  `read_chat_attachment(uuid=...)` 拿 `data_url` 兜底（避免给非视觉模型硬塞 image_url
  而触发网关错误）。

幂等：重复执行只会在列缺失时补列。
"""

from __future__ import annotations


async def upgrade(db):
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        await cursor.close()
        return any(r[1] == column for r in rows)

    if not await has_column("user_ai_config", "vision_enabled"):
        await db.execute(
            "ALTER TABLE user_ai_config ADD COLUMN vision_enabled TEXT DEFAULT 'true'"
        )
    # 老行缺省补齐为 'true'，避免 NULL 在后续逻辑里被当作 false。
    await db.execute(
        "UPDATE user_ai_config SET vision_enabled = 'true' "
        "WHERE vision_enabled IS NULL OR TRIM(COALESCE(vision_enabled, '')) = ''"
    )
    await db.commit()
