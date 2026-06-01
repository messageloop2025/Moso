"""认证相关 API：登录、注册、验证码、JWT、忘记密码、账户解锁、API 访问令牌鉴权"""
import asyncio
import hashlib
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
import bcrypt as _bcrypt
from pydantic import BaseModel

import config
from database import get_db
from services.email_sender import get_smtp_settings, send_email_to_user, send_email_to_address
from services.site_time import build_server_time_payload, get_effective_site_timezone

router = APIRouter(prefix="/api/auth", tags=["认证"])
security = HTTPBearer(auto_error=False)

# 验证码 JWT 有效期（分钟）
CAPTCHA_EXPIRE_MINUTES = 5

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
USERNAME_INVALID_DETAIL = "用户名仅支持字母、数字、点、下划线和连字符，长度 3–32 个字符"


def normalize_and_validate_username(raw: str) -> str:
    """strip 后校验用户名格式；非法则抛 HTTP 400。"""
    username = (raw or "").strip()
    if not USERNAME_PATTERN.match(username):
        raise HTTPException(status_code=400, detail=USERNAME_INVALID_DETAIL)
    return username


async def _cleanup_jwt_nonces(db) -> None:
    """清理已过期的 nonce 记录（保留略长于验证码/临时 token 有效期）。"""
    await db.execute(
        "DELETE FROM jwt_nonces WHERE created_at < datetime('now', '-20 minutes')"
    )


async def _consume_jwt_nonce(jti: str) -> bool:
    """尝试登记 jti；首次返回 True，已用过返回 False。"""
    jti = (jti or "").strip()
    if not jti:
        return False
    db = await get_db()
    await _cleanup_jwt_nonces(db)
    try:
        await db.execute("INSERT INTO jwt_nonces (jti) VALUES (?)", (jti,))
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        return False


def _generate_captcha() -> tuple[str, str]:
    """生成简单数学验证码：a + b = ?，返回 (question, answer)。"""
    a = random.randint(1, 20)
    b = random.randint(1, 20)
    return f"{a} + {b} = ?", str(a + b)


