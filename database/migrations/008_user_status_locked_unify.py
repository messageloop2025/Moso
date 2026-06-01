"""
数据库升级脚本：从版本 8 升级到版本 9。

将「安全锁定」从仅依赖 locked_until + status=active 改为三态 status：
- active：正常
- locked：因多次密码错误等被系统自动锁定（locked_until 为锁定期截止时间）
- suspended：管理员暂停

迁移后 locked_until 仅在 status=locked 时有意义；暂停账户清空 locked_until。
"""

from datetime import datetime, timezone


async def upgrade(db):
    rows = await db.execute_fetchall("SELECT id, status, locked_until FROM users")
    now = datetime.now(timezone.utc)
    for r in rows:
        uid = r["id"]
        st = (r["status"] or "").strip().lower()
        lu = r["locked_until"]
        if st == "disabled":
            await db.execute(
                "UPDATE users SET status = 'suspended', locked_until = NULL WHERE id = ?",
                (uid,),
            )
            continue
        if st == "suspended":
            await db.execute(
                "UPDATE users SET locked_until = NULL WHERE id = ? AND locked_until IS NOT NULL",
                (uid,),
            )
            continue
        if lu:
            try:
                s = lu if isinstance(lu, str) else str(lu)
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                await db.execute(
                    "UPDATE users SET locked_until = NULL, failed_login_attempts = 0, status = 'active' WHERE id = ?",
                    (uid,),
                )
                continue
            if dt > now:
                await db.execute("UPDATE users SET status = 'locked' WHERE id = ?", (uid,))
            else:
                await db.execute(
                    "UPDATE users SET locked_until = NULL, failed_login_attempts = 0, status = 'active' WHERE id = ?",
                    (uid,),
                )
        else:
            if st == "locked":
                await db.execute(
                    "UPDATE users SET status = 'active', failed_login_attempts = 0 WHERE id = ?",
                    (uid,),
                )
    await db.commit()
