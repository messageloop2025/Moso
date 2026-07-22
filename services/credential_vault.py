"""服务凭证库：按用户保存远程服务登录信息，供 AI 在密码提示时自动注入 stdin（不可查询明文）。"""
from __future__ import annotations

import json
import logging
import re
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
    "linked_credential_id, linked_host_id, created_at, updated_at, last_accessed_at"
)

# 列表/详情 SELECT：含 password_enc 存在性，但不向 API 返回明文
_SELECT_PUBLIC = (
    _PUBLIC_COLUMNS
    + ", (CASE WHEN COALESCE(TRIM(password_enc), '') != '' THEN 1 ELSE 0 END) AS _pwd_present"
)

_SORT_COLUMNS = {
    "last_accessed_at": "COALESCE(last_accessed_at, created_at)",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "service": "lower(service)",
    "address": "lower(address)",
    "service_username": "lower(service_username)",
    "id": "id",
}

_COMMAND_SERVICE_TOKENS: dict[str, str] = {
    "ssh": "ssh",
    "scp": "ssh",
    "sftp": "ssh",
    "rsync": "ssh",
    "mysql": "mysql",
    "mysqldump": "mysql",
    "mariadb": "mysql",
    "psql": "postgres",
    "pg_dump": "postgres",
    "redis-cli": "redis",
    "mongo": "mongodb",
    "mongosh": "mongodb",
    "ftp": "ftp",
    "lftp": "ftp",
    "sudo": "sudo",
}


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
    has_stored = bool(row.get("_pwd_present")) or bool(_norm(row.get("password_enc") or ""))
    has_link = bool(row.get("linked_host_id") or row.get("linked_credential_id"))
    d.pop("password_enc", None)
    d.pop("_pwd_present", None)
    d["has_password"] = has_stored or has_link
    return d


def infer_credential_hints_from_command(command: str) -> dict[str, Any]:
    """从待执行命令推断可能匹配的 service/address/port/username（供 AI 搜索凭证）。"""
    cmd = (command or "").strip()
    if not cmd:
        return {}
    lower = cmd.lower()
    parts = lower.split()
    first = parts[0] if parts else ""
    service = _COMMAND_SERVICE_TOKENS.get(first)
    if "sudo" in lower and not service:
        service = "sudo"

    address: str | None = None
    port: int | None = None
    service_username: str | None = None

    user_host = re.search(
        r"(?:@|^|\s)([A-Za-z0-9._-]+)@"
        r"([A-Za-z0-9._-]+(?:\.[A-Za-z0-9._-]+)*|\d{1,3}(?:\.\d{1,3}){3})",
        cmd,
    )
    if user_host:
        service = service or "ssh"
        service_username = user_host.group(1)
        address = user_host.group(2)

    host_flag = re.search(r"(?:^|\s)-h\s+(\S+)", cmd, re.I)
    if host_flag:
        address = address or host_flag.group(1)
        if not service and host_flag:
            service = service or "mysql"

    user_flag = re.search(r"(?:^|\s)-u\s+(\S+)", cmd, re.I)
    if user_flag:
        service_username = service_username or user_flag.group(1)

    port_cap = re.search(r"(?:^|\s)-P\s+(\d+)(?:\s|$)", cmd)
    if port_cap:
        port = int(port_cap.group(1))

    ssh_port = re.search(r"(?:^|\s)-p\s+(\d+)(?:\s|$)", cmd, re.I)
    if ssh_port and (service in (None, "ssh") or first in ("ssh", "scp", "sftp", "rsync")):
        service = service or "ssh"
        port = port or int(ssh_port.group(1))

    if not address and first in ("ssh", "scp", "sftp", "rsync"):
        bare = re.search(
            r"(?:^|\s)(?:ssh|scp|sftp|rsync)\s+(?:[^\s]+\s+)?([A-Za-z0-9._-]+(?:\.[A-Za-z0-9._-]+)*|\d{1,3}(?:\.\d{1,3}){3})(?:\s|:|$)",
            cmd,
            re.I,
        )
        if bare and "@" not in bare.group(1):
            service = service or "ssh"
            address = bare.group(1)

    out: dict[str, Any] = {}
    if service:
        out["service"] = service
    if address:
        out["address"] = address
    if port is not None:
        out["port"] = port
    if service_username:
        out["service_username"] = service_username
    return out


