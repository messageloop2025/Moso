"""JWT 一次性 nonce 表：数学验证码、找回临时 token 等短效 JWT 校验通过后即作废。"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS jwt_nonces (
            jti TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jwt_nonces_created ON jwt_nonces(created_at)"
    )
    await db.commit()
