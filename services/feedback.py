"""留言板 / 用户反馈业务层。

三张表：
- anonymous_messages：登录页匿名留言 + 管理员回复（parent_id 区分）。
- user_feedback：登录用户提交的反馈主体。
- user_feedback_replies：管理员对反馈的回复（一条反馈可有多条回复）。

状态机：
- anonymous_messages.status：'pending' → 'approved' / 'hidden'。
- user_feedback.status：
    'open' → （管理员任意回复）→ 'replied'
                          ↘ 'ignored'
    （用户撤回 = 物理删除整行；不存在 'withdrawn' 这一持久化状态）
- show_on_login（仅匿名留言/回复用）：管理员可独立切换；登录页只展示 status='approved' 且 show_on_login=1 的。

约束：
- 用户改/删反馈：仅当 status='open' 且无任何 reply 行存在时允许。
- 管理员撤回回复：物理删除 reply 行；如该反馈再无 reply，则 status 退回 'open'。
"""
from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger("edgeops.feedback")

# ── 匿名留言常量与限制 ──
MAX_ANON_CONTENT_LEN = 4000
MAX_ANON_NICKNAME_LEN = 40
ANON_RATE_LIMIT_WINDOW_SEC = 60  # 同 IP 1 分钟内
ANON_RATE_LIMIT_MAX = 3          # 最多发 3 条
ANON_RATE_LIMIT_DAY_MAX = 20     # 同 IP 24h 内最多 20 条

# ── 反馈常量 ──
MAX_FEEDBACK_TITLE_LEN = 200
MAX_FEEDBACK_CONTENT_LEN = 20000
VALID_FEEDBACK_CATEGORIES = ("bug", "feature", "tech", "general")
VALID_FEEDBACK_FILTERS = ("all", "unread", "open", "replied", "ignored", "mine")


# ─────────────────────────────────────────────
# 公共序列化
# ─────────────────────────────────────────────

def _row_to_anon(row) -> dict:
    d = dict(row)
    return {
        "id": d.get("id"),
        "parent_id": d.get("parent_id"),
        "author_type": d.get("author_type"),
        "author_user_id": d.get("author_user_id"),
        "nickname": d.get("nickname") or "",
        "content": d.get("content") or "",
        "show_on_login": bool(d.get("show_on_login")),
        "status": d.get("status") or "pending",
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }


def _row_to_anon_admin(row) -> dict:
    """管理员视角额外暴露 IP / UA。"""
    base = _row_to_anon(row)
    d = dict(row)
    base["ip_address"] = d.get("ip_address") or ""
    base["user_agent"] = d.get("user_agent") or ""
    return base


def _row_to_feedback(row) -> dict:
    d = dict(row)
    return {
        "id": d.get("id"),
        "user_id": d.get("user_id"),
        "title": d.get("title") or "",
        "content": d.get("content") or "",
        "category": d.get("category") or "general",
        "status": d.get("status") or "open",
        "is_ai_submitted": bool(d.get("is_ai_submitted")),
        "admin_read_at": d.get("admin_read_at"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }


def _row_to_reply(row) -> dict:
    d = dict(row)
    return {
        "id": d.get("id"),
        "feedback_id": d.get("feedback_id"),
        "admin_user_id": d.get("admin_user_id"),
        "content": d.get("content") or "",
        "is_ai_drafted": bool(d.get("is_ai_drafted")),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }


# ─────────────────────────────────────────────
# 匿名留言（登录页）
# ─────────────────────────────────────────────

async def check_anon_rate_limit(db: aiosqlite.Connection, ip: str) -> tuple[bool, str]:
    """简易 IP 级频控：1 分钟内 ≤3 条且 24 小时内 ≤20 条。返回 (allow, reason_if_blocked)。"""
    if not ip:
        return True, ""
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM anonymous_messages "
        "WHERE author_type='guest' AND ip_address = ? AND created_at > datetime('now', ?)",
        (ip, f"-{ANON_RATE_LIMIT_WINDOW_SEC} seconds"),
    )
    if rows and int(rows[0]["c"] or 0) >= ANON_RATE_LIMIT_MAX:
        return False, f"提交过于频繁，请稍后再试（{ANON_RATE_LIMIT_WINDOW_SEC} 秒内最多 {ANON_RATE_LIMIT_MAX} 条）"
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM anonymous_messages "
        "WHERE author_type='guest' AND ip_address = ? AND created_at > datetime('now', '-1 day')",
        (ip,),
    )
    if rows and int(rows[0]["c"] or 0) >= ANON_RATE_LIMIT_DAY_MAX:
        return False, f"今日匿名留言已达上限（{ANON_RATE_LIMIT_DAY_MAX} 条），请明天再来"
    return True, ""


