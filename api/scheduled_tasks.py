"""定时任务 API — 任务列表、CRUD、执行历史、立即执行、可供 AI 调用的触发任务列表

设计见 docs/SSH通道与后台任务设计.md。调度器在 startup 启动，到点执行 run_scheduled_task。
"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user
from services.scheduler import _next_run_from_cron
from services.task_runner import run_scheduled_task
from services.user_mail import effective_scheduled_task_notify_email_to

router = APIRouter(prefix="/api/scheduled-tasks", tags=["定时任务"])


def _scheduled_task_api_dict(row: dict) -> dict:
    """列表/详情统一补充通知邮箱展示字段（库为空时自正文解析显式行）。"""
    d = dict(row)
    stored = d.get("notify_email_to") or ""
    content = d.get("content") or ""
    disp = effective_scheduled_task_notify_email_to(stored, content)
    d["notify_email_display"] = disp
    d["notify_email_inferred"] = bool(not (stored or "").strip() and disp)
    d["inject_user_skills"] = bool(d.get("inject_user_skills"))
    return d


class ScheduledTaskCreate(BaseModel):
    name: str
    content: str
    cron_expr: Optional[str] = None
    enabled: bool = True
    notify_email_to: Optional[str] = None
    inject_user_skills: bool = False


class ScheduledTaskUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    cron_expr: Optional[str] = None
    enabled: Optional[bool] = None
    notify_email_to: Optional[str] = None
    inject_user_skills: Optional[bool] = None


@router.get("")
async def list_scheduled_tasks(user=Depends(get_current_user)):
    """列出当前用户的定时任务。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, name, content, cron_expr, next_run_at, created_at, updated_at, last_run_at, last_run_status, is_running, enabled, notify_email_to,
                  COALESCE(inject_user_skills, 0) AS inject_user_skills
           FROM scheduled_tasks WHERE user_id = ? ORDER BY updated_at DESC""",
        (user["id"],),
    )
    out = []
    for r in rows:
        d = _scheduled_task_api_dict(dict(r))
        d["inject_user_skills"] = bool(d.get("inject_user_skills"))
        out.append(d)
    return {"success": True, "tasks": out}


@router.get("/triggered-list")
async def list_triggered_for_ai(user=Depends(get_current_user)):
    """本用户可用的触发任务列表（供定时任务 AI 决策是否调用）。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.id, t.name, t.intro FROM triggered_tasks t WHERE t.user_id = ? ORDER BY t.name""",
        (user["id"],),
    )
    return {"success": True, "triggered_tasks": [dict(r) for r in rows]}


@router.get("/all-runs")
async def list_all_runs(
    task_id: Optional[int] = Query(None, description="按任务 ID 筛选"),
    task_name: Optional[str] = Query(None, description="按任务名筛选（模糊）"),
    status: Optional[str] = Query(None, description="按状态筛选：running/completed/failed"),
    from_time: Optional[str] = Query(None, description="执行时间起，ISO 或 YYYY-MM-DD"),
    to_time: Optional[str] = Query(None, description="执行时间止"),
    limit: int = Query(100, ge=1, le=500),
    user=Depends(get_current_user),
):
    """全局执行历史：按任务 ID、任务名、状态、执行时间筛选。"""
    db = await get_db()
    conditions = ["r.task_id IN (SELECT id FROM scheduled_tasks WHERE user_id = ?)"]
    params = [user["id"]]
    if task_id is not None:
        conditions.append("r.task_id = ?")
        params.append(task_id)
    if task_name and task_name.strip():
        conditions.append("t.name LIKE ?")
        params.append("%" + task_name.strip() + "%")
    if status and status.strip():
        conditions.append("r.status = ?")
        params.append(status.strip())
    if from_time and from_time.strip():
        conditions.append("r.run_at >= ?")
        params.append(from_time.strip())
    if to_time and to_time.strip():
        conditions.append("r.run_at <= ?")
        params.append(to_time.strip())
    params.append(limit)
    sql = (
        """SELECT r.id, r.task_id, t.name AS task_name, r.run_at, r.status, r.session_snapshot_id, r.log_summary, r.created_at
           FROM scheduled_task_runs r
           JOIN scheduled_tasks t ON t.id = r.task_id
           WHERE """ + " AND ".join(conditions) + """
           ORDER BY r.run_at DESC LIMIT ?"""
    )
    rows = await db.execute_fetchall(sql, tuple(params))
    return {"success": True, "runs": [dict(r) for r in rows]}


@router.post("")
async def create_scheduled_task(body: ScheduledTaskCreate, user=Depends(get_current_user)):
    """创建定时任务。根据 cron_expr 计算 next_run_at。"""
    db = await get_db()
    cron = (body.cron_expr or "").strip()
    next_run = _next_run_from_cron(cron) if body.enabled else None
    next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
    en = 1 if body.enabled else 0
    notify_to = (body.notify_email_to or "").strip()
    await db.execute(
        """INSERT INTO scheduled_tasks (user_id, name, content, cron_expr, next_run_at, enabled, notify_email_to, inject_user_skills)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user["id"],
            body.name,
            body.content,
            cron or "",
            next_run_str,
            en,
            notify_to,
            1 if body.inject_user_skills else 0,
        ),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    return {"success": True, "task_id": row[0]}