def create_captcha_token(answer: str) -> str:
    """将验证码答案签成短效 token，用于提交时校验（含一次性 jti）。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=CAPTCHA_EXPIRE_MINUTES)
    return jwt.encode(
        {"captcha": answer, "jti": secrets.token_urlsafe(16), "exp": expire},
        config.SECRET_KEY,
        algorithm=config.JWT_ALGORITHM,
    )


async def verify_and_consume_captcha(captcha_token: str | None, user_answer: str | None) -> bool:
    """校验验证码：JWT 有效、答案一致，且 token 仅可使用一次（提交即作废）。"""
    if not captcha_token or not user_answer:
        return False
    try:
        payload = jwt.decode(
            captcha_token, config.SECRET_KEY, algorithms=[config.JWT_ALGORITHM]
        )
    except JWTError:
        return False
    jti = (payload.get("jti") or "").strip()
    if not jti:
        return False
    if not await _consume_jwt_nonce(jti):
        return False
    expected = (payload.get("captcha") or "").strip()
    given = (user_answer or "").strip()
    return bool(expected and given and expected == given)


@router.get("/captcha")
async def get_captcha():
    """获取登录/注册用验证码。返回题目与 token，提交时需带上 token 与用户输入的答案。"""
    question, answer = _generate_captcha()
    token = create_captcha_token(answer)
    return {"success": True, "captcha_token": token, "question": question}


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_token: str = ""
    captcha_answer: str = ""


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    captcha_token: str = ""
    captcha_answer: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


def create_token(user_id: int, username: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=config.JWT_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "role": role, "exp": expire},
        config.SECRET_KEY,
        algorithm=config.JWT_ALGORITHM,
    )


def decode_login_jwt_with_grace(raw: str) -> dict | None:
    """解析登录 JWT；若在 REFRESH_GRACE 内过期仍返回 payload（用于滑动续期）。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return jwt.decode(raw, config.SECRET_KEY, algorithms=[config.JWT_ALGORITHM])
    except JWTError:
        pass
    try:
        payload = jwt.decode(
            raw,
            config.SECRET_KEY,
            algorithms=[config.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        exp = payload.get("exp")
        if exp is None:
            return None
        exp_dt = datetime.fromtimestamp(int(exp), tz=timezone.utc)
        grace = timedelta(minutes=max(0, int(config.JWT_REFRESH_GRACE_MINUTES)))
        if datetime.now(timezone.utc) > exp_dt + grace:
            return None
        return payload
    except (JWTError, TypeError, ValueError, OSError):
        return None


def hash_api_access_token(plain: str) -> str:
    """与用户 API 令牌表中存储的哈希一致（带 SECRET 盐派生，不明文存库）。"""
    pepper = (config.SECRET_KEY or "edgeops").encode("utf-8")
    return hashlib.sha256(pepper + b"|" + (plain or "").strip().encode("utf-8")).hexdigest()


async def _load_user_by_id(user_id: int) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT id, username, display_name, role, status, email, COALESCE(skills_enabled, 0) AS skills_enabled FROM users WHERE id = ?",
        (user_id,),
    )
    if not row:
        return None
    d = dict(row[0])
    d["skills_enabled"] = bool(d.get("skills_enabled", 0))
    return d


async def _user_from_api_token_plain(plain: str) -> dict | None:
    if not plain or len(plain.strip()) < 12:
        return None
    plain = plain.strip()
    h = hash_api_access_token(plain)
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.id AS tid, u.id, u.username, u.display_name, u.role, u.status, u.email,
                  COALESCE(u.skills_enabled, 0) AS skills_enabled
           FROM api_tokens t
           JOIN users u ON u.id = t.user_id
           WHERE t.token_hash = ?""",
        (h,),
    )
    if not rows:
        return None
    r = dict(rows[0])
    tid = r.pop("tid")
    await db.execute(
        "UPDATE api_tokens SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
        (tid,),
    )
    await db.commit()
    return {
        "id": r["id"],
        "username": r["username"],
        "display_name": r["display_name"],
        "role": r["role"],
        "status": r["status"],
        "email": r["email"],
        "skills_enabled": bool(r.get("skills_enabled", 0)),
    }


async def authenticate_bearer_credentials(raw: str) -> dict | None:
    """解析 Bearer 字符串：先 JWT（登录），再用户 API 令牌。返回 users 行字典或 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        payload = jwt.decode(raw, config.SECRET_KEY, algorithms=[config.JWT_ALGORITHM])
        user_id = int(payload.get("sub", 0))
        if user_id:
            return await _load_user_by_id(user_id)
    except JWTError:
        pass
    except (ValueError, TypeError):
        pass
    return await _user_from_api_token_plain(raw)


def assert_user_active(user: dict) -> None:
    """拒绝暂停与安全锁定账户；status 为 active / locked / suspended 三态（见用户管理）。"""
    st = (user.get("status") or "").strip().lower()
    if st == "suspended":
        raise HTTPException(status_code=403, detail="账户已被暂停使用，请联系管理员恢复")
    if st == "locked":
        raise HTTPException(
            status_code=403,
            detail="账户已锁定（安全锁定：多次密码错误等）。请通过登录页邮箱验证解锁或联系管理员解锁。",
        )
    if st != "active":
        raise HTTPException(status_code=403, detail="账户已被禁用")


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="未登录")
    user = await authenticate_bearer_credentials(credentials.credentials.strip())
    if not user:
        raise HTTPException(status_code=401, detail="Token已过期或无效")
    assert_user_active(user)
    return user


