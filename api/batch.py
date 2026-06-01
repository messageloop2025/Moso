"""批量操作 API（参考 IOTHub）：向多台主机下发命令/脚本/上传/重启等"""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List

from database import get_db
from api.auth import get_current_user, _is_admin_role

router = APIRouter(prefix="/api/batch", tags=["批量操作"])


class BatchRequest(BaseModel):
    operation_type: str   # run_command / scp_push / run_script / restart
    scope_type: str       # all / group / selected / tag
    scope_value: List[int] = Field(default_factory=list)  # 主机 ID / 分组 ID / 标签 ID 列表
    params: dict = Field(default_factory=dict)    # 操作参数（command, remote_path, content, script_path 等）
    tag_match_mode: str = "any"  # 仅 scope_type=tag 生效：any / all


async def _create_batch_and_start(
    operation_type: str,
    scope_type: str,
    scope_value: list,
    params: dict,
    created_by: int,
    tag_match_mode: str = "any",
    is_admin: bool = False,
) -> int:
    """创建批量任务并启动执行，返回 batch_id。供 API 与 AI skill 共用。"""
    db = await get_db()
    safe_params = dict(params or {})
    if scope_type == "all":
        if is_admin:
            rows = await db.execute_fetchall("SELECT id FROM hosts")
        else:
            rows = await db.execute_fetchall(
                """SELECT DISTINCT h.id
                   FROM hosts h
                   LEFT JOIN host_shares hs
                     ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                   WHERE h.created_by = ? OR hs.id IS NOT NULL""",
                (created_by, created_by),
            )
    elif scope_type == "group":
        if not scope_value:
            raise ValueError("请指定分组 ID 列表")
        placeholders = ",".join(["?"] * len(scope_value))
        if is_admin:
            rows = await db.execute_fetchall(
                f"""SELECT DISTINCT hgm.host_id as id
                    FROM host_group_members hgm
                    JOIN host_groups hg ON hg.id = hgm.group_id
                    WHERE hgm.group_id IN ({placeholders})""",
                list(scope_value),
            )
        else:
            rows = await db.execute_fetchall(
                f"""SELECT DISTINCT hgm.host_id as id
                    FROM host_group_members hgm
                    JOIN host_groups hg ON hg.id = hgm.group_id AND hg.created_by = ?
                    JOIN hosts h ON h.id = hgm.host_id
                    LEFT JOIN host_shares hs
                      ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                    WHERE hgm.group_id IN ({placeholders})
                      AND (h.created_by = ? OR hs.id IS NOT NULL)""",
                [created_by, created_by, *list(scope_value), created_by],
            )
    elif scope_type == "selected":
        if not scope_value:
            raise ValueError("请选择主机 ID 列表")
        placeholders = ",".join(["?"] * len(scope_value))
        if is_admin:
            rows = await db.execute_fetchall(
                f"SELECT DISTINCT h.id FROM hosts h WHERE h.id IN ({placeholders})",
                list(scope_value),
            )
        else:
            rows = await db.execute_fetchall(
                f"""SELECT DISTINCT h.id
                    FROM hosts h
                    LEFT JOIN host_shares hs
                      ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                    WHERE h.id IN ({placeholders}) AND (h.created_by = ? OR hs.id IS NOT NULL)""",
                [created_by] + list(scope_value) + [created_by],
            )
    elif scope_type == "tag":
        if not scope_value:
            raise ValueError("请指定标签 ID 列表")
        tag_match_mode = (tag_match_mode or "any").strip().lower() or "any"
        if tag_match_mode not in ("any", "all"):
            raise ValueError("tag_match_mode 须为 any / all")
        safe_params["tag_match_mode"] = tag_match_mode
        placeholders = ",".join(["?"] * len(scope_value))
        if tag_match_mode == "all":
            rows = await db.execute_fetchall(
                f"""SELECT h.id
                    FROM hosts h
                    JOIN host_user_tags hut
                      ON hut.host_id = h.id AND hut.user_id = ? AND hut.tag_id IN ({placeholders})
                    LEFT JOIN host_shares hs
                      ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                    WHERE h.created_by = ? OR hs.id IS NOT NULL
                    GROUP BY h.id
                    HAVING COUNT(DISTINCT hut.tag_id) = ?""",
                [created_by, *list(scope_value), created_by, created_by, len(scope_value)],
            )
        else:
            rows = await db.execute_fetchall(
                f"""SELECT DISTINCT h.id
                    FROM hosts h
                    JOIN host_user_tags hut
                      ON hut.host_id = h.id AND hut.user_id = ? AND hut.tag_id IN ({placeholders})
                    LEFT JOIN host_shares hs
                      ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                    WHERE h.created_by = ? OR hs.id IS NOT NULL""",
                [created_by, *list(scope_value), created_by, created_by],
            )
    else:
        raise ValueError("scope_type 须为 all / group / selected / tag")
    host_ids = [dict(r)["id"] for r in rows]
    if not host_ids:
        raise ValueError("没有可操作的主机")

    cursor = await db.execute(
        """INSERT INTO batch_operations (operation_type, scope_type, scope_value, total_count, pending_count, params, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (operation_type, scope_type, json.dumps(scope_value),
         len(host_ids), len(host_ids), json.dumps(safe_params), created_by),
    )
    batch_id = cursor.lastrowid
    for hid in host_ids:
        await db.execute(
            "INSERT INTO batch_operation_details (batch_id, host_id, status) VALUES (?, ?, 'pending')",
            (batch_id, hid),
        )
    await db.commit()

    import asyncio
    from services.batch_executor import execute_batch
    asyncio.create_task(execute_batch(batch_id))
    return batch_id


@router.post("")
async def create_batch(req: BatchRequest, user=Depends(get_current_user)):
    """创建批量操作并异步执行"""
    if req.operation_type not in ("run_command", "scp_push", "run_script", "restart"):
        raise HTTPException(status_code=400, detail="operation_type 须为 run_command / scp_push / run_script / restart")
    try:
        batch_id = await _create_batch_and_start(
            req.operation_type, req.scope_type, req.scope_value, req.params, user["id"], req.tag_match_mode, _is_admin_role(user.get("role"))
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = await get_db()
    cur = await db.execute("SELECT total_count FROM batch_operations WHERE id = ?", (batch_id,))
    total = (await cur.fetchone())[0]
    return {"success": True, "batch_id": batch_id, "total": total}


def _check_page_size(page_size: int) -> int:
    if page_size not in (20, 50, 100):
        return 20
    return page_size


@router.get("")
async def list_batches(
    page: int = 1,
    page_size: int = 20,
    operation_type: Optional[str] = None,
    status: Optional[str] = None,
    user=Depends(get_current_user),
):
    page_size = _check_page_size(page_size)
    page = max(1, page)
    offset = (page - 1) * page_size
    db = await get_db()
    base = "FROM batch_operations b LEFT JOIN users u ON b.created_by = u.id WHERE 1=1"
    params = []
    if not _is_admin_role(user.get("role")):
        base += " AND b.created_by = ?"
        params.append(user["id"])
    if operation_type:
        base += " AND b.operation_type = ?"
        params.append(operation_type)
    if status:
        base += " AND b.status = ?"
        params.append(status)
    count_rows = await db.execute_fetchall("SELECT COUNT(*) as n " + base, params)
    total = count_rows[0][0] if count_rows else 0
    sel = "SELECT b.*, u.username as created_by_name " + base + " ORDER BY b.created_at DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])
    rows = await db.execute_fetchall(sel, params)
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("scope_value"), str):
            try:
                d["scope_value"] = json.loads(d["scope_value"] or "[]")
            except Exception:
                d["scope_value"] = []
        if isinstance(d.get("params"), str):
            try:
                d["params"] = json.loads(d["params"] or "{}")
            except Exception:
                d["params"] = {}
        if d.get("scope_type") == "tag":
            mode = str((d.get("params") or {}).get("tag_match_mode") or "any").strip().lower() or "any"
            d["tag_match_mode"] = "all" if mode == "all" else "any"
            d["scope_label"] = "标签(" + ("AND" if d["tag_match_mode"] == "all" else "OR") + ")"
        else:
            d["tag_match_mode"] = ""
            d["scope_label"] = d.get("scope_type") or ""
        d["created_by_name"] = d.get("created_by_name") or ""
        out.append(d)
    return {"success": True, "batches": out, "total": total, "page": page, "page_size": page_size}


def _can_access_batch(batch_row: dict, user: dict) -> bool:
    return _is_admin_role(user.get("role")) or (batch_row.get("created_by") == user["id"])


@router.delete("/clear")
async def clear_batches(user=Depends(get_current_user)):
    """清空批量任务：普通用户仅清空自己的；管理员清空全部。"""
    db = await get_db()
    if _is_admin_role(user.get("role")):
        await db.execute("DELETE FROM batch_operation_details")
        await db.execute("DELETE FROM batch_operations")
    else:
        await db.execute("DELETE FROM batch_operation_details WHERE batch_id IN (SELECT id FROM batch_operations WHERE created_by = ?)", (user["id"],))
        await db.execute("DELETE FROM batch_operations WHERE created_by = ?", (user["id"],))
    await db.commit()
    return {"success": True, "message": "已清空批量任务"}


@router.get("/export")
async def export_batches(
    limit: int = 5000,
    user=Depends(get_current_user),
):
    """导出批量任务列表为 JSON，供前端生成 CSV 下载。limit 最大 10000。"""
    limit = max(1, min(10000, limit))
    db = await get_db()
    base = "FROM batch_operations b LEFT JOIN users u ON b.created_by = u.id WHERE 1=1"
    params = []
    if not _is_admin_role(user.get("role")):
        base += " AND b.created_by = ?"
        params.append(user["id"])
    params.append(limit)
    rows = await db.execute_fetchall(
        "SELECT b.*, u.username as created_by_name " + base + " ORDER BY b.created_at DESC LIMIT ?",
        params,
    )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("scope_value"), str):
            try:
                d["scope_value"] = json.loads(d["scope_value"] or "[]")
            except Exception:
                d["scope_value"] = []
        if isinstance(d.get("params"), str):
            try:
                d["params"] = json.loads(d["params"] or "{}")
            except Exception:
                d["params"] = {}
        if d.get("scope_type") == "tag":
            mode = str((d.get("params") or {}).get("tag_match_mode") or "any").strip().lower() or "any"
            d["tag_match_mode"] = "all" if mode == "all" else "any"
            d["scope_label"] = "标签(" + ("AND" if d["tag_match_mode"] == "all" else "OR") + ")"
        else:
            d["tag_match_mode"] = ""
            d["scope_label"] = d.get("scope_type") or ""
        d["created_by_name"] = d.get("created_by_name") or ""
        out.append(d)
    return {"success": True, "batches": out}


@router.get("/{batch_id}")
async def get_batch(batch_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM batch_operations WHERE id = ?", (batch_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="批量操作不存在")
    batch = dict(rows[0])
    if not _can_access_batch(batch, user):
        raise HTTPException(status_code=404, detail="批量操作不存在")
    if isinstance(batch.get("scope_value"), str):
        try:
            batch["scope_value"] = json.loads(batch["scope_value"] or "[]")
        except Exception:
            batch["scope_value"] = []
    if isinstance(batch.get("params"), str):
        try:
            batch["params"] = json.loads(batch["params"] or "{}")
        except Exception:
            batch["params"] = {}
    if batch.get("scope_type") == "tag":
        mode = str((batch.get("params") or {}).get("tag_match_mode") or "any").strip().lower() or "any"
        batch["tag_match_mode"] = "all" if mode == "all" else "any"
        batch["scope_label"] = "标签(" + ("AND" if batch["tag_match_mode"] == "all" else "OR") + ")"
    else:
        batch["tag_match_mode"] = ""
        batch["scope_label"] = batch.get("scope_type") or ""

    details = await db.execute_fetchall("""
        SELECT bd.*, h.name as host_name, h.host, h.port
        FROM batch_operation_details bd
        JOIN hosts h ON h.id = bd.host_id
        WHERE bd.batch_id = ? ORDER BY bd.id
    """, (batch_id,))
    batch["details"] = [dict(r) for r in details]
    return {"success": True, "batch": batch}


async def _cancel_batch(batch_id: int) -> None:
    """取消正在运行的批量任务（供 API 与 AI skill 共用）。"""
    db = await get_db()
    await db.execute(
        "UPDATE batch_operations SET status='cancelled', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
        (batch_id,),
    )
    await db.execute(
        "UPDATE batch_operation_details SET status='skipped' WHERE batch_id=? AND status='pending'",
        (batch_id,),
    )
    await db.commit()


async def _retry_batch(batch_id: int) -> None:
    """将批量任务中失败项重置为 pending 并重新执行（供 API 与 AI skill 共用）。"""
    db = await get_db()
    await db.execute(
        "UPDATE batch_operation_details SET status='pending', result=NULL, started_at=NULL, completed_at=NULL WHERE batch_id=? AND status='failed'",
        (batch_id,),
    )
    cur = await db.execute(
        "SELECT COUNT(*) FROM batch_operation_details WHERE batch_id=? AND status='pending'",
        (batch_id,),
    )
    n = (await cur.fetchone())[0]
    await db.execute(
        "UPDATE batch_operations SET status='running', pending_count=?, completed_at=NULL WHERE id=?",
        (n, batch_id),
    )
    await db.commit()
    import asyncio
    from services.batch_executor import execute_batch
    asyncio.create_task(execute_batch(batch_id))


@router.post("/{batch_id}/cancel")
async def cancel_batch(batch_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM batch_operations WHERE id = ?", (batch_id,))
    if not rows or not _can_access_batch(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="批量操作不存在")
    await _cancel_batch(batch_id)
    return {"success": True}


@router.post("/{batch_id}/retry")
async def retry_failed(batch_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM batch_operations WHERE id = ?", (batch_id,))
    if not rows or not _can_access_batch(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="批量操作不存在")
    await _retry_batch(batch_id)
    return {"success": True}