async def create_anon_message(
    db: aiosqlite.Connection,
    *,
    nickname: str,
    content: str,
    ip_address: str = "",
    user_agent: str = "",
) -> dict:
    """匿名访客提交一条新留言（默认 pending、show_on_login=0，由管理员审核）。"""
    nick = (nickname or "").strip()[:MAX_ANON_NICKNAME_LEN]
    body = (content or "").strip()
    if not body:
        raise ValueError("留言内容不能为空")
    if len(body) > MAX_ANON_CONTENT_LEN:
        raise ValueError(f"留言内容超出 {MAX_ANON_CONTENT_LEN} 字符上限")
    cur = await db.execute(
        """INSERT INTO anonymous_messages
           (parent_id, author_type, nickname, content, ip_address, user_agent, show_on_login, status)
           VALUES (NULL, 'guest', ?, ?, ?, ?, 0, 'pending')""",
        (nick, body, (ip_address or "")[:64], (user_agent or "")[:255]),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM anonymous_messages WHERE id = ?", (cur.lastrowid,))
    return _row_to_anon_admin(rows[0])


async def list_login_board_public(db: aiosqlite.Connection, limit: int = 30) -> list[dict]:
    """登录页要展示的内容：仅 show_on_login=1 且 status='approved' 的留言；
    每条留言带上同样满足条件的回复。最多返回 limit 条留言。"""
    limit = max(1, min(int(limit or 30), 100))
    msg_rows = await db.execute_fetchall(
        "SELECT * FROM anonymous_messages "
        "WHERE parent_id IS NULL AND show_on_login = 1 AND status = 'approved' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    if not msg_rows:
        return []
    ids = [int(r["id"]) for r in msg_rows]
    placeholder = ",".join(["?"] * len(ids))
    reply_rows = await db.execute_fetchall(
        f"SELECT * FROM anonymous_messages "
        f"WHERE parent_id IN ({placeholder}) AND show_on_login = 1 AND status = 'approved' "
        f"ORDER BY created_at ASC",
        ids,
    )
    replies_by_parent: dict[int, list[dict]] = {}
    for r in reply_rows:
        replies_by_parent.setdefault(int(r["parent_id"]), []).append(_row_to_anon(r))
    out = []
    for r in msg_rows:
        d = _row_to_anon(r)
        d["replies"] = replies_by_parent.get(int(d["id"]), [])
        out.append(d)
    return out


async def list_anon_messages_admin(
    db: aiosqlite.Connection, *, status: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict]:
    """管理员视角列出全部留言（含回复）。可按 status 过滤。"""
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    where = "parent_id IS NULL"
    params: list[Any] = []
    if status and status in ("pending", "approved", "hidden"):
        where += " AND status = ?"
        params.append(status)
    msg_rows = await db.execute_fetchall(
        f"SELECT * FROM anonymous_messages WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    if not msg_rows:
        return []
    ids = [int(r["id"]) for r in msg_rows]
    placeholder = ",".join(["?"] * len(ids))
    reply_rows = await db.execute_fetchall(
        f"SELECT * FROM anonymous_messages WHERE parent_id IN ({placeholder}) ORDER BY created_at ASC",
        ids,
    )
    replies_by_parent: dict[int, list[dict]] = {}
    for r in reply_rows:
        replies_by_parent.setdefault(int(r["parent_id"]), []).append(_row_to_anon_admin(r))
    out = []
    for r in msg_rows:
        d = _row_to_anon_admin(r)
        d["replies"] = replies_by_parent.get(int(d["id"]), [])
        out.append(d)
    return out


async def update_anon_message_admin(
    db: aiosqlite.Connection,
    msg_id: int,
    *,
    show_on_login: bool | None = None,
    status: str | None = None,
) -> dict | None:
    """管理员修改一条匿名留言/回复的展示与审核状态。"""
    rows = await db.execute_fetchall("SELECT * FROM anonymous_messages WHERE id = ?", (int(msg_id),))
    if not rows:
        return None
    fields: list[str] = []
    params: list[Any] = []
    if show_on_login is not None:
        fields.append("show_on_login = ?")
        params.append(1 if show_on_login else 0)
    if status is not None:
        if status not in ("pending", "approved", "hidden"):
            raise ValueError("status 仅支持 pending / approved / hidden")
        fields.append("status = ?")
        params.append(status)
    if not fields:
        return _row_to_anon_admin(rows[0])
    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(int(msg_id))
    await db.execute(
        f"UPDATE anonymous_messages SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM anonymous_messages WHERE id = ?", (int(msg_id),))
    return _row_to_anon_admin(rows[0]) if rows else None


async def reply_anon_message_admin(
    db: aiosqlite.Connection,
    parent_id: int,
    admin_user_id: int,
    content: str,
    *,
    show_on_login: bool = False,
) -> dict:
    """管理员回复某条匿名留言。回复默认不公开展示，需手动开 show_on_login。"""
    body = (content or "").strip()
    if not body:
        raise ValueError("回复内容不能为空")
    if len(body) > MAX_ANON_CONTENT_LEN:
        raise ValueError(f"回复内容超出 {MAX_ANON_CONTENT_LEN} 字符上限")
    rows = await db.execute_fetchall(
        "SELECT id FROM anonymous_messages WHERE id = ? AND parent_id IS NULL",
        (int(parent_id),),
    )
    if not rows:
        raise ValueError("被回复的留言不存在或本身就是回复")
    cur = await db.execute(
        """INSERT INTO anonymous_messages
           (parent_id, author_type, author_user_id, content, show_on_login, status)
           VALUES (?, 'admin', ?, ?, ?, 'approved')""",
        (int(parent_id), int(admin_user_id), body, 1 if show_on_login else 0),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM anonymous_messages WHERE id = ?", (cur.lastrowid,))
    return _row_to_anon_admin(rows[0])


async def delete_anon_message_admin(db: aiosqlite.Connection, msg_id: int) -> bool:
    """硬删除一条留言（连同子回复因 ON DELETE CASCADE 一起删）。"""
    cur = await db.execute("DELETE FROM anonymous_messages WHERE id = ?", (int(msg_id),))
    await db.commit()
    return (cur.rowcount or 0) > 0


# ─────────────────────────────────────────────
# 用户反馈
# ─────────────────────────────────────────────

async def _has_any_reply(db: aiosqlite.Connection, feedback_id: int) -> bool:
    rows = await db.execute_fetchall(
        "SELECT 1 FROM user_feedback_replies WHERE feedback_id = ? LIMIT 1",
        (int(feedback_id),),
    )
    return bool(rows)


async def create_feedback(
    db: aiosqlite.Connection,
    user_id: int,
    *,
    title: str,
    content: str,
    category: str = "general",
    is_ai_submitted: bool = False,
) -> dict:
    body = (content or "").strip()
    if not body:
        raise ValueError("反馈内容不能为空")
    if len(body) > MAX_FEEDBACK_CONTENT_LEN:
        raise ValueError(f"反馈内容超出 {MAX_FEEDBACK_CONTENT_LEN} 字符上限")
    t = (title or "").strip()[:MAX_FEEDBACK_TITLE_LEN]
    cat = (category or "general").strip().lower()
    if cat not in VALID_FEEDBACK_CATEGORIES:
        cat = "general"
    cur = await db.execute(
        """INSERT INTO user_feedback (user_id, title, content, category, status, is_ai_submitted)
           VALUES (?, ?, ?, ?, 'open', ?)""",
        (int(user_id), t, body, cat, 1 if is_ai_submitted else 0),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM user_feedback WHERE id = ?", (cur.lastrowid,))
    return _row_to_feedback(rows[0])


async def update_feedback_by_user(
    db: aiosqlite.Connection,
    user_id: int,
    feedback_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
) -> dict:
    """用户编辑自己的反馈：仅 status='open' 且尚无任何管理员回复时允许。"""
    rows = await db.execute_fetchall(
        "SELECT * FROM user_feedback WHERE id = ? AND user_id = ?",
        (int(feedback_id), int(user_id)),
    )
    if not rows:
        raise ValueError("反馈不存在或无权访问")
    if rows[0]["status"] != "open":
        raise ValueError(f"反馈处于 {rows[0]['status']} 状态，无法编辑")
    if await _has_any_reply(db, feedback_id):
        raise ValueError("管理员已回复，反馈不可再编辑")
    fields: list[str] = []
    params: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        params.append((title or "").strip()[:MAX_FEEDBACK_TITLE_LEN])
    if content is not None:
        body = (content or "").strip()
        if not body:
            raise ValueError("反馈内容不能为空")
        if len(body) > MAX_FEEDBACK_CONTENT_LEN:
            raise ValueError(f"反馈内容超出 {MAX_FEEDBACK_CONTENT_LEN} 字符上限")
        fields.append("content = ?")
        params.append(body)
    if category is not None:
        cat = (category or "general").strip().lower()
        if cat not in VALID_FEEDBACK_CATEGORIES:
            cat = "general"
        fields.append("category = ?")
        params.append(cat)
    if not fields:
        return _row_to_feedback(rows[0])
    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(int(feedback_id))
    await db.execute(f"UPDATE user_feedback SET {', '.join(fields)} WHERE id = ?", params)
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM user_feedback WHERE id = ?", (int(feedback_id),))
    return _row_to_feedback(rows[0])


async def withdraw_feedback_by_user(
    db: aiosqlite.Connection, user_id: int, feedback_id: int
) -> bool:
    """用户撤回自己的反馈：仅 status='open' 且无任何回复时允许；物理删除。"""
    rows = await db.execute_fetchall(
        "SELECT status FROM user_feedback WHERE id = ? AND user_id = ?",
        (int(feedback_id), int(user_id)),
    )
    if not rows:
        raise ValueError("反馈不存在或无权访问")
    if rows[0]["status"] != "open":
        raise ValueError(f"反馈处于 {rows[0]['status']} 状态，无法撤回")
    if await _has_any_reply(db, feedback_id):
        raise ValueError("管理员已回复，反馈不可撤回")
    await db.execute("DELETE FROM user_feedback WHERE id = ?", (int(feedback_id),))
    await db.commit()
    return True


async def list_feedback_for_user(
    db: aiosqlite.Connection, user_id: int, *, limit: int = 100
) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT * FROM user_feedback WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (int(user_id), max(1, min(int(limit or 100), 500))),
    )
    if not rows:
        return []
    ids = [int(r["id"]) for r in rows]
    placeholder = ",".join(["?"] * len(ids))
    reply_rows = await db.execute_fetchall(
        f"SELECT r.*, u.username AS admin_username, u.display_name AS admin_display "
        f"FROM user_feedback_replies r LEFT JOIN users u ON u.id = r.admin_user_id "
        f"WHERE r.feedback_id IN ({placeholder}) ORDER BY r.created_at ASC",
        ids,
    )
    replies_by_fb: dict[int, list[dict]] = {}
    for r in reply_rows:
        d = _row_to_reply(r)
        d["admin_username"] = (r["admin_username"] or "")
        d["admin_display"] = (r["admin_display"] or "")
        replies_by_fb.setdefault(int(r["feedback_id"]), []).append(d)
    out = []
    for r in rows:
        d = _row_to_feedback(r)
        d["replies"] = replies_by_fb.get(int(d["id"]), [])
        out.append(d)
    return out


async def list_feedback_for_admin(
    db: aiosqlite.Connection, *, filter_kind: str = "all", limit: int = 100, offset: int = 0
) -> dict:
    """管理员视角分页列出反馈。filter_kind:
    - all：全部
    - unread：admin_read_at IS NULL
    - open：状态 open
    - replied：状态 replied
    - ignored：状态 ignored
    返回 {items, total, unread_total}。
    """
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    where = "1=1"
    params: list[Any] = []
    if filter_kind == "unread":
        where = "f.admin_read_at IS NULL"
    elif filter_kind in ("open", "replied", "ignored"):
        where = "f.status = ?"
        params.append(filter_kind)
    rows = await db.execute_fetchall(
        f"SELECT f.*, u.username AS submitter_username, u.display_name AS submitter_display, u.email AS submitter_email "
        f"FROM user_feedback f LEFT JOIN users u ON u.id = f.user_id "
        f"WHERE {where} ORDER BY f.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    total_rows = await db.execute_fetchall(
        f"SELECT COUNT(*) AS c FROM user_feedback f WHERE {where}", params
    )
    total = int(total_rows[0]["c"]) if total_rows else 0
    unread_rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM user_feedback WHERE admin_read_at IS NULL"
    )
    unread = int(unread_rows[0]["c"]) if unread_rows else 0
    items: list[dict] = []
    if rows:
        ids = [int(r["id"]) for r in rows]
        placeholder = ",".join(["?"] * len(ids))
        reply_rows = await db.execute_fetchall(
            f"SELECT r.*, u.username AS admin_username, u.display_name AS admin_display "
            f"FROM user_feedback_replies r LEFT JOIN users u ON u.id = r.admin_user_id "
            f"WHERE r.feedback_id IN ({placeholder}) ORDER BY r.created_at ASC",
            ids,
        )
        replies_by_fb: dict[int, list[dict]] = {}
        for r in reply_rows:
            d = _row_to_reply(r)
            d["admin_username"] = (r["admin_username"] or "")
            d["admin_display"] = (r["admin_display"] or "")
            replies_by_fb.setdefault(int(r["feedback_id"]), []).append(d)
        for r in rows:
            d = _row_to_feedback(r)
            d["submitter_username"] = (r["submitter_username"] or "")
            d["submitter_display"] = (r["submitter_display"] or "")
            d["submitter_email"] = (r["submitter_email"] or "")
            d["replies"] = replies_by_fb.get(int(d["id"]), [])
            items.append(d)
    return {"items": items, "total": total, "unread_total": unread}


async def get_feedback_detail(
    db: aiosqlite.Connection, feedback_id: int, *, requester_user_id: int, is_admin: bool
) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT f.*, u.username AS submitter_username, u.display_name AS submitter_display, u.email AS submitter_email "
        "FROM user_feedback f LEFT JOIN users u ON u.id = f.user_id WHERE f.id = ?",
        (int(feedback_id),),
    )
    if not rows:
        return None
    if not is_admin and rows[0]["user_id"] != requester_user_id:
        return None
    d = _row_to_feedback(rows[0])
    d["submitter_username"] = (rows[0]["submitter_username"] or "")
    d["submitter_display"] = (rows[0]["submitter_display"] or "")
    d["submitter_email"] = (rows[0]["submitter_email"] or "")
    reply_rows = await db.execute_fetchall(
        "SELECT r.*, u.username AS admin_username, u.display_name AS admin_display "
        "FROM user_feedback_replies r LEFT JOIN users u ON u.id = r.admin_user_id "
        "WHERE r.feedback_id = ? ORDER BY r.created_at ASC",
        (int(feedback_id),),
    )
    out_replies: list[dict] = []
    for r in reply_rows:
        rd = _row_to_reply(r)
        rd["admin_username"] = (r["admin_username"] or "")
        rd["admin_display"] = (r["admin_display"] or "")
        out_replies.append(rd)
    d["replies"] = out_replies
    return d


async def mark_feedback_read(db: aiosqlite.Connection, feedback_id: int) -> None:
    """单条标已读（管理员侧）。"""
    await db.execute(
        "UPDATE user_feedback SET admin_read_at = CURRENT_TIMESTAMP WHERE id = ? AND admin_read_at IS NULL",
        (int(feedback_id),),
    )
    await db.commit()


async def mark_all_feedback_read(db: aiosqlite.Connection) -> int:
    """把所有未读反馈标为已读，返回影响行数。"""
    cur = await db.execute(
        "UPDATE user_feedback SET admin_read_at = CURRENT_TIMESTAMP WHERE admin_read_at IS NULL"
    )
    await db.commit()
    return cur.rowcount or 0


async def reply_feedback_admin(
    db: aiosqlite.Connection,
    feedback_id: int,
    admin_user_id: int,
    content: str,
    *,
    is_ai_drafted: bool = False,
) -> dict:
    body = (content or "").strip()
    if not body:
        raise ValueError("回复内容不能为空")
    if len(body) > MAX_FEEDBACK_CONTENT_LEN:
        raise ValueError(f"回复内容超出 {MAX_FEEDBACK_CONTENT_LEN} 字符上限")
    rows = await db.execute_fetchall("SELECT id, status FROM user_feedback WHERE id = ?", (int(feedback_id),))
    if not rows:
        raise ValueError("反馈不存在")
    cur = await db.execute(
        """INSERT INTO user_feedback_replies (feedback_id, admin_user_id, content, is_ai_drafted)
           VALUES (?, ?, ?, ?)""",
        (int(feedback_id), int(admin_user_id), body, 1 if is_ai_drafted else 0),
    )
    await db.execute(
        "UPDATE user_feedback SET status='replied', admin_read_at = COALESCE(admin_read_at, CURRENT_TIMESTAMP), "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (int(feedback_id),),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM user_feedback_replies WHERE id = ?", (cur.lastrowid,))
    return _row_to_reply(rows[0])


async def update_reply_admin(
    db: aiosqlite.Connection, reply_id: int, admin_user_id: int, content: str
) -> dict:
    """管理员编辑自己写过的回复（不允许编辑别人写的回复，但可撤回别人的回复——按通用 admin 权限处理）。"""
    body = (content or "").strip()
    if not body:
        raise ValueError("回复内容不能为空")
    rows = await db.execute_fetchall("SELECT * FROM user_feedback_replies WHERE id = ?", (int(reply_id),))
    if not rows:
        raise ValueError("回复不存在")
    if rows[0]["admin_user_id"] != admin_user_id:
        raise ValueError("仅回复作者本人可编辑此回复（其他管理员可撤回后重写）")
    await db.execute(
        "UPDATE user_feedback_replies SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (body, int(reply_id)),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM user_feedback_replies WHERE id = ?", (int(reply_id),))
    return _row_to_reply(rows[0])


async def delete_reply_admin(db: aiosqlite.Connection, reply_id: int) -> int | None:
    """管理员撤回回复：物理删除。若该反馈再无 reply，状态退回 'open'。返回受影响的 feedback_id。"""
    rows = await db.execute_fetchall("SELECT feedback_id FROM user_feedback_replies WHERE id = ?", (int(reply_id),))
    if not rows:
        return None
    feedback_id = int(rows[0]["feedback_id"])
    await db.execute("DELETE FROM user_feedback_replies WHERE id = ?", (int(reply_id),))
    if not await _has_any_reply(db, feedback_id):
        await db.execute(
            "UPDATE user_feedback SET status='open', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (feedback_id,),
        )
    await db.commit()
    return feedback_id


async def ignore_feedback_admin(
    db: aiosqlite.Connection, feedback_id: int
) -> dict | None:
    """忽略一条反馈：status='ignored' 且标已读；不影响已有 reply 行。"""
    rows = await db.execute_fetchall("SELECT * FROM user_feedback WHERE id = ?", (int(feedback_id),))
    if not rows:
        return None
    await db.execute(
        "UPDATE user_feedback SET status='ignored', admin_read_at = COALESCE(admin_read_at, CURRENT_TIMESTAMP), "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (int(feedback_id),),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM user_feedback WHERE id = ?", (int(feedback_id),))
    return _row_to_feedback(rows[0]) if rows else None


async def reopen_feedback_admin(
    db: aiosqlite.Connection, feedback_id: int
) -> dict | None:
    """把已忽略的反馈重新打开（用于「撤销忽略」），按是否有回复决定恢复到 open 还是 replied。"""
    rows = await db.execute_fetchall("SELECT id FROM user_feedback WHERE id = ?", (int(feedback_id),))
    if not rows:
        return None
    new_status = "replied" if await _has_any_reply(db, feedback_id) else "open"
    await db.execute(
        "UPDATE user_feedback SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_status, int(feedback_id)),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM user_feedback WHERE id = ?", (int(feedback_id),))
    return _row_to_feedback(rows[0]) if rows else None


# ─────────────────────────────────────────────
# settings
# ─────────────────────────────────────────────

_TRUE_TOKENS = {"true", "1", "on", "yes", "y", "t"}


async def get_notify_admin_on_feedback(db: aiosqlite.Connection) -> bool:
    """读取 settings.notify_admin_on_user_feedback；兼容 true/1/on/yes 等多种真值书写。"""
    rows = await db.execute_fetchall(
        "SELECT value FROM settings WHERE key = 'notify_admin_on_user_feedback'"
    )
    if not rows:
        return False
    return (rows[0]["value"] or "").strip().lower() in _TRUE_TOKENS


async def get_admin_emails(db: aiosqlite.Connection) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT email FROM users WHERE role='admin' AND email IS NOT NULL AND email != ''"
    )
    return [(r["email"] or "").strip() for r in rows if (r["email"] or "").strip()]