async def user_dict_for_websocket_from_token(token: str) -> dict | None:
    """WebSocket query token：成功返回 {id, username, role}，否则 None。"""
    raw = (token or "").strip()
    if not raw:
        return None
    try:
        payload = jwt.decode(raw, config.SECRET_KEY, algorithms=[config.JWT_ALGORITHM])
        user_id = int(payload.get("sub", 0))
        username = (payload.get("username") or "").strip()
        role = (payload.get("role") or "").strip()
        if user_id and username and role:
            # WebSocket 建连路径必须尽量轻：终端连接不能被 AI 工具链中的 SQLite 写入排队拖住。
            # JWT 已含必要身份与角色；HTTP API 仍走数据库校验账户状态。
            return {"id": user_id, "username": username, "role": role}
    except (JWTError, TypeError, ValueError):
        pass
    user = await authenticate_bearer_credentials(raw)
    if not user:
        return None
    try:
        assert_user_active(user)
    except HTTPException:
        return None
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


def _is_admin_role(role) -> bool:
    if role is None:
        return False
    s = str(role).strip()
    return bool(s and (s.lower() in ("admin", "manager") or s == "管理员"))


async def require_admin(user=Depends(get_current_user)):
    if not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# 登录失败次数上限与锁定时长（分钟）
LOGIN_FAIL_LOCK_THRESHOLD = 5
LOGIN_LOCK_MINUTES = 30


def _lock_deadline_active(locked_until) -> bool:
    """locked_until 仍在未来则视为处于安全锁定期内。"""
    if not locked_until:
        return False
    try:
        dt = locked_until
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < dt
    except (TypeError, ValueError):
        return False


async def normalize_user_lock_state(db, user: dict) -> dict:
    """
    登录前校正：已过期的安全锁改为 active；遗留的 active+未过期 locked_until 升为 locked。
    """
    uid = user["id"]
    st = (user.get("status") or "").strip().lower()
    lu = user.get("locked_until")
    if st == "suspended":
        return user
    if st == "locked":
        if not _lock_deadline_active(lu):
            await db.execute(
                "UPDATE users SET status = 'active', locked_until = NULL, failed_login_attempts = 0 WHERE id = ?",
                (uid,),
            )
            await db.commit()
            user["status"] = "active"
            user["locked_until"] = None
        return user
    if st == "active" and _lock_deadline_active(lu):
        await db.execute("UPDATE users SET status = 'locked' WHERE id = ?", (uid,))
        await db.commit()
        user["status"] = "locked"
    return user


async def _bcrypt_hash(password: str) -> str:
    return await asyncio.to_thread(
        lambda: _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
    )