def _normalize_sort(sort_by: str | None, sort_order: str | None) -> tuple[str, str]:
    key = (sort_by or "last_accessed_at").strip().lower()
    if key not in _SORT_COLUMNS:
        key = "last_accessed_at"
    order = (sort_order or "desc").strip().lower()
    if order not in ("asc", "desc"):
        order = "desc"
    return key, order


def _credential_identity_key(row: dict) -> tuple:
    svc = _norm(row.get("service")).lower() or "other"
    addr = _norm_addr(row.get("address"))
    uname = _norm(row.get("service_username")).lower()
    return (svc, addr, effective_port(svc, row.get("port")), uname)


def _credential_recency_key(row: dict) -> tuple:
    la = row.get("last_accessed_at") or ""
    ua = row.get("updated_at") or ""
    ca = row.get("created_at") or ""
    rid = int(row.get("id") or 0)
    return (str(la), str(ua), str(ca), rid)


def dedupe_credentials_keep_newest(items: list[dict]) -> list[dict]:
    """同一 service+address+port+service_username 仅保留最新一条。"""
    best: dict[tuple, dict] = {}
    for row in items:
        k = _credential_identity_key(row)
        if k not in best or _credential_recency_key(row) > _credential_recency_key(best[k]):
            best[k] = row
    out = list(best.values())
    out.sort(key=_credential_recency_key, reverse=True)
    return out


def _credential_bound_to_host(row: dict, host_id: int) -> bool:
    """凭证是否明确绑定到某主机（host_id 或 linked_host_id）。"""
    try:
        hid = int(host_id)
    except (TypeError, ValueError):
        return False
    for key in ("linked_host_id", "host_id"):
        val = row.get(key)
        if val is None or val == "":
            continue
        try:
            if int(val) == hid:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _prefer_host_bound_credentials(items: list[dict], host_id: int | None) -> list[dict]:
    """本机 sudo 场景：优先返回绑定当前主机的凭证，再按最近使用排序。"""
    if host_id is None or not items:
        return items
    bound = [i for i in items if _credential_bound_to_host(i, int(host_id))]
    unbound = [i for i in items if not _credential_bound_to_host(i, int(host_id))]
    bound.sort(key=_credential_recency_key, reverse=True)
    unbound.sort(key=_credential_recency_key, reverse=True)
    return bound + unbound


