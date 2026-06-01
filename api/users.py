"""用户管理 API"""
import asyncio
import logging
import shutil
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import bcrypt as _bcrypt

import config
from database import get_db
from api.auth import get_current_user, require_admin, _is_admin_role, normalize_and_validate_username

logger = logging.getLogger("edgeops.users")

router = APIRouter(prefix="/api/users", tags=["用户管理"])

# 用户 status：active 正常 | locked 安全锁定（多次错密等）| suspended 管理员暂停。locked 不可由管理员 PUT 写入，仅系统与解锁接口处理。
_ADMIN_SETTABLE_STATUSES = frozenset({"active", "suspended"})


def _user_public_dict(row: dict) -> dict:
    """对外统一以 status 表达三态；locked 时仅返回 lock_expires_at（原 locked_until），不返回重复字段。"""
    d = dict(row)
    lu = d.get("locked_until")
    st = (d.get("status") or "").strip().lower()
    d.pop("locked_until", None)
    d["skills_enabled"] = bool(d.get("skills_enabled", 0))
    if st == "locked" and lu:
        d["lock_expires_at"] = lu
    return d


async def _bcrypt_hash(password: str) -> str:
    return await asyncio.to_thread(
        lambda: _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
    )


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "user"


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    role: str | None = None
    status: str | None = None
    email: str | None = None
    skills_enabled: bool | None = None


class ResetPasswordRequest(BaseModel):
    password: str


class SelfDeleteRequest(BaseModel):
    password: str
    confirm: str | None = None


def _check_page_size(page_size: int) -> int:
    if page_size not in (20, 50, 100):
        return 20
    return page_size


@router.get("")
async def list_users(
    page: int = 1,
    page_size: int = 20,
    user=Depends(require_admin),
):
    page_size = _check_page_size(page_size)
    page = max(1, page)
    offset = (page - 1) * page_size
    db = await get_db()
    count_rows = await db.execute_fetchall("SELECT COUNT(*) as n FROM users")
    total = count_rows[0][0] if count_rows else 0
    rows = await db.execute_fetchall(
        "SELECT id, username, display_name, role, status, email, failed_login_attempts, locked_until, COALESCE(skills_enabled, 0) AS skills_enabled, created_at, last_login FROM users ORDER BY id LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    return {"success": True, "users": [_user_public_dict(dict(r)) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/search")
async def search_users(
    query: str = Query(..., min_length=1, description="用户名或显示名关键字"),
    limit: int = Query(20, ge=1, le=50, description="最多返回条数"),
    user=Depends(get_current_user),
):
    """按用户名/显示名模糊搜索用户（登录用户可用，用于分享主机等场景的自动补全）。"""
    q_raw = (query or "").strip()
    if not q_raw:
        raise HTTPException(status_code=400, detail="query 不能为空")
    db = await get_db()
    like = f"%{q_raw.lower()}%"
    prefix_like = f"{q_raw.lower()}%"
    me_id = int(user["id"])
    rows = await db.execute_fetchall(
        """SELECT id, username, display_name
           FROM users
           WHERE id != ?
             AND LOWER(status) = 'active'
             AND (
               LOWER(username) LIKE ?
               OR LOWER(IFNULL(display_name, '')) LIKE ?
             )
           ORDER BY
             CASE WHEN LOWER(username) = LOWER(?) THEN 0
                  WHEN LOWER(username) LIKE ? THEN 1
                  ELSE 2 END,
             username
           LIMIT ?""",
        (me_id, like, like, q_raw, prefix_like, limit),
    )
    return {
        "success": True,
        "users": [
            {
                "id": int(r["id"]),
                "username": r["username"] or "",
                "display_name": r["display_name"] or "",
            }
            for r in rows
        ],
    }


