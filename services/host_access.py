"""主机访问权限校验（供 ssh_channel、ssh_execute 等共用，避免 ai_skills 循环导入）。"""


def is_admin(user: dict) -> bool:
    return (user.get("role") or "").strip().lower() == "admin"


def can_access_host(host_row: dict, user: dict) -> bool:
    return is_admin(user) or (host_row.get("created_by") == user["id"])


async def can_access_host_with_shares(db, host_row: dict, user: dict) -> bool:
    if can_access_host(host_row, user):
        return True
    hid = host_row.get("id")
    if hid is None:
        return False
    rows = await db.execute_fetchall(
        """SELECT 1 FROM host_shares
           WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL
           LIMIT 1""",
        (hid, user["id"]),
    )
    return bool(rows)