def apply_credential_resolution(
    result: dict[str, Any],
    items: list[dict],
    *,
    inferred: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据去重后的候选集写入 resolution / suggested_credential_id / need_user_choice。"""
    inferred = inferred or {}
    host_id = inferred.get("host_id")
    items = _prefer_host_bound_credentials(items, host_id)
    deduped = dedupe_credentials_keep_newest(items)
    # 去重后再按主机绑定优先排一次，保证 suggested 优先本机
    deduped = _prefer_host_bound_credentials(deduped, host_id)
    result["credentials"] = deduped
    result["count"] = len(deduped)
    if items and len(items) != len(deduped):
        result["deduped_from"] = len(items)

    svc = inferred.get("service") or (deduped[0].get("service") if len(deduped) == 1 else None)
    addr = inferred.get("address") or (deduped[0].get("address") if len(deduped) == 1 else None)

    if not deduped:
        cmd_lower = str(inferred.get("command_hint") or "").lower()
        is_sudo_ctx = (svc or "").lower() == "sudo" or "sudo" in cmd_lower
        is_cross_ssh = (svc or "").lower() == "ssh" and bool(addr)
        if is_sudo_ctx and not is_cross_ssh:
            hid = inferred.get("host_id")
            result["resolution"] = "try_linked_host_or_execute"
            result["need_user_choice"] = False
            result["use_host_login"] = True
            result["choice_hint"] = (
                "未找到绑定当前主机的 sudo 服务凭证。**禁止**向用户说「没有 sudo 密码」，"
                "**禁止**改用其它主机 / mysql / 无关凭证，**禁止**sudo 后默认注入密码。"
                "请静默：① 执行 sudo 并 **read 输出**（很多环境 NOPASSWD 免密）；"
                "② **仅当**尾部出现密码提示 → `send_service_password(use_host_login=true, host_id=当前, target=…)`；"
                "③ 无密码提示且命令已继续 → 成功，勿注入；"
                "④ sudo 权限不足再试 su；仅均失败才 ask_user_choice。"
            )
            if hid is not None:
                result["suggested_linked_host_id"] = int(hid)
            return result
        result["resolution"] = "ask_user_identity"
        result["need_user_choice"] = False
        result["choice_hint"] = (
            f"未找到 service={svc or '?'} address={addr or '?'} 的服务凭证。"
            "请 ask_user_choice：① 用户指定登录用户名；② 使用当前控制台/会话用户名（whoami，勿臆测其它默认用户）；"
            "选定后 add_service_credential 保存，或确认无密码后执行。"
        )
        return result

    if len(deduped) == 1:
        result["resolution"] = "use_credential"
        result["suggested_credential_id"] = deduped[0].get("id")
        result["need_user_choice"] = False
        bound_note = ""
        if host_id is not None and _credential_bound_to_host(deduped[0], int(host_id)):
            bound_note = "（已绑定当前主机）"
            result["use_host_login"] = False
        result["choice_hint"] = (
            f"仅 1 条匹配凭证 id={deduped[0].get('id')} "
            f"({deduped[0].get('service_username') or '无用户名'}){bound_note}，"
            "可直接用于 send_service_password。"
        )
        return result

    # 多身份：若有且仅有一条绑定当前主机，自动建议该条，减少乱选
    if host_id is not None:
        bound = [d for d in deduped if _credential_bound_to_host(d, int(host_id))]
        if len(bound) == 1:
            result["resolution"] = "use_credential"
            result["suggested_credential_id"] = bound[0].get("id")
            result["need_user_choice"] = False
            result["choice_hint"] = (
                f"多条候选中仅 id={bound[0].get('id')} 绑定当前 host_id={host_id}，"
                "本机 sudo/su **必须**用此凭证（或 use_host_login=true）；勿选用其它主机凭证。"
            )
            return result

    result["resolution"] = "user_choice"
    result["need_user_choice"] = True
    result["choice_hint"] = (
        f"匹配到 {len(deduped)} 条不同登录身份的服务凭证（已合并同用户重复项，各保留最新）。"
        "请 ask_user_choice 展示 id、service_username、label、has_password，让用户选定 credential_id。"
        "本机 sudo 优先选 linked_host_id/host_id=当前主机 的条目，或 use_host_login=true。"
    )
    return result


async def search_credentials_for_user(
    user_id: int,
    *,
    service: str | None = None,
    address: str | None = None,
    port: int | None = None,
    service_username: str | None = None,
    host_id: int | None = None,
    credential_id: int | None = None,
    keyword: str | None = None,
    command_hint: str | None = None,
    sort_by: str | None = "last_accessed_at",
    sort_order: str | None = "desc",
    limit: int | None = 50,
    resolve: bool = True,
) -> dict[str, Any]:
    """搜索/列出凭证元数据；支持 command_hint 推断过滤、去重取最新、自动 resolution。"""
    inferred: dict[str, Any] = {}
    if command_hint:
        inferred = infer_credential_hints_from_command(command_hint)
        service = service or inferred.get("service")
        if address is None and inferred.get("address"):
            address = inferred.get("address")
        if port is None and inferred.get("port") is not None:
            port = inferred.get("port")
        # 选凭证阶段不按命令里的 user@ 过滤：先列出该 IP+service 下全部身份，避免默认当前机用户
        if not service_username and inferred.get("service_username"):
            inferred["command_username"] = inferred.get("service_username")

    db = await get_db()
    sql = f"SELECT {_SELECT_PUBLIC} FROM host_service_credentials WHERE user_id = ?"
    params: list[Any] = [user_id]

    if credential_id is not None:
        sql += " AND id = ?"
        params.append(int(credential_id))
    if service:
        sql += " AND lower(service) = lower(?)"
        params.append(_norm(service))
    if address is not None:
        addr = _norm_addr(address)
        if addr:
            sql += " AND lower(address) = lower(?)"
            params.append(addr)
        elif service and _norm(service).lower() == "sudo":
            sql += " AND (address = '' OR address IS NULL)"
    if service_username:
        sql += " AND lower(service_username) = lower(?)"
        params.append(_norm(service_username))
    if host_id is not None:
        svc_lower = _norm(service).lower() if service else ""
        if svc_lower == "sudo":
            # 本机 sudo：只匹配明确绑定当前主机的凭证，禁止「空 address 全局 sudo」跨机污染
            sql += " AND (host_id = ? OR linked_host_id = ?)"
            params.extend([int(host_id), int(host_id)])
        else:
            sql += " AND host_id = ?"
            params.append(int(host_id))

    kw = _norm(keyword)
    if kw:
        like = f"%{kw.lower()}%"
        sql += (
            " AND (CAST(id AS TEXT) LIKE ? OR lower(address) LIKE ?"
            " OR lower(service_username) LIKE ? OR lower(label) LIKE ?"
            " OR lower(notes) LIKE ? OR lower(service) LIKE ?)"
        )
        params.extend([like, like, like, like, like, like])

    sort_key, sort_ord = _normalize_sort(sort_by, sort_order)
    sql += f" ORDER BY {_SORT_COLUMNS[sort_key]} {sort_ord.upper()}, id DESC"

    lim = 50 if limit is None else max(1, min(int(limit), 200))
    sql += " LIMIT ?"
    params.append(lim)

    rows = await db.execute_fetchall(sql, params)
    items = [row_to_public_dict(dict(r)) for r in rows]
    if port is not None:
        items = [i for i in items if ports_match(i.get("service"), i.get("port"), port)]
    if host_id is not None and service and _norm(service).lower() == "sudo":
        items = _prefer_host_bound_credentials(items, int(host_id))

    result: dict[str, Any] = {
        "credentials": items,
        "count": len(items),
    }
    if inferred:
        result["inferred_hints"] = inferred
    if host_id is not None:
        inferred = {**inferred, "host_id": int(host_id)}
        result["host_id"] = int(host_id)
    if resolve and (command_hint or service or address or host_id is not None):
        apply_credential_resolution(result, items, inferred=inferred)
    elif command_hint and len(items) > 1:
        result["need_user_choice"] = True
        result["resolution"] = "user_choice"
        result["choice_hint"] = "匹配到多条凭证，请 ask_user_choice 让用户选定 credential_id"
    elif command_hint and len(items) == 1:
        result["suggested_credential_id"] = items[0].get("id")
        result["resolution"] = "use_credential"
    return result


async def get_credential_for_user(user_id: int, credential_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        f"SELECT {_SELECT_PUBLIC} FROM host_service_credentials WHERE id = ? AND user_id = ?",
        (credential_id, user_id),
    )
    return row_to_public_dict(dict(rows[0])) if rows else None


async def touch_credential_access(user_id: int, credential_id: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE host_service_credentials SET last_accessed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND user_id = ?",
        (int(credential_id), user_id),
    )
    await db.commit()


async def list_credentials_for_user(
    user_id: int,
    *,
    service: str | None = None,
    address: str | None = None,
    port: int | None = None,
    service_username: str | None = None,
    host_id: int | None = None,
    keyword: str | None = None,
    command_hint: str | None = None,
    sort_by: str | None = "last_accessed_at",
    sort_order: str | None = "desc",
    limit: int | None = 50,
) -> list[dict]:
    result = await search_credentials_for_user(
        user_id,
        service=service,
        address=address,
        port=port,
        service_username=service_username,
        host_id=host_id,
        keyword=keyword,
        command_hint=command_hint,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
    )
    return result["credentials"]


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
        f"SELECT {_SELECT_PUBLIC} FROM host_service_credentials WHERE id = ? AND user_id = ?",
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
        f"SELECT {_SELECT_PUBLIC} FROM host_service_credentials WHERE id = ? AND user_id = ?",
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


def _ssh_channel_tail_text(channel_id: int, *, last_n: int = 30) -> str:
    """读取 SSH 通道末尾（含无换行的 password: 提示）。"""
    from services.ssh_channel_manager import SSHChannelManager

    return SSHChannelManager.get_instance().get_tail_text(int(channel_id), last_n=last_n) or ""


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
    credential_id: int | None = None,
    target: str = "terminal",
    host_id: int | None = None,
    slot: int | None = None,
    channel_id: int | None = None,
    terminal_scope_id: str | None = None,
    require_password_prompt: bool = True,
    tail_text: str | None = None,
    use_host_login: bool = False,
) -> dict:
    """程序化注入：按 credential_id 或当前主机 SSH 登录密码写 PTY stdin。密码不出现在返回值中。

    默认 require_password_prompt=True：终端/通道尾部必须像密码提示才注入，避免 sudo 免密时误发密码。
    """
    from services.password_prompt import looks_like_password_prompt

    target = (target or "terminal").strip().lower()
    use_host_login = bool(use_host_login)

    if credential_id is None and not use_host_login:
        return {
            "success": False,
            "error": (
                "缺少 credential_id。本机 sudo/su 可传 use_host_login=true 与 host_id，"
                "注入当前主机 SSH 登录密码；或先 list_service_credentials 选定 credential_id。"
            ),
        }
    if use_host_login and host_id is None:
        return {"success": False, "error": "use_host_login=true 时必须提供 host_id（当前控制台/通道所在主机）"}

    if require_password_prompt:
        if not tail_text:
            if target == "ssh_channel":
                if channel_id is None:
                    return {"success": False, "error": "ssh_channel 需要 channel_id"}
                from services.ssh_channel_service import reconcile_channel_if_stale

                db = await get_db()
                await reconcile_channel_if_stale(db, user, int(channel_id))
                tail_text = _ssh_channel_tail_text(int(channel_id))
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
                "error": (
                    "终端尾部未检测到密码提示，已拒绝注入。"
                    "sudo/su **经常免密（NOPASSWD）**：须先 read 输出；"
                    "仅当出现 `[sudo] password for` / `Password:` / `口令：` 等提示时再调用本工具。"
                    "若命令已继续或回到 shell 提示符，视为无需密码，勿注入。"
                ),
                "password_prompt_detected": False,
            }

    cred_row: dict | None = None
    pwd: str | None = None
    svc = "sudo"
    cid: int | None = None
    source = "credential"

    if use_host_login:
        db = await get_db()
        pwd = await _resolve_login_password_for_host(db, int(host_id))
        if not pwd:
            return {
                "success": False,
                "error": (
                    f"主机 id={host_id} 无可用 SSH 登录密码（可能为密钥登录或未配置密码认证）。"
                    "请 add_service_credential 写入独立密码，或改用密钥/其它方式。"
                ),
            }
        source = "host_login"
        svc = "sudo"
    else:
        cred_row, pwd = await resolve_credential_for_injection(user, credential_id=int(credential_id))
        if not pwd:
            return {
                "success": False,
                "error": (
                    f"凭证 id={credential_id} 不存在、无权限或没有可用密码"
                    "（has_password 为 false 时需 update/add 写入 password 或 linked_*）"
                ),
            }
        svc = (cred_row or {}).get("service") or "sudo"
        cid = int(cred_row.get("id")) if cred_row else int(credential_id)
        # 本机 sudo/su：拒绝注入未绑定当前主机的凭证，避免乱选其它机/其它服务密码
        if host_id is not None and (svc or "").lower() in ("sudo", "su"):
            if cred_row and not _credential_bound_to_host(cred_row, int(host_id)):
                return {
                    "success": False,
                    "error": (
                        f"凭证 id={cid} 未绑定 host_id={host_id}（linked_host_id/host_id 均不匹配）。"
                        "本机 sudo/su **禁止**使用其它主机或未绑定凭证。"
                        "请改用 send_service_password(use_host_login=true, host_id=…) "
                        "或 list_service_credentials(service=sudo, host_id=…) 选绑定本机的条目。"
                    ),
                    "use_host_login": True,
                    "suggested_host_id": int(host_id),
                }

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
    if cid is not None:
        await touch_credential_access(user["id"], int(cid))
    await log_credential_injection_audit(
        user_id=user["id"],
        host_id=int(host_id) if host_id is not None else None,
        credential_id=cid,
        target=target,
        service=svc if source == "credential" else f"{svc}:host_login",
    )
    out: dict[str, Any] = {
        "success": True,
        "message": msg,
        "injected": True,
        "password_source": source,
    }
    if cid is not None:
        out["credential_id"] = cid
    if use_host_login and host_id is not None:
        out["host_id"] = int(host_id)
    return out


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
1. **先查凭证、再定身份、再执行**：从 A 机 SSH/MySQL/SCP 等到 B 机时，**先** `list_service_credentials(service=…, address=目标IP, command_hint=待执行命令)`；**禁止**默认用当前控制台登录用户名充当目标机 SSH 用户。
2. **SCP/SFTP/rsync 与 SSH 共用凭证**：这些命令本质走 SSH，查凭证时 **`service=ssh`**（`command_hint` 含 scp 时会自动推断为 ssh）。
3. **resolution 字段**：`use_credential`→`suggested_credential_id`；`try_linked_host_or_execute`→本机 sudo 无绑定凭证，出现提示后用 **`send_service_password(use_host_login=true, host_id=当前主机)`**；`user_choice`→**ask_user_choice**；`ask_user_identity`→跨机无凭证时问用户身份后再 add。
4. **同用户重复凭证**：按 service+address+port+username **去重保留最新**。
5. **注入**：本机 sudo 优先 `use_host_login=true`；跨机用选定的 `credential_id`。密码不进模型上下文；勿 send 发明文。

### 可用工具
- `list_service_credentials` — 本机 sudo **必须**带 `host_id`（只返回绑定该主机的条目）
- `add_service_credential` / `update_service_credential` / `delete_service_credential`
- `send_service_password` — `credential_id` **或** `use_host_login=true`+`host_id`

### 跨机登录选凭证（SSH / SCP / MySQL 等）
1. 明确 **目标 IP/域名** 与 **服务类型**（scp→ssh；mysql→mysql）
2. `list_service_credentials(command_hint="ssh 172.31.0.1" 或 "scp … user@172.31.0.1:…")` 或 `service="ssh", address="172.31.0.1"`
3. 看 `resolution`：
   - **use_credential**：用 `suggested_credential_id` 构造 `ssh user@host` 或注入密码
   - **user_choice**：ask_user_choice 列出各条 `service_username` / label / id
   - **ask_user_identity**：ask_user_choice（指定用户名 | 使用当前控制台 whoami 用户名）→ 无则 add_service_credential
4. 出现 password 提示 → `send_service_password(credential_id, target=…)`

### 标准流程（ssh_channel）
1. `ssh_channel_send` 发 **ssh/scp 等命令**（用户名应来自上一步选定的凭证，勿臆测）
2. `ssh_channel_read_lines` 确认交互状态
3. `list_service_credentials` 选 credential_id
4. `send_service_password(credential_id, target=ssh_channel, channel_id=…)`

### 本机 sudo / su（当前控制台主机，高优先级）
1. **sudo 不总是要密码**（常见 NOPASSWD）。**禁止**发完 sudo 就默认调用 `send_service_password`。
2. **强制观察**：send sudo → **必须 read**（可用 `until_contains="password"`，但超时无提示也要看完整尾部）。
3. **仅当**尾部出现 `[sudo] password for` / `Password:` / `口令：` 等 → 再注入：  
   - 有本机绑定 `credential_id` → `send_service_password(credential_id=…, host_id=当前, target=…)`  
   - 否则 → `send_service_password(use_host_login=true, host_id=当前, target=…)`  
   服务端默认会校验密码提示，无提示则拒绝注入。
4. **无密码提示**且命令继续 / 回到 `#`/`$` → 免密成功，**勿**注入、**勿**向用户索要密码。
5. sudo 权限不足再试 su（同样先 read 再决定是否注入）。仅均失败才 `ask_user_choice`。
6. **禁止**用其它主机 / mysql / 未绑定本机的凭证。

### 与 SSH 登录凭证（credentials 表）的区别
`credentials` + hosts = **毛竹 SSH 连主机**；本库 = **连上之后还要登录的服务**。本机 sudo 仅在**确认密码提示后**用 `use_host_login` 或绑定本机的 credential。
"""
