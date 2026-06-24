"""从 schema 33 升级到 34：服务凭证增加 port、host_id 改为可选（不再绑定操作主机）。

脚本编号 033。
"""
from __future__ import annotations


async def upgrade(db) -> None:
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)

    async def host_id_nullable() -> bool:
        cursor = await db.execute("PRAGMA table_info(host_service_credentials)")
        rows = await cursor.fetchall()
        for r in rows:
            if r[1] == "host_id":
                return r[3] == 0
        return False

    if await has_column("host_service_credentials", "port") and await host_id_nullable():
        return

    await db.execute(
        """
        CREATE TABLE host_service_credentials_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            host_id INTEGER,
            service TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            port INTEGER,
            service_username TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            password_enc TEXT NOT NULL DEFAULT '',
            linked_credential_id INTEGER,
            linked_host_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE SET NULL,
            FOREIGN KEY (linked_credential_id) REFERENCES credentials(id) ON DELETE SET NULL,
            FOREIGN KEY (linked_host_id) REFERENCES hosts(id) ON DELETE SET NULL
        )
        """
    )
    if await has_column("host_service_credentials", "port"):
        await db.execute(
            """
            INSERT INTO host_service_credentials_new
                (id, user_id, host_id, service, address, port, service_username, label, notes,
                 password_enc, linked_credential_id, linked_host_id, created_at, updated_at)
            SELECT id, user_id, host_id, service, address, port, service_username, label, notes,
                   password_enc, linked_credential_id, linked_host_id, created_at, updated_at
            FROM host_service_credentials
            """
        )
    else:
        await db.execute(
            """
            INSERT INTO host_service_credentials_new
                (id, user_id, host_id, service, address, port, service_username, label, notes,
                 password_enc, linked_credential_id, linked_host_id, created_at, updated_at)
            SELECT id, user_id, host_id, service, address, NULL, service_username, label, notes,
                   password_enc, linked_credential_id, linked_host_id, created_at, updated_at
            FROM host_service_credentials
            """
        )
    await db.execute("DROP TABLE host_service_credentials")
    await db.execute("ALTER TABLE host_service_credentials_new RENAME TO host_service_credentials")
    await db.execute("DROP INDEX IF EXISTS idx_hsc_user_host")
    await db.execute("DROP INDEX IF EXISTS idx_hsc_user_service")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hsc_user ON host_service_credentials(user_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hsc_user_lookup "
        "ON host_service_credentials(user_id, service, address, port, service_username)"
    )
    await db.commit()