async def _bcrypt_check(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    return await asyncio.to_thread(
        _bcrypt.checkpw, password.encode(), hashed.encode()
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    username = normalize_and_validate_username(req.username)
    if not await verify_and_consume_captcha(req.captcha_token, req.captcha_answer):
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新验证码后重试")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, username, display_name, password_hash, role, status, email, failed_login_attempts, locked_until, COALESCE(skills_enabled, 0) AS skills_enabled FROM users WHERE username = ?",
        (username,),
    )
    if not rows:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    user = dict(rows[0])
    user = await normalize_user_lock_state(db, user)
    if user["status"] == "suspended":
        raise HTTPException(status_code=403, detail="账户已被暂停使用，请联系管理员恢复")
    if user["status"] == "locked":
        raise HTTPException(
            status_code=403,
            detail="账户已锁定（连续多次密码错误）。可通过邮件解锁或联系管理员解锁。",
        )
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="账户已被禁用")

    now_utc = datetime.now(timezone.utc)

    if not await _bcrypt_check(req.password, user.get("password_hash") or ""):
        attempts = int(user.get("failed_login_attempts") or 0) + 1
        lock_until = None
        if attempts >= LOGIN_FAIL_LOCK_THRESHOLD:
            lock_until = now_utc + timedelta(minutes=LOGIN_LOCK_MINUTES)
        if lock_until:
            await db.execute(
                "UPDATE users SET failed_login_attempts = ?, locked_until = ?, status = 'locked' WHERE id = ?",
                (attempts, lock_until.isoformat(), user["id"]),
            )
        else:
            await db.execute(
                "UPDATE users SET failed_login_attempts = ? WHERE id = ?",
                (attempts, user["id"]),
            )
        await db.commit()
        if lock_until:
            from services.email_sender import send_notification_to_user
            await send_notification_to_user(db, user["id"], "email_template_lock_body", user.get("username") or "")
            raise HTTPException(
                status_code=403,
                detail="账户已锁定（连续多次密码错误）。可通过登录页找回功能解锁或联系管理员解锁。",
            )
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    await db.execute(
        "UPDATE users SET last_login = CURRENT_TIMESTAMP, failed_login_attempts = 0, locked_until = NULL, status = 'active' WHERE id = ?",
        (user["id"],),
    )
    await db.execute(
        "INSERT INTO user_login_events (user_id, login_type) VALUES (?, 'password')",
        (user["id"],),
    )
    await db.commit()

    token = create_token(user["id"], user["username"], user["role"])
    return TokenResponse(
        access_token=token,
        user={
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "email": (user.get("email") or "").strip() or None,
            "skills_enabled": bool(user.get("skills_enabled", 0)),
        },
    )


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest):
    username = normalize_and_validate_username(req.username)
    if not await verify_and_consume_captcha(req.captcha_token, req.captcha_answer):
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新验证码后重试")
    db = await get_db()
    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = 'self_register'")
    if rows and rows[0]["value"] != "true":
        raise HTTPException(status_code=403, detail="自助注册已关闭")

    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少6个字符")

    # 新实例：若库中尚无任何用户，或仅有预置 admin 账号，则首个自助注册用户自动为管理员（仅此一次）。
    cnt_rows = await db.execute_fetchall("SELECT COUNT(*) AS c FROM users")
    n_users = int(cnt_rows[0]["c"]) if cnt_rows else 0
    role = "user"
    if n_users == 0:
        role = "admin"
    elif n_users == 1:
        one = await db.execute_fetchall(
            "SELECT username, role FROM users ORDER BY id LIMIT 1"
        )
        if one:
            u0 = (one[0]["username"] or "").strip().lower()
            r0 = (one[0].get("role") or "").strip().lower()
            if u0 == "admin" and r0 == "admin":
                role = "admin"

    try:
        cursor = await db.execute(
            "INSERT INTO users (username, display_name, password_hash, role) VALUES (?, ?, ?, ?)",
            (
                username,
                req.display_name or username,
                await _bcrypt_hash(req.password),
                role,
            ),
        )
        await db.commit()
        user_id = cursor.lastrowid
    except Exception:
        raise HTTPException(status_code=400, detail="用户名已存在")

    token = create_token(user_id, username, role)
    return TokenResponse(
        access_token=token,
        user={
            "id": user_id,
            "username": username,
            "display_name": req.display_name or username,
            "role": role,
            "skills_enabled": False,
        },
    )


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    db = await get_db()
    tz = await get_effective_site_timezone(db)
    times = build_server_time_payload(tz)
    return {"success": True, "user": user, **times}


