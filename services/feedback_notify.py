"""反馈邮件通知：用户提交反馈后，按 settings.notify_admin_on_user_feedback 决定是否通知所有管理员。

设计：
- 沿用 services/email_sender.py 里的系统级 SMTP（管理员配的全局发信）。
- 用户级 SMTP（user_mail_config）不参与，保证「即使提交者没配自己的发信也能通知到管理员」。
- 内置 30 秒节流，避免短时间大量反馈时反复打扰管理员。
- 全部异常吞掉只打 warning，不能因为邮件失败影响主流程。
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

from services.email_sender import send_email_to_address
from services.feedback import (
    get_admin_emails,
    get_notify_admin_on_feedback,
)

logger = logging.getLogger("edgeops.feedback.notify")

# ── 进程内简易节流：避免短时间多条反馈反复发邮件骚扰管理员 ──
# 多 worker 部署时去抖按进程独立；每 30s 内同一 key 最多发一次。
_LAST_NOTIFY_TS: dict[str, float] = {}
_NOTIFY_MIN_INTERVAL_SEC = 30


def _notify_throttled(key: str) -> bool:
    now = time.time()
    last = _LAST_NOTIFY_TS.get(key, 0.0)
    if now - last < _NOTIFY_MIN_INTERVAL_SEC:
        return True
    _LAST_NOTIFY_TS[key] = now
    return False


def _build_subject_body(submitter: str, fb: dict, site_url: str = "") -> tuple[str, str]:
    fb_id = fb.get("id")
    title = (fb.get("title") or "").strip()
    cat = fb.get("category") or "general"
    is_ai = fb.get("is_ai_submitted")
    content = (fb.get("content") or "").strip()
    if len(content) > 1500:
        content = content[:1500] + "\n\n…（已截断，前往 毛竹 反馈后台查看完整内容）"
    subject = f"[毛竹 反馈#{fb_id}] {title or '(无标题)'} — 来自 {submitter}"
    body_lines = [
        f"您收到一条新的用户反馈（编号 #{fb_id}）。",
        "",
        f"  · 提交者: {submitter}",
        f"  · 类别:   {cat}",
        f"  · 标题:   {title or '(无)'}",
        f"  · 来源:   {'AI 代提交' if is_ai else '用户直接提交'}",
        "",
        "── 反馈正文（Markdown）" + " " * 20,
        content,
        "",
        "──",
        "请登录 毛竹 「反馈」菜单查看与回复。",
    ]
    if site_url:
        body_lines.append(f"后台直达: {site_url.rstrip('/')}/feedback")
    body_lines.append(
        "可在「系统设置 → 全局设置」关闭 notify_admin_on_user_feedback 停止此类通知。"
    )
    return subject, "\n".join(body_lines)


async def _get_site_url(db: aiosqlite.Connection) -> str:
    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key='site_url'")
    return (rows[0]["value"] or "").strip() if rows else ""


async def maybe_notify_admins_on_new_feedback(
    db: aiosqlite.Connection, *, feedback: dict, submitter_username: str
) -> None:
    """新反馈到达后调用一次。所有配置 / SMTP / 节流不通过都静默返回。"""
    try:
        if not await get_notify_admin_on_feedback(db):
            return
        if _notify_throttled("user_feedback_new"):
            logger.info("反馈邮件通知被节流（30s 内已发过一次），跳过。")
            return
        admins = await get_admin_emails(db)
        if not admins:
            logger.info("无管理员邮箱可通知。")
            return
        site_url = await _get_site_url(db)
        subject, body = _build_subject_body(submitter_username or "(未知用户)", feedback, site_url)
        # 顺序串行发送，单个失败不阻断其它
        sent = 0
        for to in admins:
            try:
                ok = await send_email_to_address(db, to, subject, body)
                if ok:
                    sent += 1
            except Exception as e:
                logger.warning("通知管理员 %s 失败: %s", to, e)
        logger.info("反馈通知邮件：成功 %d / %d", sent, len(admins))
    except Exception as e:
        logger.warning("反馈通知整体失败（忽略）: %s", e)


def schedule_notify_admins_on_new_feedback(
    db: aiosqlite.Connection, *, feedback: dict, submitter_username: str
) -> None:
    """以 fire-and-forget 方式发送通知，不阻塞 HTTP 响应。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            maybe_notify_admins_on_new_feedback(
                db, feedback=feedback, submitter_username=submitter_username
            )
        )
    except RuntimeError:
        # 没有运行中的 loop 时（理论不应发生），直接放弃通知
        logger.warning("无运行中的事件循环，反馈通知未发送。")
