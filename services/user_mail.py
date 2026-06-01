"""每用户 SMTP 配置：发信能力与校验（与管理员全局 settings SMTP 独立）。"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
from pathlib import Path

from services.email_sender import send_email_sync

logger = logging.getLogger("edgeops.user_mail")

# 供 AI 与 API 错误提示引用（与前端路由一致）
USER_MAIL_SETUP_HINT_ZH = (
    "请到 毛竹「系统设置」页面，在「我的发信设置」中填写 SMTP 服务器、端口、用户名、密码、发件人地址，"
    "正确选择 TLS/SSL 方式，勾选「启用邮件发送」并保存。"
    "启用前必须各项均已填写完整。"
)


def _parse_port(raw) -> int:
    try:
        p = int(str(raw or "587").strip())
        return p if 1 <= p <= 65535 else 587
    except (TypeError, ValueError):
        return 587


def normalize_user_mail_row(row: dict | None) -> dict:
    """将表行转为逻辑字段（password 仍原文，仅服务层使用）。"""
    if not row:
        return {
            "mail_enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "",
            "smtp_use_tls": True,
            "smtp_use_ssl": False,
        }
    d = dict(row)
    enabled = (d.get("mail_enabled") or "false").strip().lower() == "true"
    return {
        "mail_enabled": enabled,
        "smtp_host": (d.get("smtp_host") or "").strip(),
        "smtp_port": _parse_port(d.get("smtp_port")),
        "smtp_user": (d.get("smtp_user") or "").strip(),
        "smtp_password": (d.get("smtp_password") or "").strip(),
        "smtp_from": (d.get("smtp_from") or "").strip(),
        "smtp_use_tls": (d.get("smtp_use_tls") or "true").strip().lower() != "false",
        "smtp_use_ssl": (d.get("smtp_use_ssl") or "").strip().lower() == "true",
    }


def smtp_settings_complete(cfg: dict) -> bool:
    """启用发信所需的最低完整度：主机、发件人、认证账号与密码、合法端口。"""
    if not (cfg.get("smtp_host") or "").strip():
        return False
    if not (cfg.get("smtp_from") or "").strip():
        return False
    if not (cfg.get("smtp_user") or "").strip():
        return False
    if not (cfg.get("smtp_password") or "").strip():
        return False
    try:
        p = int(cfg.get("smtp_port") or 0)
        if not (1 <= p <= 65535):
            return False
    except (TypeError, ValueError):
        return False
    return True


def user_may_send_mail(cfg: dict) -> bool:
    return bool(cfg.get("mail_enabled")) and smtp_settings_complete(cfg)


async def load_user_mail_config(db, user_id: int) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM user_mail_config WHERE user_id = ?", (user_id,))
    return normalize_user_mail_row(dict(rows[0]) if rows else None)


def public_mail_config_for_api(cfg: dict, password_is_set: bool) -> dict:
    """返回给前端的配置（密码永不返回明文）。"""
    return {
        "mail_enabled": cfg["mail_enabled"],
        "smtp_host": cfg["smtp_host"],
        "smtp_port": cfg["smtp_port"],
        "smtp_user": cfg["smtp_user"],
        "smtp_from": cfg["smtp_from"],
        "smtp_use_tls": cfg["smtp_use_tls"],
        "smtp_use_ssl": cfg["smtp_use_ssl"],
        "smtp_password_set": password_is_set,
        "smtp_config_complete": smtp_settings_complete(cfg),
        "may_send_mail": user_may_send_mail(cfg),
    }


async def send_mail_as_user(
    db,
    user_id: int,
    to_addrs: list[str],
    subject: str,
    body: str,
    *,
    body_html: str | None = None,
    attachments: list[dict] | None = None,
) -> tuple[bool, str]:
    """
    使用当前用户的 SMTP 配置发信（plain 默认；可选 HTML 与附件）。
    返回 (成功, 失败时的简短原因或空字符串)。
    """
    to_addrs = [x.strip() for x in to_addrs if (x or "").strip()]
    if not to_addrs:
        return False, "收件人为空"
    if not (body or "").strip() and not (body_html or "").strip():
        return False, "正文为空：请提供 body（纯文本）和/或 body_html（HTML）"
    cfg = await load_user_mail_config(db, user_id)
    if not user_may_send_mail(cfg):
        return False, "邮件功能未开启或未配置完整 SMTP。" + USER_MAIL_SETUP_HINT_ZH
    ok = send_email_sync(
        host=cfg["smtp_host"],
        port=cfg["smtp_port"],
        user=cfg["smtp_user"],
        password=cfg["smtp_password"],
        from_addr=cfg["smtp_from"] or cfg["smtp_user"],
        to_addrs=to_addrs,
        subject=(subject or "").strip() or "(无主题)",
        body_text=body or "",
        body_html=body_html,
        attachments=attachments,
        use_tls=cfg["smtp_use_tls"],
        use_ssl=cfg["smtp_use_ssl"],
    )
    if ok:
        return True, ""
    return False, "SMTP 发送失败（请检查服务器地址、端口、账号密码及 TLS/SSL 是否与邮箱服务商要求一致）。"


def _safe_attachment_filename(name: str) -> str:
    base = Path((name or "").replace("\\", "/")).name.strip()
    base = re.sub(r'[^\w.\- \u4e00-\u9fff()（）\[\]]+', "_", base)
    return base or "attachment.bin"


async def resolve_user_mail_attachments(user: dict, attachments_arg) -> tuple[list[dict], str | None]:
    """解析 send_email 的 attachments 参数为 [{filename, data, mime_type?}, ...]。"""
    if not attachments_arg:
        return [], None
    if not isinstance(attachments_arg, list):
        return [], "attachments 必须是数组"
    try:
        from config import (
            USER_SEND_EMAIL_ATTACHMENT_MAX_BYTES as max_file,
            USER_SEND_EMAIL_ATTACHMENT_MAX_FILES as max_files,
            USER_SEND_EMAIL_ATTACHMENT_MAX_TOTAL_BYTES as max_total,
        )
    except Exception:
        max_file = 25 * 1024 * 1024
        max_total = 50 * 1024 * 1024
        max_files = 10
    max_files = max(1, int(max_files or 10))
    max_file = int(max_file or 0)
    max_total = int(max_total or 0)

    if len(attachments_arg) > max_files:
        return [], f"附件数量超过上限 {max_files}"

    from api.filesystem import get_user_fs_root, resolve_fs_path

    out: list[dict] = []
    total = 0
    base = get_user_fs_root(user)

    for idx, raw in enumerate(attachments_arg):
        if not isinstance(raw, dict):
            return [], f"attachments[{idx}] 必须是对象"
        filename = _safe_attachment_filename(raw.get("filename") or "")
        local_path = (raw.get("local_path") or "").strip().replace("\\", "/").lstrip("/")
        content = raw.get("content")
        encoding = (raw.get("encoding") or "utf-8").strip().lower()

        data: bytes | None = None
        if local_path:
            try:
                path_obj = resolve_fs_path(local_path, base)
            except Exception:
                return [], f"attachments[{idx}] local_path 无效: {local_path}"
            if not path_obj.is_file():
                return [], f"attachments[{idx}] 文件不存在: {local_path}"
            try:
                data = await asyncio.to_thread(path_obj.read_bytes)
            except OSError as exc:
                return [], f"attachments[{idx}] 读取失败: {exc}"
            if not filename or filename == "attachment.bin":
                filename = _safe_attachment_filename(path_obj.name)
        elif content is not None:
            if encoding in ("base64", "b64"):
                try:
                    data = base64.b64decode(str(content), validate=False)
                except Exception:
                    return [], f"attachments[{idx}] base64 解码失败"
            else:
                data = str(content).encode("utf-8")
            if not filename or filename == "attachment.bin":
                filename = f"attachment-{idx + 1}.bin"
        else:
            return [], f"attachments[{idx}] 需要 local_path 或 content"

        if not data:
            return [], f"attachments[{idx}] 内容为空"
        if max_file > 0 and len(data) > max_file:
            return [], f"attachments[{idx}] {filename} 超过单文件上限 {max_file} 字节"
        total += len(data)
        if max_total > 0 and total > max_total:
            return [], f"附件总大小超过上限 {max_total} 字节"
        mime_type = (raw.get("mime_type") or "").strip()
        if not mime_type:
            guessed, _ = mimetypes.guess_type(filename)
            mime_type = guessed or "application/octet-stream"
        out.append({"filename": filename, "data": data, "mime_type": mime_type})

    return out, None


def parse_notify_emails(raw: str) -> list[str]:
    if not raw:
        return []
    parts = []
    for chunk in str(raw).replace(";", ",").split(","):
        s = chunk.strip()
        if s and "@" in s:
            parts.append(s)
    return parts


_NOTIFY_LINE_RES = (
    re.compile(r"^\s*notify_email_to\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*结果通知邮箱\s*[:：]\s*(.+)$", re.MULTILINE),
    re.compile(r"^\s*通知邮箱\s*[:：]\s*(.+)$", re.MULTILINE),
    re.compile(r"^\s*#\s*notify_email_to\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE),
)


def infer_scheduled_task_notify_raw(content: str | None) -> str:
    """从任务正文按行解析显式写明的通知地址（库中 notify_email_to 为空时的补充，避免误抓正文里任意 @）。"""
    if not content:
        return ""
    seen: set[str] = set()
    ordered: list[str] = []
    for cre in _NOTIFY_LINE_RES:
        for m in cre.finditer(str(content)):
            fragment = (m.group(1) or "").strip()
            for addr in parse_notify_emails(fragment):
                key = addr.lower()
                if key not in seen:
                    seen.add(key)
                    ordered.append(addr)
    return ", ".join(ordered)


def effective_scheduled_task_notify_email_to(stored: str | None, content: str | None) -> str:
    s = (stored or "").strip()
    if s:
        return s
    return infer_scheduled_task_notify_raw(content)


async def upsert_user_mail_from_patch(db, user_id: int, patch: dict) -> tuple[bool, str, dict]:
    """
    合并 patch 写入 user_mail_config。
    patch 可含: mail_enabled, smtp_host, smtp_port, smtp_user, smtp_password, smtp_from, smtp_use_tls, smtp_use_ssl
    若 patch 含 smtp_password 且为非空字符串则更新密码；若含 smtp_password 且为空字符串则保留库中已有密码；若不含该键则保留当前内存中的密码。
    返回 (成功, 错误信息, 公开配置 dict)。
    """
    base = await load_user_mail_config(db, user_id)
    if "mail_enabled" in patch and patch["mail_enabled"] is not None:
        base["mail_enabled"] = bool(patch["mail_enabled"])
    if "smtp_host" in patch and patch["smtp_host"] is not None:
        base["smtp_host"] = str(patch["smtp_host"] or "").strip()
    if "smtp_port" in patch and patch["smtp_port"] is not None:
        try:
            p = int(patch["smtp_port"])
            base["smtp_port"] = p if 1 <= p <= 65535 else 587
        except (TypeError, ValueError):
            base["smtp_port"] = 587
    if "smtp_user" in patch and patch["smtp_user"] is not None:
        base["smtp_user"] = str(patch["smtp_user"] or "").strip()
    if "smtp_password" in patch and patch["smtp_password"] is not None:
        newp = str(patch["smtp_password"]).strip()
        if newp:
            base["smtp_password"] = newp
    if "smtp_from" in patch and patch["smtp_from"] is not None:
        base["smtp_from"] = str(patch["smtp_from"] or "").strip()
    # 注意：SSL 直连（implicit TLS, 通常端口 465）与 STARTTLS（通常端口 587）是
    # 互斥的两种 SMTP 连接模式，绝不能同时为 true：
    #   - smtp_use_ssl=True  → smtplib.SMTP_SSL（连接一开始就是 TLS），smtp_use_tls 会被发信侧忽略；
    #   - smtp_use_ssl=False, smtp_use_tls=True → smtplib.SMTP + STARTTLS（明文连接后协商升级）。
    # 历史上这里把 use_ssl→use_tls 一起置 True，导致前端只勾 SSL 直连保存后会被回填为
    # 「同时勾上 STARTTLS」，看起来像 BUG。这里按"SSL 优先且互斥"来归一化。
    use_tls_in_patch = "smtp_use_tls" in patch and patch["smtp_use_tls"] is not None
    use_ssl_in_patch = "smtp_use_ssl" in patch and patch["smtp_use_ssl"] is not None
    if use_tls_in_patch:
        base["smtp_use_tls"] = bool(patch["smtp_use_tls"])
    if use_ssl_in_patch:
        base["smtp_use_ssl"] = bool(patch["smtp_use_ssl"])
    # 互斥归一：本次提交里 SSL 与 STARTTLS 同时为 true 时，按"SSL 直连"为准，关掉 STARTTLS。
    if base["smtp_use_ssl"] and base["smtp_use_tls"]:
        # 如果用户本次明确传了 smtp_use_ssl=True，则以 SSL 优先，关掉 STARTTLS；
        # 否则（仅传了 use_tls=True，旧行残留 use_ssl=True）以本次的 STARTTLS 为准，关掉 SSL。
        if use_ssl_in_patch and bool(patch["smtp_use_ssl"]):
            base["smtp_use_tls"] = False
        else:
            base["smtp_use_ssl"] = False

    pwd_set = bool((base.get("smtp_password") or "").strip())
    if base["mail_enabled"] and not smtp_settings_complete(base):
        return False, (
            "开启邮件发送前需填写完整的 SMTP：服务器、端口、用户名、密码、发件人地址。"
            + USER_MAIL_SETUP_HINT_ZH
        ), public_mail_config_for_api(base, pwd_set)

    mail_en = "true" if base["mail_enabled"] else "false"
    tls_s = "true" if base["smtp_use_tls"] else "false"
    ssl_s = "true" if base["smtp_use_ssl"] else "false"
    port_s = str(base["smtp_port"])

    await db.execute(
        """INSERT INTO user_mail_config (user_id, mail_enabled, smtp_host, smtp_port, smtp_user, smtp_password, smtp_from, smtp_use_tls, smtp_use_ssl, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE SET
             mail_enabled=excluded.mail_enabled,
             smtp_host=excluded.smtp_host,
             smtp_port=excluded.smtp_port,
             smtp_user=excluded.smtp_user,
             smtp_password=excluded.smtp_password,
             smtp_from=excluded.smtp_from,
             smtp_use_tls=excluded.smtp_use_tls,
             smtp_use_ssl=excluded.smtp_use_ssl,
             updated_at=CURRENT_TIMESTAMP""",
        (
            user_id,
            mail_en,
            base["smtp_host"],
            port_s,
            base["smtp_user"],
            base["smtp_password"],
            base["smtp_from"],
            tls_s,
            ssl_s,
        ),
    )
    await db.commit()

    saved = await load_user_mail_config(db, user_id)
    ok_pwd = bool((saved.get("smtp_password") or "").strip())
    return True, "", public_mail_config_for_api(saved, ok_pwd)