@router.post("")
async def create_user(req: CreateUserRequest, user=Depends(require_admin)):
    username = normalize_and_validate_username(req.username)
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO users (username, display_name, password_hash, role) VALUES (?, ?, ?, ?)",
            (
                username,
                req.display_name or username,
                await _bcrypt_hash(req.password),
                req.role,
            ),
        )
        await db.commit()
        return {"success": True, "id": cursor.lastrowid}
    except Exception:
        raise HTTPException(status_code=400, detail="用户名已存在")


@router.put("/{user_id}")
async def update_user(user_id: int, req: UpdateUserRequest, user=Depends(require_admin)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, role, status, username, COALESCE(skills_enabled, 0) AS skills_enabled FROM users WHERE id = ?",
        (user_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    target = dict(rows[0])
    if _is_admin_role(target.get("role")) and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="仅管理员可修改管理员账户")
    old_status = (target.get("status") or "").strip()
    old_skills_enabled = bool(target.get("skills_enabled", 0))

    updates, params = [], []
    if req.display_name is not None:
        updates.append("display_name = ?")
        params.append(req.display_name)
    if req.role is not None:
        updates.append("role = ?")
        params.append(req.role)
    if req.status is not None:
        ns = req.status.strip().lower()
        if ns not in _ADMIN_SETTABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail="无效的状态：管理员仅可设为 active（正常）或 suspended（暂停）。安全锁定 locked 由系统自动产生，请使用「解锁」接口。",
            )
        updates.append("status = ?")
        params.append(ns)
        if ns == "suspended":
            updates.append("locked_until = NULL")
            updates.append("failed_login_attempts = 0")
        elif ns == "active":
            updates.append("locked_until = NULL")
            updates.append("failed_login_attempts = 0")
    if req.email is not None:
        updates.append("email = ?")
        params.append((req.email or "").strip())
    if req.skills_enabled is not None:
        updates.append("skills_enabled = ?")
        params.append(1 if req.skills_enabled else 0)
    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(user_id)
        await db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
        if req.skills_enabled is True and not old_skills_enabled:
            try:
                from services.user_skills_registry import scan_user_skills_from_disk

                urows = await db.execute_fetchall(
                    """SELECT id, username, display_name, role, status, email,
                              COALESCE(skills_enabled, 0) AS skills_enabled FROM users WHERE id = ?""",
                    (user_id,),
                )
                if urows:
                    ud = dict(urows[0])
                    ud["skills_enabled"] = bool(ud.get("skills_enabled"))
                    result = await scan_user_skills_from_disk(db, user_id, ud)
                    logger.info(
                        "用户 %s 开启 Skills，已自动扫描 skills/：count=%s",
                        ud.get("username"),
                        result.get("count", 0),
                    )
            except Exception as e:
                logger.warning("开启 Skills 后自动扫描磁盘失败 user_id=%s: %s", user_id, e)
        if req.status is not None:
            from services.email_sender import send_notification_to_user
            uname = target.get("username") or ""
            if req.status.strip().lower() == "suspended":
                await send_notification_to_user(db, user_id, "email_template_suspend_body", uname)
            elif old_status.lower() == "suspended" and req.status.strip().lower() == "active":
                await send_notification_to_user(db, user_id, "email_template_restore_body", uname)
    return {"success": True}


def _safe_username(name: str) -> str:
    if not name or not isinstance(name, str):
        return "default"
    s = "".join(c for c in name.strip() if c.isalnum() or c in "._-")[:64]
    return s or "default"


async def _table_exists(db, table: str) -> bool:
    """判断表是否存在（部分老库可能缺少某些表，兼容性用）。"""
    rows = await db.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return bool(rows)


async def _chunked_in_delete(db, sql_template: str, ids: list) -> None:
    """针对 `... IN (?, ?, ...)` 的批量删除，自动按 500 一批切块，避免 SQLite 参数数量上限。
    `sql_template` 必须含占位符 `{placeholders}`。
    """
    if not ids:
        return
    CHUNK = 500
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        await db.execute(sql_template.format(placeholders=placeholders), chunk)


