"""服务凭证库：按用户保存远程服务登录信息，供 AI 在密码提示时自动注入 stdin（不可查询明文）。"""
from __future__ import annotations

import json
import logging
from typing import Any

from database import get_db

logger = logging.getLogger("edgeops.credential_vault")

SETTINGS_KEY = "credentials_vault_enabled"

CREDENTIAL_VAULT_TOOL_NAMES = frozenset({
    "list_service_credentials",
    "add_service_credential",
    "update_service_credential",
    "delete_service_credential",
    "send_service_password",
})

KNOWN_SERVICES = (
    "sudo",
    "ssh",
    "mysql",
    "postgres",
    "redis",
    "mongodb",
    "ftp",
    "other",
)

DEFAULT_PORTS: dict[str, int] = {
    "ssh": 22,
    "mysql": 3306,
    "postgres": 5432,
    "redis": 6379,
    "mongodb": 27017,
    "ftp": 21,
}

_PUBLIC_COLUMNS = (
    "id, user_id, host_id, service, address, port, service_username, label, notes, "
    "linked_credential_id, linked_host_id, created_at, updated_at"
)


def _norm(s: str | None) -> str:
    return (s or "").strip()


def _norm_addr(s: str | None) -> str:
    return _norm(s).lower()


def _norm_port(port: int | str | None) -> int | None:
    if port is None or port == "":
        return None
    try:
        p = int(port)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def default_port_for_service(service: str | None) -> int | None:
    return DEFAULT_PORTS.get(_norm(service).lower())


def effective_port(service: str | None, port: int | None) -> int | None:
    """NULL/未设端口视为该 service 的默认端口（sudo 等无端口则 None）。"""
    explicit = _norm_port(port)
    if explicit is not None:
        return explicit
    return default_port_for_service(service)


def ports_match(service: str | None, stored_port: int | None, query_port: int | None) -> bool:
    if query_port is None:
        return True
    stored_eff = effective_port(service, stored_port)
    query_eff = effective_port(service, query_port)
    if stored_eff is None and query_eff is None:
        return True
    return stored_eff == query_eff


async def credentials_vault_enabled(db=None) -> bool:
    if db is None:
        db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT value FROM settings WHERE key = ? LIMIT 1",
        (SETTINGS_KEY,),
    )
    if not rows:
        return False
    v = (rows[0][0] if isinstance(rows[0], tuple) else rows[0]["value"] or "").strip().lower()
    return v in ("1", "true", "yes", "on")


async def filter_credential_vault_tools(tools: list) -> list:
    db = await get_db()
    if await credentials_vault_enabled(db):
        return tools
    return [t for t in tools if (t.get("function") or {}).get("name") not in CREDENTIAL_VAULT_TOOL_NAMES]


def row_to_public_dict(row: dict) -> dict:
    """列表/详情用：绝不包含 password_enc 明文。"""
    d = dict(row)
    has_stored = bool(_norm(row.get("password_enc") or ""))
    has_link = bool(row.get("linked_host_id") or row.get("linked_credential_id"))
    d.pop("password_enc", None)
    d["has_password"] = has_stored or has_link
    return d


async def list_credentials_for_user(
    user_id: int,
    *,
    service: str | None = None,
    address: str | None = None,
    port: int | None = None,
    service_username: str | None = None,
    host_id: int | None = None,
) -> list[dict]:
    db = await get_db()
    sql = f"SELECT {_PUBLIC_COLUMNS} FROM host_service_credentials WHERE user_id = ?"
    params: list[Any] = [user_id]
    if service:
        sql += " AND lower(service) = lower(?)"
        params.append(_norm(service))
    if address is not None:
        sql += " AND lower(address) = lower(?)"
        params.append(_norm_addr(address))
    if service_username:
        sql += " AND lower(service_username) = lower(?)"
        params.append(_norm(service_username))
    if host_id is not None:
        sql += " AND host_id = ?"
        params.append(host_id)
    sql += " ORDER BY updated_at DESC, id DESC"
    rows = await db.execute_fetchall(sql, params)
    items = [row_to_public_dict(dict(r)) for r in rows]
    if port is not None:
        items = [i for i in items if ports_match(i.get("service"), i.get("port"), port)]
    return items


