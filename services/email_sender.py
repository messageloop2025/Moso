"""发邮件服务：从 settings 读取 SMTP 配置，用于密码重置、账户解锁等。"""
import logging
import mimetypes
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("edgeops.email")

# 连接与登录超时（秒），网络较慢或海外 SMTP 可适当调大
SMTP_TIMEOUT = 30


async def get_smtp_settings(db) -> dict:
    """从 settings 表读取 SMTP 配置。"""
    keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "smtp_use_tls", "smtp_use_ssl"]
    out = {}
    for k in keys:
        rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (k,))
        out[k] = (rows[0]["value"] if rows else "") or ""
    try:
        out["smtp_port"] = int(out.get("smtp_port") or "587")
    except (TypeError, ValueError):
        out["smtp_port"] = 587
    out["smtp_use_tls"] = (out.get("smtp_use_tls") or "true").strip().lower() == "true"
    # 端口 465 一般用 SSL 直连，587 用 STARTTLS
    out["smtp_use_ssl"] = (out.get("smtp_use_ssl") or "").strip().lower() == "true"
    return out


def _build_email_message(
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    attachments: list[dict] | None = None,
) -> MIMEMultipart:
    """构建 MIME 邮件：默认 plain；可选 html（multipart/alternative）；可选附件（multipart/mixed）。"""
    attachments = attachments or []
    has_html = bool((body_html or "").strip())
    has_atts = bool(attachments)

    if has_atts:
        root = MIMEMultipart("mixed")
    else:
        root = MIMEMultipart("alternative")

    root["Subject"] = subject
    root["From"] = from_addr
    root["To"] = ", ".join(to_addrs)

    if has_atts:
        body_part = MIMEMultipart("alternative")
        root.attach(body_part)
    else:
        body_part = root

    body_part.attach(MIMEText(body_text or "", "plain", "utf-8"))
    if has_html:
        body_part.attach(MIMEText(body_html or "", "html", "utf-8"))

    for att in attachments:
        filename = (att.get("filename") or "attachment.bin").strip() or "attachment.bin"
        data = att.get("data")
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        mime_type = (att.get("mime_type") or "").strip()
        if not mime_type:
            guessed, _ = mimetypes.guess_type(filename)
            mime_type = guessed or "application/octet-stream"
        maintype, _, subtype = mime_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        part = MIMEBase(maintype, subtype)
        part.set_payload(bytes(data))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        root.attach(part)

    return root


def send_email_sync(
    host: str,
    port: int,
    user: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    use_tls: bool = True,
    use_ssl: bool = False,
    timeout: int = SMTP_TIMEOUT,
    *,
    body_html: str | None = None,
    attachments: list[dict] | None = None,
) -> bool:
    """同步发送邮件。默认 plain/text；可选 body_html（multipart/alternative）；可选 attachments（bytes + filename）。"""
    if not host or not to_addrs:
        return False
    if not (body_text or "").strip() and not (body_html or "").strip():
        return False
    try:
        msg = _build_email_message(
            from_addr=from_addr or user,
            to_addrs=to_addrs,
            subject=subject,
            body_text=body_text or "",
            body_html=body_html,
            attachments=attachments,
        )
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as s:
                if user and password:
                    s.login(user, password)
                s.sendmail(from_addr or user, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                if use_tls:
                    s.starttls()
                if user and password:
                    s.login(user, password)
                s.sendmail(from_addr or user, to_addrs, msg.as_string())
        return True
    except Exception as e:
        logger.warning("发送邮件失败: %s", e)
        return False


async def send_email_to_user(db, user_id: int, subject: str, body: str) -> bool:
    """根据 user_id 查用户 email，从 settings 取 SMTP 配置并发送。无邮箱或未配置 SMTP 时返回 False。"""
    rows = await db.execute_fetchall("SELECT email FROM users WHERE id = ?", (user_id,))
    if not rows:
        return False
    to_email = (rows[0]["email"] or "").strip()
    if not to_email:
        return False
    return await send_email_to_address(db, to_email, subject, body)


async def get_notification_email(db, template_key: str, username: str) -> tuple[str, str]:
    """从 settings 读取通知邮件标题与正文模板，将 {{username}} 替换为账户名。返回 (subject, body)。"""
    subject_rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = 'email_notification_subject'")
    subject = (subject_rows[0]["value"] if subject_rows else "") or "毛竹通知"
    body_rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (template_key,))
    body = (body_rows[0]["value"] if body_rows else "").replace("{{username}}", username or "")
    return (subject.strip(), body)


async def send_notification_to_user(db, user_id: int, template_key: str, username: str | None = None) -> bool:
    """根据 user_id 查用户邮箱与用户名，用模板发通知邮件。若未传 username 则从数据库取。"""
    rows = await db.execute_fetchall("SELECT email, username FROM users WHERE id = ?", (user_id,))
    if not rows:
        return False
    to_email = (rows[0]["email"] or "").strip()
    if not to_email:
        return False
    name = username if username is not None else (rows[0]["username"] or "")
    subject, body = await get_notification_email(db, template_key, name)
    return await send_email_to_address(db, to_email, subject, body)


async def send_email_to_address(db, to_email: str, subject: str, body: str) -> bool:
    """向指定邮箱发送纯文本邮件（用于绑定邮箱验证码、找回密码验证码等）。"""
    to_email = (to_email or "").strip()
    if not to_email:
        return False
    cfg = await get_smtp_settings(db)
    if not (cfg.get("smtp_host") or "").strip():
        return False
    return send_email_sync(
        host=cfg["smtp_host"],
        port=cfg["smtp_port"],
        user=(cfg.get("smtp_user") or "").strip(),
        password=(cfg.get("smtp_password") or "").strip(),
        from_addr=(cfg.get("smtp_from") or cfg.get("smtp_user") or "").strip(),
        to_addrs=[to_email],
        subject=subject,
        body_text=body,
        use_tls=cfg["smtp_use_tls"],
        use_ssl=cfg.get("smtp_use_ssl", False),
        timeout=SMTP_TIMEOUT,
    )