async def delete_user_with_cascade(db, user_id: int, *, actor_user_id: int | None = None):
    """删除用户并级联删除其**一切**关联数据与 web/fs 目录。不执行 commit。
    返回 (True, None) 成功，或 (False, error_message) 失败。

    管理员删除规则：
    - 管理员不能删除自己（后台删除接口传 actor_user_id 时强制校验）。
    - 管理员账户数量大于 1 时，允许一位管理员删除另一位管理员。
    - 不允许删除最后一个管理员账户。

    清理范围（务必保持与 `docs/数据库结构.md` 的"用户删除级联清理"小节一致）：
    A. 该用户所创建的主机，及由这些主机所产生的**所有用户**（包括被分享方）侧数据：
       - ai_chat_sessions / ai_chat_messages（host_id 指向这些主机的）
       - ai_host_knowledge / ai_host_prompts（host_id 指向这些主机的）
       - host_shares（host_id 指向这些主机的）
       - host_user_tags、host_group_members（host_id 指向这些主机的）
       - ssh_channels、batch_operation_details、operation_logs（host_id 指向这些主机的）
       - 然后再删除主机行本身
    B. 该用户自身的每用户数据（user_id = <user_id>）：
       - ai_chat_sessions（所有 scope，含全局 / 本机 / 主机会话）及其 messages
       - ai_host_knowledge / ai_host_prompts（作为"被分享方"在别人主机上的个人知识/提示词）
       - host_user_tags、host_tags（个人标签）
       - host_shares（作为被分享方 `shared_with_user_id` 的份额）
       - host_groups（及其 host_group_members 映射）
       - credentials（本人创建的）
       - triggered_tasks / triggered_task_runs / triggered_task_run_messages / triggered_task_expose
       - scheduled_tasks / scheduled_task_runs / scheduled_task_run_messages
       - ssh_channels、batch_operations / batch_operation_details
       - local_shell_sessions / local_shell_logs
       - api_tokens、user_mail_config、user_ai_config、user_system_ai_usage
       - user_login_events、operation_logs、server_maintenance_history(created_by)
       - email_verification_codes / password_reset_tokens
       - user_feedback（连同 user_feedback_replies 因 ON DELETE CASCADE 一并删除）
       - user_feedback_replies（作为管理员回复作者：admin_user_id 指向本人的回复行）
       - anonymous_messages.author_user_id：作者引用置 NULL（保留留言/回复内容本身）
       - best_practices：不删内容，仅 `created_by = NULL`（共享知识库不因个人账户销毁而丢失）
    C. users 表自身行 + web/fs 目录（按 username 清理）。

    注：以上三类反馈相关清理，SQLite 在 `PRAGMA foreign_keys=ON` 时也会自动级联，
    这里**显式重复一遍**是为了：① 与 docs/数据库结构.md 的"用户删除级联清理"小节一致；
    ② 防御外键被未来迁移意外改弱；③ 让 grep 能直接定位到所有受影响的表。
    """
    rows = await db.execute_fetchall("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    if not rows:
        return (False, "用户不存在")
    row = dict(rows[0])
    if actor_user_id is not None and int(actor_user_id) == int(user_id):
        return (False, "管理员不能删除自己的账户")
    if _is_admin_role(row.get("role")):
        admin_rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM users WHERE lower(COALESCE(role, '')) IN ('admin', 'manager') OR role = '管理员'"
        )
        admin_count = int(admin_rows[0][0]) if admin_rows else 0
        if admin_count <= 1:
            return (False, "不能删除最后一个管理员账户")
    username = row.get("username") or ""

    # ── A. 收集该用户创建的主机/分组 ID ──────────────────────────────
    host_rows = await db.execute_fetchall("SELECT id FROM hosts WHERE created_by = ?", (user_id,))
    host_id_list = [r["id"] for r in host_rows]
    group_rows = await db.execute_fetchall("SELECT id FROM host_groups WHERE created_by = ?", (user_id,))
    group_id_list = [r["id"] for r in group_rows]

    # ── A.1 先清理"其他用户因这些主机形成的数据"，再删主机本身 ────
    # 注意：不管是否为该用户本人持有，凡是 host_id 指向这些主机的每用户数据都要清掉
    if host_id_list:
        for tpl in [
            "DELETE FROM ai_chat_messages WHERE session_id IN (SELECT id FROM ai_chat_sessions WHERE host_id IN ({placeholders}))",
            "DELETE FROM ai_chat_sessions WHERE host_id IN ({placeholders})",
            "DELETE FROM ai_host_knowledge WHERE host_id IN ({placeholders})",
            "DELETE FROM ai_host_prompts WHERE host_id IN ({placeholders})",
            "DELETE FROM host_shares WHERE host_id IN ({placeholders})",
            "DELETE FROM host_user_tags WHERE host_id IN ({placeholders})",
            "DELETE FROM host_group_members WHERE host_id IN ({placeholders})",
            "DELETE FROM batch_operation_details WHERE host_id IN ({placeholders})",
            "DELETE FROM operation_logs WHERE host_id IN ({placeholders})",
        ]:
            await _chunked_in_delete(db, tpl, host_id_list)
        if await _table_exists(db, "ssh_channels"):
            await _chunked_in_delete(
                db, "DELETE FROM ssh_channels WHERE host_id IN ({placeholders})", host_id_list
            )
        # 最后删主机
        await _chunked_in_delete(db, "DELETE FROM hosts WHERE id IN ({placeholders})", host_id_list)

    # ── A.2 该用户创建的主机分组（及其成员映射：按 group_id） ────
    if group_id_list:
        await _chunked_in_delete(
            db, "DELETE FROM host_group_members WHERE group_id IN ({placeholders})", group_id_list
        )
    await db.execute("DELETE FROM host_groups WHERE created_by = ?", (user_id,))

    # ── B. 该用户自身的每用户数据（user_id = <user_id>） ────────────
    # B.1 AI 会话 + 消息（含全局 / 本机 / 在他人主机上的会话）
    await db.execute(
        "DELETE FROM ai_chat_messages WHERE session_id IN (SELECT id FROM ai_chat_sessions WHERE user_id = ?)",
        (user_id,),
    )
    await db.execute("DELETE FROM ai_chat_sessions WHERE user_id = ?", (user_id,))

    # B.2 该用户作为"被分享方"在别人主机上的个人知识/提示词/标签 —
    #     以及作为被分享方 的分享记录本身
    await db.execute("DELETE FROM ai_host_knowledge WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM ai_host_prompts WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "host_user_tags"):
        await db.execute("DELETE FROM host_user_tags WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "host_tags"):
        await db.execute("DELETE FROM host_tags WHERE created_by = ?", (user_id,))
    if await _table_exists(db, "host_shares"):
        await db.execute(
            "DELETE FROM host_shares WHERE shared_with_user_id = ? OR owner_user_id = ?",
            (user_id, user_id),
        )

    # B.3 凭证（本人创建的；凡是本人主机已在 A 里删掉，此处不会再有引用）
    await db.execute("DELETE FROM credentials WHERE created_by = ?", (user_id,))

    # B.4 触发任务 + 定时任务（按 user_id）及其 runs / messages / expose
    if await _table_exists(db, "triggered_tasks"):
        tt_rows = await db.execute_fetchall(
            "SELECT id FROM triggered_tasks WHERE user_id = ?", (user_id,)
        )
        tt_ids = [r["id"] for r in tt_rows]
        if tt_ids:
            if await _table_exists(db, "triggered_task_run_messages"):
                await _chunked_in_delete(
                    db,
                    "DELETE FROM triggered_task_run_messages WHERE run_id IN (SELECT id FROM triggered_task_runs WHERE task_id IN ({placeholders}))",
                    tt_ids,
                )
            if await _table_exists(db, "triggered_task_runs"):
                await _chunked_in_delete(
                    db, "DELETE FROM triggered_task_runs WHERE task_id IN ({placeholders})", tt_ids
                )
            if await _table_exists(db, "triggered_task_expose"):
                await _chunked_in_delete(
                    db, "DELETE FROM triggered_task_expose WHERE task_id IN ({placeholders})", tt_ids
                )
        await db.execute("DELETE FROM triggered_tasks WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "scheduled_tasks"):
        st_rows = await db.execute_fetchall(
            "SELECT id FROM scheduled_tasks WHERE user_id = ?", (user_id,)
        )
        st_ids = [r["id"] for r in st_rows]
        if st_ids:
            if await _table_exists(db, "scheduled_task_run_messages"):
                await _chunked_in_delete(
                    db,
                    "DELETE FROM scheduled_task_run_messages WHERE run_id IN (SELECT id FROM scheduled_task_runs WHERE task_id IN ({placeholders}))",
                    st_ids,
                )
            if await _table_exists(db, "scheduled_task_runs"):
                await _chunked_in_delete(
                    db, "DELETE FROM scheduled_task_runs WHERE task_id IN ({placeholders})", st_ids
                )
        await db.execute("DELETE FROM scheduled_tasks WHERE user_id = ?", (user_id,))

    # B.5 SSH 通道（按 user_id）
    if await _table_exists(db, "ssh_channels"):
        await db.execute("DELETE FROM ssh_channels WHERE user_id = ?", (user_id,))

    # B.6 批量操作（按 created_by）及其明细
    batch_rows = await db.execute_fetchall(
        "SELECT id FROM batch_operations WHERE created_by = ?", (user_id,)
    )
    batch_ids = [r["id"] for r in batch_rows]
    if batch_ids:
        await _chunked_in_delete(
            db, "DELETE FROM batch_operation_details WHERE batch_id IN ({placeholders})", batch_ids
        )
    await db.execute("DELETE FROM batch_operations WHERE created_by = ?", (user_id,))

    # B.7 本机 Shell 会话与日志
    local_rows = await db.execute_fetchall(
        "SELECT id FROM local_shell_sessions WHERE user_id = ?", (user_id,)
    )
    local_ids = [r["id"] for r in local_rows]
    if local_ids:
        await _chunked_in_delete(
            db, "DELETE FROM local_shell_logs WHERE session_id IN ({placeholders})", local_ids
        )
    await db.execute("DELETE FROM local_shell_sessions WHERE user_id = ?", (user_id,))

    # B.8 个人配置 / 配额 / 通知 / 令牌
    await db.execute("DELETE FROM user_ai_config WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM user_system_ai_usage WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "user_mail_config"):
        await db.execute("DELETE FROM user_mail_config WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "api_tokens"):
        await db.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "user_mcp_servers"):
        await db.execute("DELETE FROM user_mcp_servers WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "user_login_events"):
        await db.execute("DELETE FROM user_login_events WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM email_verification_codes WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))

    # B.9 操作日志 / 维护历史（该用户所做的）
    await db.execute("DELETE FROM operation_logs WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM server_maintenance_history WHERE created_by = ?", (user_id,))

    # B.10 best_practices 为共享知识库，不删内容、仅移除个人归属
    if await _table_exists(db, "best_practices"):
        await db.execute(
            "UPDATE best_practices SET created_by = NULL WHERE created_by = ?", (user_id,)
        )

    # B.11 用户反馈与登录页留言板（PRAGMA foreign_keys 也会级联，这里显式做一遍以保持可见性）
    if await _table_exists(db, "user_feedback_replies"):
        await db.execute(
            "DELETE FROM user_feedback_replies WHERE admin_user_id = ?", (user_id,)
        )
    if await _table_exists(db, "user_feedback"):
        await db.execute("DELETE FROM user_feedback WHERE user_id = ?", (user_id,))
    if await _table_exists(db, "anonymous_messages"):
        await db.execute(
            "UPDATE anonymous_messages SET author_user_id = NULL WHERE author_user_id = ?",
            (user_id,),
        )

    # ── C. 最后删除用户本行 + 清理 web/fs 目录 ─────────────────────
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    try:
        fs_dir = Path(config.FS_DIR).resolve()
        user_fs = (fs_dir / _safe_username(username)).resolve()
        if user_fs.exists() and user_fs.is_dir() and str(user_fs).startswith(str(fs_dir)):
            shutil.rmtree(user_fs, ignore_errors=True)
    except Exception:
        pass
    return (True, None)


@router.delete("/{user_id}")
async def delete_user(user_id: int, user=Depends(require_admin)):
    db = await get_db()
    ok, err = await delete_user_with_cascade(db, user_id, actor_user_id=int(user["id"]))
    if not ok:
        raise HTTPException(status_code=404 if err == "用户不存在" else 403, detail=err or "删除失败")
    await db.commit()
    return {"success": True}


@router.post("/me/delete")
async def delete_my_account(req: SelfDeleteRequest, user=Depends(get_current_user)):
    """用户自助注销账户：校验当前密码后**级联删除自己一切数据**。

    - 前端应要求用户再次输入当前密码，并传 `confirm == "DELETE"` 作为二次确认。
    - 管理员账户禁止自助注销（避免误删最后一个管理员），请由另一位管理员从后台删除。
    """
    confirm = (req.confirm or "").strip()
    if confirm != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="需要二次确认：请在 confirm 字段中原样填写 'DELETE'",
        )
    if _is_admin_role(user.get("role")):
        raise HTTPException(
            status_code=403,
            detail="管理员不能自助注销账户，请联系另一位管理员在「用户管理」里删除该账号，以免删除掉最后一个管理员。",
        )
    password = (req.password or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="需要输入当前密码")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
    )
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    from api.auth import _bcrypt_check
    if not await _bcrypt_check(password, rows[0]["password_hash"] or ""):
        raise HTTPException(status_code=400, detail="当前密码错误")
    ok, err = await delete_user_with_cascade(db, int(user["id"]))
    if not ok:
        raise HTTPException(status_code=400, detail=err or "注销失败")
    await db.commit()
    return {
        "success": True,
        "message": "账户已注销。你的所有主机、凭证、AI 会话、个人配置等数据均已清除；通过你分享给其他用户的主机及对方因这些主机形成的数据（含聊天记录）也已一并清理。",
    }


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int, req: ResetPasswordRequest, user=Depends(require_admin)
):
    db = await get_db()
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP, failed_login_attempts = 0, locked_until = NULL, status = 'active' WHERE id = ?",
        (await _bcrypt_hash(req.password), user_id),
    )
    await db.commit()
    return {"success": True}


async def perform_admin_security_unlock(db, user_id: int) -> tuple[bool, str | None]:
    """
    解除安全锁定（status=locked）。成功返回 (True, None)；失败返回 (False, 错误信息)。
    """
    rows = await db.execute_fetchall("SELECT id, username, status FROM users WHERE id = ?", (user_id,))
    if not rows:
        return (False, "用户不存在")
    target = dict(rows[0])
    st = (target.get("status") or "").strip().lower()
    if st != "locked":
        return (
            False,
            "该用户当前不是安全锁定状态。若为管理员暂停，请使用「恢复」将 status 设为 active，而非本操作。",
        )
    uname = (target.get("username") or "").strip()
    await db.execute(
        "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, status = 'active' WHERE id = ?",
        (user_id,),
    )
    await db.commit()
    from services.email_sender import send_notification_to_user

    await send_notification_to_user(db, user_id, "email_template_unlock_body", uname)
    return (True, None)


@router.post("/{user_id}/unlock")
async def unlock_user(user_id: int, user=Depends(require_admin)):
    """管理员解除安全锁定（status=locked）；暂停账户请使用「恢复」。"""
    db = await get_db()
    ok, err = await perform_admin_security_unlock(db, user_id)
    if not ok:
        raise HTTPException(status_code=404 if err == "用户不存在" else 400, detail=err or "解锁失败")
    return {"success": True}