@router.post("/{task_id}/run-now")
async def run_scheduled_task_now(task_id: int, user=Depends(get_current_user)):
    """立即执行一次定时任务（不等到 cron 时间）。AI 或前端可调用。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, name FROM scheduled_tasks WHERE id = ? AND user_id = ?",
        (task_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="任务不存在")
    await db.execute(
        "INSERT INTO scheduled_task_runs (task_id, run_at, status) VALUES (?, datetime('now', 'localtime'), 'running')",
        (task_id,),
    )
    await db.execute("UPDATE scheduled_tasks SET is_running = 1 WHERE id = ?", (task_id,))
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    run_id = (await cur.fetchone())[0]
    asyncio.create_task(run_scheduled_task(run_id))
    return {"success": True, "run_id": run_id, "message": "已加入执行队列"}


@router.get("/{task_id}/runs")
async def list_runs(
    task_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    user=Depends(get_current_user),
):
    """定时任务执行 history：按 run_at 倒序分页。"""
    db = await get_db()
    cnt_rows = await db.execute_fetchall(
        """SELECT COUNT(*) AS c FROM scheduled_task_runs
           WHERE task_id = ? AND task_id IN (SELECT id FROM scheduled_tasks WHERE user_id = ?)""",
        (task_id, user["id"]),
    )
    total = int(cnt_rows[0]["c"]) if cnt_rows else 0
    offset = (page - 1) * page_size
    rows = await db.execute_fetchall(
        """SELECT id, task_id, run_at, status, session_snapshot_id, log_summary, created_at
           FROM scheduled_task_runs
           WHERE task_id = ? AND task_id IN (SELECT id FROM scheduled_tasks WHERE user_id = ?)
           ORDER BY run_at DESC LIMIT ? OFFSET ?""",
        (task_id, user["id"], page_size, offset),
    )
    return {
        "success": True,
        "runs": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{task_id}/runs/export")
async def export_task_runs(
    task_id: int,
    limit: int = Query(10_000, ge=1, le=50_000),
    user=Depends(get_current_user),
):
    """导出该定时任务全部执行记录（run_at 倒序），供前端生成 CSV。"""
    db = await get_db()
    rows_own = await db.execute_fetchall(
        "SELECT id FROM scheduled_tasks WHERE id = ? AND user_id = ?",
        (task_id, user["id"]),
    )
    if not rows_own:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = await db.execute_fetchall(
        """SELECT id, task_id, run_at, status, session_snapshot_id, log_summary, created_at
           FROM scheduled_task_runs WHERE task_id = ?
           ORDER BY run_at DESC LIMIT ?""",
        (task_id, limit),
    )
    return {"success": True, "runs": [dict(r) for r in rows]}


@router.delete("/{task_id}/runs")
async def clear_task_runs(task_id: int, user=Depends(get_current_user)):
    """清除该定时任务的执行历史与会话消息；不删除 status=running 的记录以免打断当次执行；不删除任务本身。"""
    db = await get_db()
    rows_own = await db.execute_fetchall(
        "SELECT id FROM scheduled_tasks WHERE id = ? AND user_id = ?",
        (task_id, user["id"]),
    )
    if not rows_own:
        raise HTTPException(status_code=404, detail="任务不存在")
    await db.execute(
        """DELETE FROM scheduled_task_run_messages WHERE run_id IN (
               SELECT id FROM scheduled_task_runs WHERE task_id = ? AND COALESCE(status, '') != 'running'
           )""",
        (task_id,),
    )
    await db.execute(
        "DELETE FROM scheduled_task_runs WHERE task_id = ? AND COALESCE(status, '') != 'running'",
        (task_id,),
    )
    await db.commit()
    return {"success": True}


@router.get("/{task_id}/runs/{run_id}/messages")
async def get_run_messages(
    task_id: int,
    run_id: int,
    user=Depends(get_current_user),
):
    """获取定时任务某次执行的会话消息（供前端或 AI 查看）。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM scheduled_task_runs WHERE id = ? AND task_id = ? AND task_id IN (SELECT id FROM scheduled_tasks WHERE user_id = ?)",
        (run_id, task_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    msg_rows = await db.execute_fetchall(
        "SELECT id, role, content, created_at FROM scheduled_task_run_messages WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    )
    return {"success": True, "run_id": run_id, "task_id": task_id, "messages": [dict(r) for r in msg_rows]}


@router.get("/{task_id}")
async def get_scheduled_task(task_id: int, user=Depends(get_current_user)):
    """获取单条定时任务。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
    if not rows:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "task": _scheduled_task_api_dict(dict(rows[0]))}


@router.patch("/{task_id}")
async def update_scheduled_task(task_id: int, body: ScheduledTaskUpdate, user=Depends(get_current_user)):
    """更新定时任务。若更新了 cron_expr 则重新计算 next_run_at。"""
    db = await get_db()
    updates = []
    params = []
    if body.name is not None: updates.append("name = ?"); params.append(body.name)
    if body.content is not None: updates.append("content = ?"); params.append(body.content)
    cron_updated_next = False
    if body.cron_expr is not None:
        updates.append("cron_expr = ?")
        params.append(body.cron_expr)
        next_run = _next_run_from_cron(body.cron_expr)
        next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
        updates.append("next_run_at = ?")
        params.append(next_run_str)
        cron_updated_next = True
    if body.notify_email_to is not None:
        updates.append("notify_email_to = ?")
        params.append((body.notify_email_to or "").strip())
    if body.enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if body.enabled else 0)
        if body.enabled and not cron_updated_next:
            crow = await db.execute_fetchall(
                "SELECT cron_expr FROM scheduled_tasks WHERE id = ? AND user_id = ?",
                (task_id, user["id"]),
            )
            if crow:
                ce = (crow[0]["cron_expr"] or "").strip()
                nr = _next_run_from_cron(ce) if ce else None
                updates.append("next_run_at = ?")
                params.append(nr.strftime("%Y-%m-%d %H:%M:%S") if nr else None)
    if body.inject_user_skills is not None:
        updates.append("inject_user_skills = ?")
        params.append(1 if body.inject_user_skills else 0)
    if not updates:
        return {"success": True}
    params.extend([task_id, user["id"]])
    await db.execute(
        "UPDATE scheduled_tasks SET " + ", ".join(updates) + ", updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        params,
    )
    await db.commit()
    return {"success": True}


@router.delete("/{task_id}")
async def delete_scheduled_task(task_id: int, user=Depends(get_current_user)):
    """删除定时任务及其全部执行历史与会话记录。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])
    )
    if not rows:
        raise HTTPException(status_code=404, detail="任务不存在")
    await db.execute(
        "DELETE FROM scheduled_task_run_messages WHERE run_id IN (SELECT id FROM scheduled_task_runs WHERE task_id = ?)",
        (task_id,),
    )
    await db.execute("DELETE FROM scheduled_task_runs WHERE task_id = ?", (task_id,))
    await db.execute("DELETE FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
    await db.commit()
    return {"success": True}