@router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """滑动续期：有效 JWT 或宽限期内过期的 JWT 可换取新 token（API 访问令牌不适用）。"""
    if not credentials:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_login_jwt_with_grace(credentials.credentials.strip())
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Token已过期或无效")
    try:
        user_id = int(payload.get("sub", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Token已过期或无效")
    user = await _load_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Token已过期或无效")
    assert_user_active(user)
    token = create_token(user["id"], user["username"], user["role"])
    return TokenResponse(
        access_token=token,
        user={
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": user["role"],
        "email": (user.get("email") or "").strip() or None,
        "skills_enabled": bool(user.get("skills_enabled", 0)),
    },
    )


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ---------- 绑定/解绑邮箱（6 位验证码，一次有效）----------
EMAIL_CODE_EXPIRE_MINUTES = 10
RECOVER_CODE_EXPIRE_MINUTES = 15
RECOVER_TEMP_TOKEN_EXPIRE_MINUTES = 15


def _generate_email_code() -> str:
    """生成 6 位数字验证码。"""
    return "".join(str(random.randint(0, 9)) for _ in range(6))


class SendBindEmailCodeRequest(BaseModel):
    email: str = ""


class VerifyBindEmailRequest(BaseModel):
    email: str = ""
    code: str = ""


@router.post("/send-bind-email-code")
async def send_bind_email_code(req: SendBindEmailCodeRequest, user=Depends(get_current_user)):
    """发送绑定邮箱验证码到目标邮箱。登录后调用，验证码 6 位、10 分钟内有效、一次有效。"""
    email = (req.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="请输入有效邮箱地址")
    db = await get_db()
    code = _generate_email_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=EMAIL_CODE_EXPIRE_MINUTES)
    cursor = await db.execute(
        "INSERT INTO email_verification_codes (user_id, email, code, purpose, expires_at) VALUES (?, ?, ?, ?, ?)",
        (user["id"], email, code, "bind", expires.isoformat()),
    )
    await db.commit()
    code_id = cursor.lastrowid
    body = f"您好，\n\n您正在绑定 毛竹 账户邮箱，验证码为：{code}\n\n{EMAIL_CODE_EXPIRE_MINUTES} 分钟内有效，请勿泄露。如非本人操作请忽略。"
    sent = await send_email_to_address(db, email, "毛竹 邮箱验证码", body)
    if not sent:
        await db.execute("DELETE FROM email_verification_codes WHERE id = ?", (code_id,))
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="邮件发送失败。若为连接超时，请检查：1) SMTP 地址与端口（587 常用 STARTTLS，465 常用 SSL）；2) 系统设置中是否勾选「使用 SSL」（465 端口建议勾选）；3) 本机网络能否访问该 SMTP 服务器。",
        )
    return {"success": True, "message": "验证码已发送到您的邮箱，请查收。"}


@router.post("/verify-bind-email")
async def verify_bind_email(req: VerifyBindEmailRequest, user=Depends(get_current_user)):
    """使用验证码完成邮箱绑定。"""
    email = (req.email or "").strip().lower()
    code = (req.code or "").strip()
    if not email or not code:
        raise HTTPException(status_code=400, detail="请输入邮箱和验证码")
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, expires_at FROM email_verification_codes
           WHERE user_id = ? AND email = ? AND code = ? AND purpose = 'bind' AND used_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (user["id"], email, code),
    )
    if not rows:
        raise HTTPException(status_code=400, detail="验证码错误或已使用，请重新获取")
    r = dict(rows[0])
    try:
        exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="验证码无效")
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE email_verification_codes SET used_at = ? WHERE id = ?", (now, r["id"]))
    await db.execute("UPDATE users SET email = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (email, user["id"]))
    await db.commit()
    return {"success": True, "message": "邮箱已绑定。"}


@router.post("/unbind-email")
async def unbind_email(user=Depends(get_current_user)):
    """解绑当前用户邮箱。无需验证。"""
    db = await get_db()
    await db.execute("UPDATE users SET email = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
    await db.commit()
    return {"success": True, "message": "邮箱已解绑。"}


# ---------- 找回密码/解除锁定（登录页：用户名+邮箱 → 验证码 → 重设/解锁）----------
class RequestRecoverRequest(BaseModel):
    username: str = ""
    email: str = ""


class VerifyRecoverRequest(BaseModel):
    username: str = ""
    email: str = ""
    code: str = ""


class RecoverCompleteRequest(BaseModel):
    temp_token: str = ""
    action: str = ""  # reset | unlock
    new_password: str = ""


@router.post("/request-recover")
async def request_recover(req: RequestRecoverRequest):
    """找回密码/解除锁定：输入用户名与邮箱，校验一致后向该邮箱发送 6 位验证码。"""
    try:
        username = normalize_and_validate_username(req.username)
    except HTTPException:
        raise HTTPException(status_code=400, detail="请输入用户名和有效邮箱地址")
    email = (req.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="请输入用户名和有效邮箱地址")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, username, email, status FROM users WHERE username = ? AND status IN ('active', 'locked')",
        (username,),
    )
    if not rows:
        return {"success": True, "message": "若用户名与邮箱匹配，将收到验证码。"}
    user = dict(rows[0])
    bound = (user.get("email") or "").strip().lower()
    if bound != email:
        return {"success": True, "message": "若用户名与邮箱匹配，将收到验证码。"}
    code = _generate_email_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=RECOVER_CODE_EXPIRE_MINUTES)
    cursor = await db.execute(
        "INSERT INTO email_verification_codes (user_id, email, code, purpose, expires_at) VALUES (?, ?, ?, ?, ?)",
        (user["id"], email, code, "recover", expires.isoformat()),
    )
    await db.commit()
    code_id = cursor.lastrowid
    body = f"您好 {user.get('username', '')}，\n\n您正在通过邮箱找回 毛竹 账户，验证码为：{code}\n\n{RECOVER_CODE_EXPIRE_MINUTES} 分钟内有效，请勿泄露。如非本人操作请忽略。"
    sent = await send_email_to_address(db, email, "毛竹 找回账户验证码", body)
    if not sent:
        await db.execute("DELETE FROM email_verification_codes WHERE id = ?", (code_id,))
        await db.commit()
        raise HTTPException(status_code=503, detail="邮件发送失败，请检查系统 SMTP 配置。")
    return {"success": True, "message": "验证码已发送到您的邮箱，请查收。"}