async def add_credential(
    user: dict,
    *,
    service: str,
    password: str | None = None,
    address: str = "",
    port: int | None = None,
    service_username: str = "",
    label: str = "",
    notes: str = "",
    linked_credential_id: int | None = None,
    linked_host_id: int | None = None,
    host_id: int | None = None,
) -> dict:
    service = _norm(service) or "other"
    pwd = _norm(password)
    if not pwd and not linked_host_id and not linked_credential_id:
        raise ValueError("password 不能为空（未设置 linked_host_id / linked_credential_id 时）")
    db = await get_db()
    if host_id is not None:
        from services.ai_skills import _can_access_host_with_shares, _get_host_row

        row = await _get_host_row(host_id)
        if not row:
            raise ValueError(f"主机 ID={host_id} 不存在")
        if not await _can_access_host_with_shares(row, user):
            raise ValueError("无权访问该主机")
    await db.execute(
        """INSERT INTO host_service_credentials
           (user_id, host_id, service, address, port, service_username, label, notes, password_enc,
            linked_credential_id, linked_host_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            user["id"],
            host_id,
            service,
            _norm(address),
            _norm_port(port),
            _norm(service_username),
            _norm(label),
            _norm(notes),
            pwd,
            linked_credential_id,
            linked_host_id,
        ),
    )
    await db.commit()
    rid = (await db.execute_fetchall("SELECT last_insert_rowid() AS id"))[0]
    cid = rid["id"] if isinstance(rid, dict) else rid[0]
    rows = await db.execute_fetchall(
        f"SELECT {_PUBLIC_COLUMNS} FROM host_service_credentials WHERE id = ? AND user_id = ?",
        (cid, user["id"]),
    )
    return row_to_public_dict(dict(rows[0]))


async def update_credential(
    user: dict,
    credential_id: int,
    *,
    service: str | None = None,
    password: str | None = None,
    address: str | None = None,
    port: int | None = None,
    service_username: str | None = None,
    label: str | None = None,
    notes: str | None = None,
    linked_credential_id: int | None = None,
    linked_host_id: int | None = None,
    host_id: int | None = None,
) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM host_service_credentials WHERE id = ? AND user_id = ?",
        (credential_id, user["id"]),
    )
    if not rows:
        raise ValueError("凭证不存在")
    updates: list[str] = []
    params: list[Any] = []
    if service is not None:
        updates.append("service = ?")
        params.append(_norm(service) or "other")
    if address is not None:
        updates.append("address = ?")
        params.append(_norm(address))
    if port is not None:
        updates.append("port = ?")
        params.append(_norm_port(port))
    if service_username is not None:
        updates.append("service_username = ?")
        params.append(_norm(service_username))
    if label is not None:
        updates.append("label = ?")
        params.append(_norm(label))
    if notes is not None:
        updates.append("notes = ?")
        params.append(_norm(notes))
    if password is not None and _norm(password):
        updates.append("password_enc = ?")
        params.append(_norm(password))
    if linked_credential_id is not None:
        updates.append("linked_credential_id = ?")
        params.append(linked_credential_id)
    if linked_host_id is not None:
        updates.append("linked_host_id = ?")
        params.append(linked_host_id)
    if host_id is not None:
        updates.append("host_id = ?")
        params.append(host_id)
    if not updates:
        raise ValueError("没有可更新的字段")
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.extend([credential_id, user["id"]])
    await db.execute(
        f"UPDATE host_service_credentials SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
        params,
    )
    await db.commit()
    rows = await db.execute_fetchall(
        f"SELECT {_PUBLIC_COLUMNS} FROM host_service_credentials WHERE id = ? AND user_id = ?",
        (credential_id, user["id"]),
    )
    return row_to_public_dict(dict(rows[0]))


async def delete_credential(user: dict, credential_id: int) -> None:
    db = await get_db()
    cur = await db.execute(
        "DELETE FROM host_service_credentials WHERE id = ? AND user_id = ?",
        (credential_id, user["id"]),
    )
    await db.commit()
    if cur.rowcount == 0:
        raise ValueError("凭证不存在")


async def _get_password_row(user_id: int, credential_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM host_service_credentials WHERE id = ? AND user_id = ?",
        (credential_id, user_id),
    )
    return dict(rows[0]) if rows else None


async def _resolve_linked_credential_password(db, linked_credential_id: int) -> str | None:
    rows = await db.execute_fetchall(
        "SELECT type, password_enc FROM credentials WHERE id = ?",
        (linked_credential_id,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    if (row.get("type") or "").strip().lower() not in ("password",):
        return None
    return row.get("password_enc") or None


async def _resolve_login_password_for_host(db, linked_host_id: int) -> str | None:
    from api.hosts import _resolve_host_auth

    rows = await db.execute_fetchall("SELECT * FROM hosts WHERE id = ?", (linked_host_id,))
    if not rows:
        return None
    auth = await _resolve_host_auth(db, dict(rows[0]))
    if not auth:
        return None
    if (auth.get("auth_type") or "password") == "password":
        return auth.get("password") or None
    return None


async def _resolve_password_from_row(db, row: dict) -> str | None:
    pwd = _norm(row.get("password_enc") or "")
    if pwd:
        return pwd
    if row.get("linked_host_id"):
        lp = await _resolve_login_password_for_host(db, int(row["linked_host_id"]))
        if lp:
            return lp
    if row.get("linked_credential_id"):
        lp = await _resolve_linked_credential_password(db, int(row["linked_credential_id"]))
        if lp:
            return lp
    return None


async def _fetch_matching_rows(
    db,
    user_id: int,
    *,
    service: str,
    address: str | None = None,
    service_username: str | None = None,
    port: int | None = None,
) -> list[dict]:
    svc = _norm(service) or "sudo"
    sql = """SELECT * FROM host_service_credentials
             WHERE user_id = ? AND lower(service) = lower(?)"""
    params: list[Any] = [user_id, svc]
    addr = _norm_addr(address) if address is not None else None
    if addr:
        sql += " AND lower(address) = lower(?)"
        params.append(addr)
    elif svc == "sudo":
        sql += " AND (address = '' OR address IS NULL)"
    uname = _norm(service_username)
    if uname:
        sql += " AND lower(service_username) = lower(?)"
        params.append(uname)
    sql += " ORDER BY updated_at DESC, id DESC"
    rows = await db.execute_fetchall(sql, params)
    out = [dict(r) for r in rows]
    if port is not None:
        out = [r for r in out if ports_match(svc, r.get("port"), port)]
    return out


async def resolve_credential_for_injection(
    user: dict,
    *,
    credential_id: int,
) -> tuple[dict | None, str | None]:
    """按 credential_id 加载当前用户凭证，返回 (行, 密码)。"""
    db = await get_db()
    row = await _get_password_row(user["id"], int(credential_id))
    if not row:
        return None, None
    pwd = await _resolve_password_from_row(db, row)
    return row, pwd


async def inject_password_to_target(
    user: dict,
    *,
    target: str,
    password: str,
    scope: str | None = None,
    terminal_scope_id: str | None = None,
    slot: int | None = None,
    channel_id: int | None = None,
    host_id: int | None = None,
) -> tuple[bool, str]:
    """向终端或 ssh_channel 发送密码（含换行）。不在返回值中回显密码。"""
    from api.terminal import (
        resolve_ai_slot,
        send_to_user_terminal,
        wait_for_terminal_session_ready,
    )
    from services.ssh_channel_manager import SSHChannelManager
    from services.ssh_channel_service import reconcile_channel_if_stale

    payload = password if password.endswith("\n") else password + "\n"
    target = (target or "terminal").strip().lower()

    if target == "ssh_channel":
        if channel_id is None:
            return False, "ssh_channel 目标需要 channel_id"
        db = await get_db()
        await reconcile_channel_if_stale(db, user, int(channel_id))
        rows = await db.execute_fetchall(
            "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ? AND status = 'open'",
            (channel_id, user["id"]),
        )
        if not rows:
            return False, "SSH 通道不存在或已关闭"
        err = SSHChannelManager.get_instance().send(int(channel_id), payload)
        if err:
            return False, err
        return True, "已向 SSH 通道注入密码（未在工具结果中返回明文）"

    if target == "local_terminal":
        from api import local_host

        slot_val, slot_err = local_host.resolve_local_slot(
            user["id"], terminal_scope_id, slot
        )
        if slot_err:
            return False, slot_err
        ok = await local_host.send_to_local_terminal(user["id"], slot_val, payload, terminal_scope_id)
        if not ok:
            await local_host.wait_for_local_terminal_ready(user["id"], slot_val, terminal_scope_id)
            ok = await local_host.send_to_local_terminal(user["id"], slot_val, payload, terminal_scope_id)
        if not ok:
            return False, "本机控制台未就绪"
        return True, "已向本机控制台注入密码（未在工具结果中返回明文）"

    slot_val, slot_err = resolve_ai_slot(
        user["id"], terminal_scope_id, slot, host_id
    )
    if slot_err:
        return False, slot_err
    ok = send_to_user_terminal(user["id"], payload, slot_val, scope_id=terminal_scope_id)
    if not ok:
        await wait_for_terminal_session_ready(user["id"], slot_val, terminal_scope_id)
        ok = send_to_user_terminal(user["id"], payload, slot_val, scope_id=terminal_scope_id)
    if not ok:
        return False, "AI 控制台未连接，请先 connect_terminal 或 create_console"
    return True, "已向 AI 控制台注入密码（未在工具结果中返回明文）"


async def perform_service_password_injection(
    user: dict,
    *,
    credential_id: int,
    target: str = "terminal",
    host_id: int | None = None,
    slot: int | None = None,
    channel_id: int | None = None,
    terminal_scope_id: str | None = None,
    require_password_prompt: bool = True,
    tail_text: str | None = None,
) -> dict:
    """程序化注入：按 credential_id 查凭证库 → 写 PTY stdin。密码不出现在返回值中。供 AI 工具 / REST 共用。"""
    from services.password_prompt import looks_like_password_prompt

    if credential_id is None:
        return {"success": False, "error": "缺少 credential_id"}

    target = (target or "terminal").strip().lower()
    if require_password_prompt:
        if not tail_text:
            if target == "ssh_channel":
                if channel_id is None:
                    return {"success": False, "error": "ssh_channel 需要 channel_id"}
                from services.ssh_channel_manager import SSHChannelManager

                result = SSHChannelManager.get_instance().get_lines(int(channel_id), last_n=30)
                tail_text = "\n".join(result[0]) if result else ""
            elif target == "local_terminal":
                from api import local_host

                slot_val, slot_err = local_host.resolve_local_slot(
                    user["id"], terminal_scope_id, slot
                )
                if slot_err:
                    return {"success": False, "error": slot_err}
                tail_text, _ = local_host.get_local_terminal_buffer(
                    user["id"], slot_val, terminal_scope_id
                )
            else:
                from api.terminal import get_terminal_buffer_for_user, resolve_ai_slot

                if target == "terminal" and host_id is None:
                    return {"success": False, "error": "target=terminal 时需要 host_id（控制台所在主机）"}
                slot_val, slot_err = resolve_ai_slot(
                    user["id"], terminal_scope_id, slot, host_id
                )
                if slot_err:
                    return {"success": False, "error": slot_err}
                tail_text, _ = get_terminal_buffer_for_user(
                    user["id"], slot_val, scope_id=terminal_scope_id
                )
        if not looks_like_password_prompt(tail_text or ""):
            return {
                "success": False,
                "error": "终端尾部未检测到密码提示。请先发送命令并确认出现 password 提示后再注入。",
            }

    cred_row, pwd = await resolve_credential_for_injection(user, credential_id=int(credential_id))
    if not pwd:
        return {
            "success": False,
            "error": f"凭证 id={credential_id} 不存在、无权限或没有可用密码（has_password 为 false 时需 update/add 写入 password 或 linked_*）",
        }
    svc = (cred_row or {}).get("service") or "sudo"

    ok, msg = await inject_password_to_target(
        user,
        target=target,
        password=pwd,
        terminal_scope_id=terminal_scope_id,
        slot=slot,
        channel_id=channel_id,
        host_id=host_id,
    )
    if not ok:
        return {"success": False, "error": msg}
    cid = cred_row.get("id") if cred_row else int(credential_id)
    await log_credential_injection_audit(
        user_id=user["id"],
        host_id=int(host_id) if host_id is not None else None,
        credential_id=cid,
        target=target,
        service=svc,
    )
    return {
        "success": True,
        "message": msg,
        "credential_id": cid,
        "injected": True,
    }


async def log_credential_injection_audit(
    *,
    user_id: int,
    host_id: int | None,
    credential_id: int | None,
    target: str,
    service: str,
) -> None:
    try:
        db = await get_db()
        await db.execute(
            """INSERT INTO operation_logs (user_id, operation, params, result, source)
               VALUES (?, ?, ?, ?, ?)""",
            (
                user_id,
                "credential_vault:inject",
                json.dumps(
                    {
                        "host_id": host_id,
                        "credential_id": credential_id,
                        "target": target,
                        "service": service,
                    },
                    ensure_ascii=False,
                ),
                "success",
                "credential_vault",
            ),
        )
        await db.commit()
    except Exception as exc:
        logger.warning("credential injection audit failed: %s", exc)


async def build_credential_vault_system_section() -> str:
    """凭证库启用时注入 system 段落；关闭时返回空串。"""
    if not await credentials_vault_enabled():
        return ""
    return """
## 服务凭证库（已启用，高优先级）

凭证按用户保存；**密码仅存在于服务端**，AI **只能看元数据、不能读密码**，也**禁止在回复中向用户重复索要已保存的密码**。

### 设计原则（必须遵守）
1. **AI 负责决策**：查凭证表、与用户确认、选定 `credential_id`、选定注入目标（terminal / ssh_channel）。
2. **工具负责注入**：`send_service_password(credential_id, target, …)` 在服务端查库写 PTY stdin；**密码不出现在工具返回 JSON 与模型上下文中**。
3. **不要自动瞎猜**：不要跳过 list/确认直接靠 service+address 模糊匹配；不要 `send_to_terminal` 发明文密码。

### 可用工具
- `list_service_credentials` — 元数据（id、service、address、port、service_username、label、has_password）
- `add_service_credential` / `update_service_credential` / `delete_service_credential`
- `send_service_password` — **唯一**注入入口（须传 `credential_id` + `target`）

### 标准流程（Web 控制台 terminal）
1. `send_to_terminal` **仅**发命令（如 sudo、ssh user@host）
2. `get_terminal_buffer` 看末尾是否出现 password 提示
3. `list_service_credentials`（可按 service/address 过滤）→ 若无合适条目则 `add_service_credential`（用户口述时）或 `ask_user_choice`
4. `send_service_password(credential_id=…, target=terminal, host_id=控制台主机, slot=…)`

### 标准流程（ssh_channel，逻辑相同）
1. `ssh_channel_send` 发命令 → `ssh_channel_read_lines` 看末尾提示
2. `list_service_credentials` → 选定 `credential_id`
3. `send_service_password(credential_id=…, target=ssh_channel, channel_id=…)`

### 与 SSH 登录凭证（credentials 表）的区别
`credentials` + hosts = **毛竹 SSH 连主机**；本库 = **连上之后还要登录的服务**（sudo / 跳板 ssh / mysql 等）。
"""
