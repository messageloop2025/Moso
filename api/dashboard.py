"""仪表盘统计 API：请求量、活跃用户、AI 回合、主机注册、TOP 榜单等"""
from fastapi import APIRouter, Depends

from database import get_db
from api.auth import get_current_user, _is_admin_role

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"])


@router.get("/stats")
async def dashboard_stats(user=Depends(get_current_user)):
    """返回四项统计序列，供前端绘制近 7 日/近 30 日曲线。"""
    db = await get_db()
    admin = _is_admin_role(user.get("role"))

    # 1) 每小时请求数，近 7 日（operation_logs 按小时聚合）
    rows_req = await db.execute_fetchall(
        """SELECT strftime('%Y-%m-%d %H:00', created_at) AS hour, COUNT(*) AS cnt
           FROM operation_logs
           WHERE created_at >= datetime('now', '-7 days')
           """ + ("" if admin else " AND user_id = ?") + """
           GROUP BY hour ORDER BY hour""",
        () if admin else (user["id"],),
    )
    requests_per_hour_7d = [{"hour": r["hour"], "count": r["cnt"]} for r in rows_req]

    # 2) 每日活跃用户数，近 30 日（按日去重 user_id）
    if admin:
        rows_act = await db.execute_fetchall(
            """SELECT date(l.created_at) AS day, COUNT(DISTINCT l.user_id) AS cnt
               FROM operation_logs l
               JOIN users u ON u.id = l.user_id
               WHERE l.created_at >= date('now', '-30 days')
                 AND LOWER(COALESCE(u.role, '')) NOT IN ('admin', 'manager')
                 AND COALESCE(u.role, '') != '管理员'
               GROUP BY day ORDER BY day"""
        )
    else:
        rows_act = await db.execute_fetchall(
            """SELECT date(created_at) AS day, COUNT(DISTINCT user_id) AS cnt
               FROM operation_logs
               WHERE created_at >= date('now', '-30 days') AND user_id = ?
               GROUP BY day ORDER BY day""",
            (user["id"],),
        )
    daily_active_users_30d = [{"date": r["day"], "count": r["cnt"]} for r in rows_act]

    # 3) AI 请求回合数，每小时，近 7 日（source 含 ai 的日志按小时聚合）
    rows_ai = await db.execute_fetchall(
        """SELECT strftime('%Y-%m-%d %H:00', created_at) AS hour, COUNT(*) AS cnt
           FROM operation_logs
           WHERE created_at >= datetime('now', '-7 days')
           AND (source LIKE 'ai%' OR source = 'ai_agent' OR operation LIKE 'ai_%')
           """ + ("" if admin else " AND user_id = ?") + """
           GROUP BY hour ORDER BY hour""",
        () if admin else (user["id"],),
    )
    ai_rounds_per_hour_7d = [{"hour": r["hour"], "count": r["cnt"]} for r in rows_ai]

    # 4) 注册服务器（新增主机）每小时，近 7 日
    if admin:
        rows_host = await db.execute_fetchall(
            """SELECT strftime('%Y-%m-%d %H:00', created_at) AS hour, COUNT(*) AS cnt
               FROM hosts
               WHERE created_at >= datetime('now', '-7 days')
               GROUP BY hour ORDER BY hour"""
        )
    else:
        rows_host = await db.execute_fetchall(
            """SELECT strftime('%Y-%m-%d %H:00', created_at) AS hour, COUNT(*) AS cnt
               FROM hosts
               WHERE created_at >= datetime('now', '-7 days') AND created_by = ?
               GROUP BY hour ORDER BY hour""",
            (user["id"],),
        )
    hosts_per_hour_7d = [{"hour": r["hour"], "count": r["cnt"]} for r in rows_host]

    # 5) 近 7 日 AI 调用计数 TOP20（管理员看全量；普通用户仅自己）
    rows_ai_top = await db.execute_fetchall(
        """SELECT u.id AS user_id, u.username, COALESCE(u.display_name, '') AS display_name, COUNT(*) AS cnt
           FROM operation_logs l
           JOIN users u ON u.id = l.user_id
           WHERE l.created_at >= datetime('now', '-7 days')
             AND (l.source LIKE 'ai%' OR l.source = 'ai_agent' OR l.operation LIKE 'ai_%')
             AND LOWER(COALESCE(u.role, '')) NOT IN ('admin', 'manager')
             AND COALESCE(u.role, '') != '管理员'
             """
        + ("" if admin else " AND l.user_id = ?")
        + """
           GROUP BY u.id, u.username, u.display_name
           ORDER BY cnt DESC, u.id ASC
           LIMIT 20""",
        () if admin else (user["id"],),
    )
    ai_calls_top20_7d = [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "display_name": r["display_name"] or "",
            "count": r["cnt"],
        }
        for r in rows_ai_top
    ]

    # 6) 近 7 日用户登录计数 TOP20（管理员视图：排除管理员角色）
    if admin:
        rows_login_top = await db.execute_fetchall(
            """SELECT u.id AS user_id, u.username, COALESCE(u.display_name, '') AS display_name, COUNT(*) AS cnt
               FROM user_login_events e
               JOIN users u ON u.id = e.user_id
               WHERE e.created_at >= datetime('now', '-7 days')
                 AND LOWER(COALESCE(u.role, '')) NOT IN ('admin', 'manager')
                 AND COALESCE(u.role, '') != '管理员'
               GROUP BY u.id, u.username, u.display_name
               ORDER BY cnt DESC, u.id ASC
               LIMIT 20"""
        )
        login_top20_7d = [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "display_name": r["display_name"] or "",
                "count": r["cnt"],
            }
            for r in rows_login_top
        ]
    else:
        login_top20_7d = []

    return {
        "success": True,
        "requests_per_hour_7d": requests_per_hour_7d,
        "daily_active_users_30d": daily_active_users_30d,
        "ai_rounds_per_hour_7d": ai_rounds_per_hour_7d,
        "hosts_per_hour_7d": hosts_per_hour_7d,
        "ai_calls_top20_7d": ai_calls_top20_7d,
        "login_top20_7d": login_top20_7d,
    }
