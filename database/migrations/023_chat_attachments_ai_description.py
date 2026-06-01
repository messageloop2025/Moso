"""给 chat_attachments 增加 AI 识别结果缓存字段：

- `ai_description`            文本描述（OCR + 主要视觉元素 + 结构化信息，AI 第一次看图时生成）
- `ai_description_model`      生成描述所用的模型名，便于前端/日志观察
- `ai_description_updated_at` 最近一次刷新时间（便于做缓存失效判断）

动机：多轮对话里，每次把图片的 base64 送给 provider 会把 token 预算反复打爆
（典型错误：`Range of input length should be [1, 29804]`）。改为由 AI 首次看图时
把"提取出的内容"存回附件行作为文本扩展信息，后续轮次默认只使用这段文本描述回答，
只有用户明确要求"重新分析图/再看一遍"时才强制回读原图像素。
"""
from __future__ import annotations


async def upgrade(db):
    async def _has_column(table: str, col: str) -> bool:
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        await cur.close()
        return any(r[1] == col for r in rows)

    if not await _has_column("chat_attachments", "ai_description"):
        await db.execute(
            "ALTER TABLE chat_attachments ADD COLUMN ai_description TEXT NOT NULL DEFAULT ''"
        )
    if not await _has_column("chat_attachments", "ai_description_model"):
        await db.execute(
            "ALTER TABLE chat_attachments ADD COLUMN ai_description_model TEXT NOT NULL DEFAULT ''"
        )
    if not await _has_column("chat_attachments", "ai_description_updated_at"):
        await db.execute(
            "ALTER TABLE chat_attachments ADD COLUMN ai_description_updated_at DATETIME"
        )
    await db.commit()
