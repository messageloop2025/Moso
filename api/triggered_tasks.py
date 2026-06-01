"""触发任务 API — 任务列表、CRUD、触发接口、执行历史

设计见 docs/SSH通道与后台任务设计.md。触发时异步执行 run_triggered_task。
"""
import asyncio
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user

router = APIRouter(prefix="/api/triggered-tasks", tags=["触发任务"])


class TriggeredTaskCreate(BaseModel):
    name: str
    content: str
    intro: str = ""
    trigger_conditions: Optional[str] = None


class TriggeredTaskUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    intro: Optional[str] = None
    trigger_conditions: Optional[str] = None


class TriggerRequest(BaseModel):
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    instruction: str = ""
    caller_task_id: Optional[str] = None
    caller_task_name: Optional[str] = None
    caller_status: Optional[str] = None


class ExposeAdd(BaseModel):
    expose_code: str
    description: str = ""


@router.get("")
async def list_triggered_tasks(user=Depends(get_current_user)):
    """列出当前用户的触发任务。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, name, content, intro, trigger_conditions, created_at, updated_at, last_run_at, last_run_status, is_running
           FROM triggered_tasks WHERE user_id = ? ORDER BY updated_at DESC""",
        (user["id"],),
    )
    return {"success": True, "tasks": [dict(r) for r in rows]}


@router.post("")
async def create_triggered_task(body: TriggeredTaskCreate, user=Depends(get_current_user)):
    """创建触发任务。"""
    db = await get_db()
    await db.execute(
        """INSERT INTO triggered_tasks (user_id, name, content, intro, trigger_conditions)
           VALUES (?, ?, ?, ?, ?)""",
        (user["id"], body.name, body.content, body.intro or "", body.trigger_conditions or ""),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    return {"success": True, "task_id": row[0]}


@router.get("/all-runs")
async def list_all_runs(
    task_id: Optional[int] = Query(None, description="按任务 ID 筛选"),
    task_name: Optional[str] = Query(None, description="按任务名筛选（模糊）"),
    status: Optional[str] = Query(None, description="按状态筛选：pending/running/completed/failed"),
    from_time: Optional[str] = Query(None, description="执行时间起，ISO 或 YYYY-MM-DD"),
    to_time: Optional[str] = Query(None, description="执行时间止"),
    limit: int = Query(100, ge=1, le=500),
    user=Depends(get_current_user),
):
    """全局执行历史：按任务 ID、任务名、状态、执行时间筛选。"""
    db = await get_db()
    conditions = ["r.task_id IN (SELECT id FROM triggered_tasks WHERE user_id = ?)"]
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
        conditions.append("r.triggered_at >= ?")
        params.append(from_time.strip())
    if to_time and to_time.strip():
        conditions.append("r.triggered_at <= ?")
        params.append(to_time.strip())
    params.append(limit)
    sql = (
        """SELECT r.id, r.task_id, t.name AS task_name, r.triggered_at, r.triggered_by_type, r.triggered_by_id,
                  r.caller_task_name, r.status, r.instruction, r.log_summary, r.created_at
           FROM triggered_task_runs r
           JOIN triggered_tasks t ON t.id = r.task_id
           WHERE """ + " AND ".join(conditions) + """
           ORDER BY r.triggered_at DESC LIMIT ?"""
    )
    rows = await db.execute_fetchall(sql, tuple(params))
    return {"success": True, "runs": [dict(r) for r in rows]}


@router.get("/exposed")
async def list_exposed(user=Depends(get_current_user)):
    """列出本用户可被定时任务发现的触发任务（名称、介绍、接口 code）。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.id, t.name, t.intro, e.expose_code, e.description
           FROM triggered_tasks t
           LEFT JOIN triggered_task_expose e ON e.task_id = t.id
           WHERE t.user_id = ? ORDER BY t.name""",
        (user["id"],),
    )
    by_task = {}
    for r in rows:
        r = dict(r)
        tid = r["id"]
        if tid not in by_task:
            by_task[tid] = {"id": r["id"], "name": r["name"], "intro": r.get("intro") or "", "expose": []}
        if r.get("expose_code"):
            by_task[tid]["expose"].append({"code": r["expose_code"], "description": r.get("description") or ""})
    return {"success": True, "tasks": list(by_task.values())}


