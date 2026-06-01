"""定时任务调度器：按 cron 表达式计算下次运行时间并到点执行"""
import asyncio
import logging
import re
from datetime import datetime

from croniter import croniter

from database import get_db
from services.task_runner import run_scheduled_task

logger = logging.getLogger("edgeops.scheduler")

CHECK_INTERVAL_SEC = 30


def _next_run_from_cron(cron_expr: str, from_time: datetime | None = None) -> datetime | None:
    """根据 cron 表达式计算下次执行时间（严格晚于 from_time 的下一拍）。"""
    if not (cron_expr or "").strip():
        return None
    try:
        base = from_time or datetime.now()
        if base.tzinfo is not None:
            base = base.replace(tzinfo=None)
        it = croniter(cron_expr.strip(), base)
        return it.get_next(datetime)
    except Exception as e:
        logger.warning("Cron parse error %r: %s", cron_expr, e)
        return None


def _parse_next_run_at(val) -> datetime | None:
    """将库中的 next_run_at 解析为本地 naive datetime，供与 now 比较。"""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    s = s.replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1].strip()
    s = re.sub(r"\.\d+$", "", s)
    if len(s) >= 19:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if len(s) >= 16:
        try:
            return datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    if len(s) >= 10:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            pass
    return None


def _is_task_due(next_run_at_raw, now: datetime) -> bool:
    """是否已到点：无下次时间视为立即需要排一次 next；否则本地时间比较。"""
    dt = _parse_next_run_at(next_run_at_raw)
    if dt is None:
        return True
    return dt <= now


async def _tick() -> None:
    """检查是否有定时任务到点需要执行。

    每个任务在 BEGIN IMMEDIATE 事务内：先抢占 is_running（仅当仍为 0），再插入 run 并更新
    next_run_at，一次提交。避免在 await 间隙被并发 tick 重复调度同一任务。
    """
    db = await get_db()
    now = datetime.now()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    rows = await db.execute_fetchall(
        """SELECT id FROM scheduled_tasks
           WHERE TRIM(IFNULL(cron_expr, '')) != ''
           AND (is_running = 0 OR is_running IS NULL)
           AND (enabled IS NULL OR enabled != 0)
           ORDER BY id""",
    )
    for r in rows:
        task_id = r["id"]
        try:
            await db.execute("BEGIN IMMEDIATE")
            chk = await db.execute_fetchall(
                """SELECT next_run_at, cron_expr FROM scheduled_tasks
                   WHERE id = ? AND (is_running = 0 OR is_running IS NULL)
                   AND TRIM(IFNULL(cron_expr, '')) != ''
                   AND (enabled IS NULL OR enabled != 0)""",
                (task_id,),
            )
            if not chk:
                await db.rollback()
                continue
            row = chk[0]
            if not _is_task_due(row["next_run_at"], now):
                await db.rollback()
                continue
            await db.execute(
                "UPDATE scheduled_tasks SET is_running = 1 WHERE id = ? AND (is_running = 0 OR is_running IS NULL)",
                (task_id,),
            )
            chg = await db.execute_fetchall("SELECT changes()")
            if not chg or int(chg[0][0] or 0) == 0:
                await db.rollback()
                continue
            cron_expr = (row["cron_expr"] or "").strip()
            next_run = _next_run_from_cron(cron_expr, now)
            await db.execute(
                "INSERT INTO scheduled_task_runs (task_id, run_at, status) VALUES (?, datetime('now', 'localtime'), 'running')",
                (task_id,),
            )
            cur = await db.execute("SELECT last_insert_rowid()")
            run_id = (await cur.fetchone())[0]
            if next_run:
                await db.execute(
                    "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
                    (next_run.strftime("%Y-%m-%d %H:%M:%S"), task_id),
                )
            await db.commit()
        except Exception as e:
            logger.exception("Scheduled task create run failed: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            continue
        asyncio.create_task(run_scheduled_task(run_id))


async def scheduler_loop() -> None:
    """后台循环：每 CHECK_INTERVAL_SEC 秒检查一次定时任务。"""
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.exception("Scheduler tick error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SEC)
