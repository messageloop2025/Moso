"""主机服务凭证表 + 凭证库功能开关 settings.credentials_vault_enabled。

从 schema 32 升级到 33（脚本编号 032）。
"""
from __future__ import annotations


async def upgrade(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS host_service_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            host_id INTEGER NOT NULL,
            service TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            service_username TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            password_enc TEXT NOT NULL DEFAULT '',
            linked_credential_id INTEGER,
            linked_host_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE,
            FOREIGN KEY (linked_credential_id) REFERENCES credentials(id) ON DELETE SET NULL,
            FOREIGN KEY (linked_host_id) REFERENCES hosts(id) ON DELETE SET NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hsc_user_host ON host_service_credentials(user_id, host_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hsc_user_service ON host_service_credentials(user_id, service, address)"
    )
    await db.execute(
        """
        INSERT OR IGNORE INTO settings (key, value) VALUES ('credentials_vault_enabled', 'false')
        """
    )
    await db.commit()