@router.post("/verify-recover")
async def verify_recover(req: VerifyRecoverRequest):
    """校验找回验证码，返回临时 token，用于下一步重设密码或仅解锁。"""
    try:
        username = normalize_and_validate_username(req.username)
    except HTTPException:
        raise HTTPException(status_code=400, detail="验证码错误或已使用")
    email = (req.email or "").strip().lower()
    code = (req.code or "").strip()
    if not email or not code:
        raise HTTPException(status_code=400, detail="请输入用户名、邮箱和验证码")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, username, email, status FROM users WHERE username = ? AND status IN ('active', 'locked')",
        (username,),
    )
    if not rows:
        raise HTTPException(status_code=400, detail="验证码错误或已使用")
    user = dict(rows[0])
    if (user.get("email") or "").strip().lower() != email:
        raise HTTPException(status_code=400, detail="验证码错误或已使用")
    rows = await db.execute_fetchall(
        """SELECT id, expires_at FROM email_verification_codes
           WHERE user_id = ? AND email = ? AND code = ? AND purpose = 'recover' AND used_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (user["id"], email, code),
    )
    if not rows:
        raise HTTPException(status_code=400, detail="验证码错误或已使用")
    r = dict(rows[0])
    try:
        exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="验证码无效")
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE email_verification_codes SET used_at = ? WHERE id = ?", (now, r["id"]))
    await db.commit()
    expire = datetime.now(timezone.utc) + timedelta(minutes=RECOVER_TEMP_TOKEN_EXPIRE_MINUTES)
    temp_token = jwt.encode(
        {
            "sub": str(user["id"]),
            "purpose": "recover",
            "jti": secrets.token_urlsafe(16),
            "exp": expire,
        },
        config.SECRET_KEY,
        algorithm=config.JWT_ALGORITHM,
    )
    return {"success": True, "temp_token": temp_token, "username": user["username"]}


@router.post("/recover-complete")
async def recover_complete(req: RecoverCompleteRequest):
    """使用临时 token 完成重设密码或仅解锁。action=reset 时需提供 new_password。暂停账户不可自助解锁。"""
    token = (req.temp_token or "").strip()
    action = (req.action or "").strip().lower()
    if not token or action not in ("reset", "unlock"):
        raise HTTPException(status_code=400, detail="缺少临时凭证或操作类型")
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=[config.JWT_ALGORITHM])
        if payload.get("purpose") != "recover":
            raise HTTPException(status_code=400, detail="无效的临时凭证")
        user_id = int(payload.get("sub", 0))
        if not user_id:
            raise HTTPException(status_code=400, detail="无效的临时凭证")
        jti = (payload.get("jti") or "").strip()
        if not jti or not await _consume_jwt_nonce(jti):
            raise HTTPException(status_code=400, detail="临时凭证已过期或无效")
    except JWTError:
        raise HTTPException(status_code=400, detail="临时凭证已过期或无效")
    db = await get_db()
    user_rows = await db.execute_fetchall("SELECT id, username, status FROM users WHERE id = ?", (user_id,))
    if not user_rows:
        raise HTTPException(status_code=400, detail="用户不存在")
    user_row = dict(user_rows[0])
    if action == "unlock" and user_row.get("status") == "suspended":
        raise HTTPException(status_code=400, detail="账户已被管理员暂停，无法自助解锁，请联系管理员恢复")
    if action == "reset":
        if len(req.new_password or "") < 6:
            raise HTTPException(status_code=400, detail="新密码至少 6 个字符")
        new_hash = await _bcrypt_hash(req.new_password)
        await db.execute(
            "UPDATE users SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL, status = 'active', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_hash, user_id),
        )
    else:
        await db.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, status = 'active', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user_id,),
        )
    await db.commit()
    if action == "unlock":
        from services.email_sender import send_notification_to_user
        await send_notification_to_user(db, user_id, "email_template_unlock_body", user_row.get("username") or "")
    msg = "密码已重置，请使用新密码登录。" if action == "reset" else "账户已解锁，请重新登录。"
    return {"success": True, "message": msg}


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, user=Depends(get_current_user)):
    """当前用户修改自己的密码：需提供旧密码验证，新密码至少 6 位。"""
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 个字符")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
    )
    if not rows:
        raise HTTPException(status_code=401, detail="用户不存在")
    current_hash = rows[0]["password_hash"]
    if not await _bcrypt_check(req.old_password, current_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    new_hash = await _bcrypt_hash(req.new_password)
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_hash, user["id"]),
    )
    await db.commit()
    return {"success": True}


# ---------- 忘记密码 / 邮件重置 / 邮件解锁 ----------
RESET_TOKEN_EXPIRE_MINUTES = 60
TOKEN_KIND_RESET = "reset"
TOKEN_KIND_UNLOCK = "unlock"


async def _get_site_url(db) -> str:
    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = 'site_url'")
    return (rows[0]["value"] if rows else "") or ""


class ForgotPasswordRequest(BaseModel):
    username: str = ""


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    """忘记密码：按用户名查找用户，若已绑定邮箱则发重置链接（需配置 SMTP 与 site_url）。"""
    try:
        username = normalize_and_validate_username(req.username)
    except HTTPException:
        return {"success": True, "message": "若该用户存在且已绑定邮箱，将收到重置邮件。"}
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, username, email FROM users WHERE username = ? AND status IN ('active', 'locked')",
        (username,),
    )
    if not rows:
        return {"success": True, "message": "若该用户存在且已绑定邮箱，将收到重置邮件。"}
    user = dict(rows[0])
    if not (user.get("email") or "").strip():
        return {"success": True, "message": "若该用户存在且已绑定邮箱，将收到重置邮件。"}

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_EXPIRE_MINUTES)
    await db.execute(
        "INSERT INTO password_reset_tokens (token, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
        (token, user["id"], TOKEN_KIND_RESET, expires.isoformat()),
    )
    await db.commit()

    site_url = (await _get_site_url(db)).rstrip("/")
    link = f"{site_url}/reset-password?token={token}" if site_url else f"（请配置 site_url 后使用）token={token}"
    body = f"您好 {user.get('username', '')}，\n\n您正在通过邮件重置 毛竹 登录密码。请点击或复制以下链接（{RESET_TOKEN_EXPIRE_MINUTES} 分钟内有效）：\n\n{link}\n\n如非本人操作请忽略。"
    sent = await send_email_to_user(db, user["id"], "毛竹 密码重置", body)
    if not sent:
        await db.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
        await db.commit()
        raise HTTPException(status_code=503, detail="邮件发送失败，请检查系统 SMTP 配置与用户邮箱。")
    return {"success": True, "message": "若该用户存在且已绑定邮箱，将收到重置邮件。"}


class ResetPasswordByTokenRequest(BaseModel):
    token: str
    new_password: str


class UnlockByTokenRequest(BaseModel):
    token: str


@router.post("/reset-password")
async def reset_password(req: ResetPasswordByTokenRequest):
    """通过邮件链接中的 token 重置密码（无需登录）。"""
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 个字符")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT user_id, kind, expires_at FROM password_reset_tokens WHERE token = ?",
        (req.token.strip(),),
    )
    if not rows:
        raise HTTPException(status_code=400, detail="链接无效或已过期")
    r = dict(rows[0])
    if r["kind"] != TOKEN_KIND_RESET:
        raise HTTPException(status_code=400, detail="链接无效或已过期")
    try:
        exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            await db.execute("DELETE FROM password_reset_tokens WHERE token = ?", (req.token.strip(),))
            await db.commit()
            raise HTTPException(status_code=400, detail="链接已过期，请重新申请")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="链接无效或已过期")
    user_id = r["user_id"]
    new_hash = await _bcrypt_hash(req.new_password)
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP, failed_login_attempts = 0, locked_until = NULL, status = 'active' WHERE id = ?",
        (new_hash, user_id),
    )
    await db.execute("DELETE FROM password_reset_tokens WHERE token = ?", (req.token.strip(),))
    await db.commit()
    return {"success": True, "message": "密码已重置，请使用新密码登录。"}


@router.post("/request-unlock")
async def request_unlock(req: ForgotPasswordRequest):
    """账户被锁定时，通过用户名请求解锁邮件（需用户已绑定邮箱且配置 SMTP）。"""
    try:
        username = normalize_and_validate_username(req.username)
    except HTTPException:
        return {"success": True, "message": "若该账户存在且已绑定邮箱，将收到解锁邮件。"}
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, username, email, locked_until, status FROM users WHERE username = ? AND status IN ('active', 'locked')",
        (username,),
    )
    if not rows:
        return {"success": True, "message": "若该账户存在且已绑定邮箱，将收到解锁邮件。"}
    user = dict(rows[0])
    if not (user.get("email") or "").strip():
        return {"success": True, "message": "若该账户存在且已绑定邮箱，将收到解锁邮件。"}
    st = (user.get("status") or "").strip().lower()
    if st != "locked" and not user.get("locked_until"):
        return {"success": True, "message": "该账户未锁定。"}

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_EXPIRE_MINUTES)
    await db.execute(
        "INSERT INTO password_reset_tokens (token, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
        (token, user["id"], TOKEN_KIND_UNLOCK, expires.isoformat()),
    )
    await db.commit()

    site_url = (await _get_site_url(db)).rstrip("/")
    link = f"{site_url}/unlock?token={token}" if site_url else f"（请配置 site_url）token={token}"
    body = f"您好 {user.get('username', '')}，\n\n您的 毛竹 账户因多次密码错误已被锁定。请点击或复制以下链接解锁（{RESET_TOKEN_EXPIRE_MINUTES} 分钟内有效）：\n\n{link}\n\n如非本人操作请忽略。"
    sent = await send_email_to_user(db, user["id"], "毛竹 账户解锁", body)
    if not sent:
        await db.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
        await db.commit()
        raise HTTPException(status_code=503, detail="邮件发送失败，请检查系统 SMTP 配置与用户邮箱。")
    return {"success": True, "message": "若该账户存在且已绑定邮箱，将收到解锁邮件。"}


@router.post("/unlock-by-token")
async def unlock_by_token(req: UnlockByTokenRequest):
    """通过邮件中的 token 解锁账户。"""
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="缺少 token")
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT user_id, kind, expires_at FROM password_reset_tokens WHERE token = ?",
        (token,),
    )
    if not rows:
        raise HTTPException(status_code=400, detail="链接无效或已过期")
    r = dict(rows[0])
    if r["kind"] != TOKEN_KIND_UNLOCK:
        raise HTTPException(status_code=400, detail="链接无效或已过期")
    try:
        exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            await db.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
            await db.commit()
            raise HTTPException(status_code=400, detail="链接已过期，请重新申请")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="链接无效或已过期")
    user_id = r["user_id"]
    await db.execute(
        "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, status = 'active' WHERE id = ?",
        (user_id,),
    )
    await db.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
    await db.commit()
    return {"success": True, "message": "账户已解锁，请重新登录。"}
