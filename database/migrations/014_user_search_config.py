"""新增「用户搜索服务配置」表：每用户每 provider 一行，统一保存 GitHub / Aliyun IQS 等
搜索服务的 API Key 与启用状态。后续接入 Bing / SerpAPI 等只新增 provider 标识，不再加表。"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_search_config (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            extra TEXT DEFAULT '{}',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, provider)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_search_config_provider ON user_search_config(provider)"
    )
    await db.commit()