@router.post("/trigger")
async def trigger_task(body: TriggerRequest, user=Depends(get_current_user)):
    """执行触发（由定时任务或外部调用）。同用户才能触发。"""
    db = await get_db()
    if body.task_id:
        rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?", (body.task_id, user["id"]))
    elif body.task_name:
        rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE name = ? AND user_id = ?", (body.task_name, user["id"]))
    else:
        raise HTTPException(status_code=400, detail="请提供 task_id 或 task_name")
    if not rows:
        raise HTTPException(status_code=404, detail="触发任务不存在")
    task_id = rows[0]["id"]
    await db.execute(
        """INSERT INTO triggered_task_runs (task_id, triggered_by_type, triggered_by_id, caller_task_name, status, instruction)
           VALUES (?, 'api', ?, ?, 'pending', ?)""",
        (task_id, body.caller_task_id or "", body.caller_task_name or "", body.instruction or ""),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    run_id = (await cur.fetchone())[0]
    await db.execute(
        "UPDATE triggered_tasks SET last_run_at = CURRENT_TIMESTAMP, last_run_status = 'pending', is_running = 1 WHERE id = ?",
        (task_id,),
    )
    await db.commit()
    from services.task_runner import run_triggered_task
    asyncio.create_task(run_triggered_task(run_id))
    return {"success": True, "run_id": run_id, "message": "已加入执行队列"}


@router.post("/{task_id}/expose")
async def add_expose(task_id: int, body: ExposeAdd, user=Depends(get_current_user)):
    """为触发任务添加一个暴露接口（供定时任务发现）。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
    if not rows:
        raise HTTPException(status_code=404, detail="任务不存在")
    code = (body.expose_code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="expose_code 不能为空")
    try:
        await db.execute(
            "INSERT INTO triggered_task_expose (task_id, expose_code, description) VALUES (?, ?, ?)",
            (task_id, code, body.description or ""),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="该接口 code 已存在")
    return {"success": True, "message": "已添加"}


@router.delete("/{task_id}/expose")
async def remove_expose(
    task_id: int,
    code: str = Query(..., description="expose_code"),
    user=Depends(get_current_user),
):
    """移除触发任务的一个暴露接口。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
    if not rows:
        raise HTTPException(status_code=404, detail="任务不存在")
    await db.execute("DELETE FROM triggered_task_expose WHERE task_id = ? AND expose_code = ?", (task_id, code))
    await db.commit()
    return {"success": True, "message": "已移除"}


@router.get("/{task_id}/runs")
async def list_runs(
    task_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    user=Depends(get_current_user),
):
    """触发任务执行历史：按 triggered_at 倒序分页。"""
    db = await get_db()
    cnt_rows = await db.execute_fetchall(
        """SELECT COUNT(*) AS c FROM triggered_task_runs
           WHERE task_id = ? AND task_id IN (SELECT id FROM triggered_tasks WHERE user_id = ?)""",
        (task_id, user["id"]),
    )
    total = int(cnt_rows[0]["c"]) if cnt_rows else 0
    offset = (page - 1) * page_size
    rows = await db.execute_fetchall(
        """SELECT id, task_id, triggered_at, triggered_by_type, triggered_by_id, caller_task_name, status, instruction, log_summary, created_at
           FROM triggered_task_runs WHERE task_id = ? AND task_id IN (SELECT id FROM triggered_tasks WHERE user_id = ?)
           ORDER BY triggered_at DESC LIMIT ? OFFSET ?""",
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
    """导出该触发任务全部执行记录（triggered_at 倒序），供前端生成 CSV。"""
    db = await get_db()
    rows_own = await db.execute_fetchall(
        "SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?",
        (task_id, user["id"]),
    )
    if not rows_own:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = await db.execute_fetchall(
        """SELECT id, task_id, triggered_at, triggered_by_type, triggered_by_id, caller_task_name,
                  status, instruction, log_summary, created_at
           FROM triggered_task_runs WHERE task_id = ?
           ORDER BY triggered_at DESC LIMIT ?""",
        (task_id, limit),
    )
    return {"success": True, "runs": [dict(r) for r in rows]}


@router.delete("/{task_id}/runs")
async def clear_task_runs(task_id: int, user=Depends(get_current_user)):
    """清除该触发任务的执行历史与会话消息。
    仅删除已结束记录（status 为 completed / failed）；pending 等未结束记录保留，避免打断当次执行。
    不删除触发任务本身。
    """
    db = await get_db()
    rows_own = await db.execute_fetchall(
        "SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?",
        (task_id, user["id"]),
    )
    if not rows_own:
        raise HTTPException(status_code=404, detail="任务不存在")
    await db.execute(
        """DELETE FROM triggered_task_run_messages WHERE run_id IN (
               SELECT id FROM triggered_task_runs WHERE task_id = ? AND status IN ('completed', 'failed')
           )""",
        (task_id,),
    )
    await db.execute(
        "DELETE FROM triggered_task_runs WHERE task_id = ? AND status IN ('completed', 'failed')",
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
    """获取触发任务某次执行的会话消息（供前端或 AI 查看）。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM triggered_task_runs WHERE id = ? AND task_id = ? AND task_id IN (SELECT id FROM triggered_tasks WHERE user_id = ?)",
        (run_id, task_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    msg_rows = await db.execute_fetchall(
        "SELECT id, role, content, created_at FROM triggered_task_run_messages WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    )
    return {"success": True, "run_id": run_id, "task_id": task_id, "messages": [dict(r) for r in msg_rows]}


@router.get("/{task_id}")
async def get_triggered_task(task_id: int, user=Depends(get_current_user)):
    """获取单条触发任务。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
    if not rows:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "task": dict(rows[0])}


@router.patch("/{task_id}")
async def update_triggered_task(task_id: int, body: TriggeredTaskUpdate, user=Depends(get_current_user)):
    """更新触发任务。"""
    db = await get_db()
    updates = []
    params = []
    if body.name is not None: updates.append("name = ?"); params.append(body.name)
    if body.content is not None: updates.append("content = ?"); params.append(body.content)
    if body.intro is not None: updates.append("intro = ?"); params.append(body.intro)
    if body.trigger_conditions is not None: updates.append("trigger_conditions = ?"); params.append(body.trigger_conditions)
    if not updates:
        return {"success": True}
    params.extend([task_id, user["id"]])
    await db.execute(
        "UPDATE triggered_tasks SET " + ", ".join(updates) + ", updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        params,
    )
    await db.commit()
    return {"success": True}


@router.delete("/{task_id}")
async def delete_triggered_task(task_id: int, user=Depends(get_current_user)):
    """删除触发任务。"""
    db = await get_db()
    await db.execute("DELETE FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
    await db.commit()
    return {"success": True}
