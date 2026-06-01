"""SSH 主机管理 API（主机与凭证分离：仅支持选择已有凭证或新建凭证并关联，不内联账号密码）"""
import asyncio
import json
import re
import secrets
import socket
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user, _is_admin_role
from services.ssh_client import run_ssh_command
from services.credential_utils import normalize_private_key_pem

router = APIRouter(prefix="/api/hosts", tags=["主机管理"])


def parse_host_aliases_cell(raw) -> list[str]:
    """将 hosts.aliases 列（JSON 数组字符串）解析为别名列表；非法或空则返回 []。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except json.JSONDecodeError:
        pass
    return [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]


def serialize_host_aliases_for_db(value) -> str:
    """将别名序列化为存入数据库的 JSON 数组字符串（去重、去空白、保序）。"""
    items: list[str] = []
    if value is None:
        items = []
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            items = []
        else:
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    items = [str(x).strip() for x in parsed if str(x).strip()]
                else:
                    items = [s]
            except json.JSONDecodeError:
                items = [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]
    elif isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return json.dumps(out, ensure_ascii=False)


def normalize_host_aliases_in_dict(d: dict) -> dict:
    """API 输出：将行字典中的 aliases 原始列转为 list[str]。"""
    out = dict(d)
    out["aliases"] = parse_host_aliases_cell(out.get("aliases"))
    return out


async def _fetch_user_host_tags_map(db, *, user_id: int, host_ids: list[int]) -> dict[int, list[dict]]:
    """按当前用户读取主机标签映射：host_id -> [{id,name,color}, ...]。"""
    valid_ids = [int(hid) for hid in host_ids if hid is not None]
    if not valid_ids:
        return {}
    placeholders = ",".join(["?"] * len(valid_ids))
    rows = await db.execute_fetchall(
        f"""SELECT hut.host_id, t.id, t.name, t.color
            FROM host_user_tags hut
            JOIN host_tags t ON t.id = hut.tag_id
            WHERE hut.user_id = ? AND t.created_by = ? AND hut.host_id IN ({placeholders})
            ORDER BY t.name COLLATE NOCASE, t.id""",
        [user_id, user_id, *valid_ids],
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        hid = int(d.get("host_id"))
        out.setdefault(hid, []).append(
            {"id": d.get("id"), "name": d.get("name") or "", "color": d.get("color") or ""}
        )
    return out


async def _attach_user_tags_to_hosts(db, hosts: list[dict], user_id: int) -> None:
    """为主机列表附加当前用户可见的标签字段：tags/tag_names。"""
    if not hosts:
        return
    host_ids: list[int] = []
    host_key_by_obj: list[int | None] = []
    for h in hosts:
        try:
            raw_id = h.get("id")
            if raw_id is None:
                raw_id = h.get("host_id")
            if raw_id is not None:
                hid = int(raw_id)
                host_ids.append(hid)
                host_key_by_obj.append(hid)
            else:
                host_key_by_obj.append(None)
        except (TypeError, ValueError):
            host_key_by_obj.append(None)
            continue
    tags_map = await _fetch_user_host_tags_map(db, user_id=user_id, host_ids=host_ids)
    for idx, h in enumerate(hosts):
        hid = host_key_by_obj[idx] if idx < len(host_key_by_obj) else None
        if hid is None:
            h["tags"] = []
            h["tag_names"] = []
            continue
        tags = tags_map.get(int(hid), [])
        h["tags"] = tags
        h["tag_names"] = [t.get("name") or "" for t in tags if (t.get("name") or "").strip()]


class NewCredentialCreate(BaseModel):
    """新建凭证并关联到主机时使用"""
    code: str
    name: str
    username: str = ""
    type: str = "password"
    description: str = ""
    password: Optional[str] = None
    public_key: Optional[str] = None
    private_key: Optional[str] = None


class HostCreate(BaseModel):
    name: str
    host: str
    port: int = 22
    credential_id: Optional[int] = None
    new_credential: Optional[NewCredentialCreate] = None
    description: str = ""
    aliases: Optional[list[str]] = None
    remark: str = ""


class HostUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    credential_id: Optional[int] = None
    username: Optional[str] = None
    auth_type: Optional[str] = None
    password: Optional[str] = None
    key_path: Optional[str] = None
    description: Optional[str] = None
    host_type: Optional[str] = None   # Linux / macOS / Windows / FreeBSD / OpenBSD / NetBSD / 未知
    host_version: Optional[str] = None  # 如 Ubuntu 22.04、Windows Server 2019
    host_shell: Optional[str] = None       # 如 bash / zsh / sh（供 AI 命令与脚本策略）
    host_package_manager: Optional[str] = None  # 如 apt / yum / apk（供 AI 安装命令策略）
    aliases: Optional[list[str]] = None  # 整列表替换；传 [] 可清空
    remark: Optional[str] = None


class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 30


class HostShareCreateRequest(BaseModel):
    user_id: Optional[int] = None
    username: Optional[str] = None


def _host_row_to_dict(row, mask_secret: bool = True):
    d = dict(row)
    if mask_secret and d.get("password_enc"):
        d["password_enc"] = "***"
    d["aliases"] = parse_host_aliases_cell(d.get("aliases"))
    return d


async def _resolve_host_auth(db, host_row: dict):
    """从主机行解析出 SSH 认证参数：username, auth_type, password, key_path, private_key_pem。"""
    credential_id = host_row.get("credential_id")
    if credential_id:
        rows = await db.execute_fetchall("SELECT * FROM credentials WHERE id = ?", (credential_id,))
        if not rows:
            return None
        cred = dict(rows[0])
        username = cred.get("username") or ""
        if cred.get("type") == "password":
            return {
                "username": username,
                "auth_type": "password",
                "password": cred.get("password_enc"),
                "key_path": None,
                "private_key_pem": None,
            }
        return {
            "username": username,
            "auth_type": "key_pair",
            "password": None,
            "key_path": None,
            "private_key_pem": cred.get("private_key_enc"),
        }
    username = host_row.get("username") or ""
    auth_type = host_row.get("auth_type") or "password"
    return {
        "username": username,
        "auth_type": auth_type,
        "password": host_row.get("password_enc") if auth_type == "password" else None,
        "key_path": host_row.get("key_path"),
        "private_key_pem": None,
    }


@router.get("")
async def list_hosts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
):
    db = await get_db()
    if _is_admin_role(user.get("role")):
        count_rows = await db.execute_fetchall("SELECT COUNT(*) FROM hosts")
        total = count_rows[0][0] if count_rows else 0
        rows = await db.execute_fetchall(
            """SELECT h.id, h.name, h.host, h.port, h.credential_id, h.username, h.auth_type, h.description,
                      h.aliases, h.remark,
                      h.host_type, h.host_version, h.host_shell, h.host_package_manager, h.created_at, h.created_by,
                      u.username AS created_by_username, u.display_name AS created_by_display_name,
                      0 AS is_shared, NULL AS shared_from_user_id, NULL AS shared_from_username, NULL AS shared_from_display_name,
                      c.code AS credential_code, c.name AS credential_name
               FROM hosts h
               LEFT JOIN credentials c ON h.credential_id = c.id
               LEFT JOIN users u ON h.created_by = u.id
               ORDER BY h.id LIMIT ? OFFSET ?""",
            (page_size, (page - 1) * page_size),
        )
    else:
        count_rows = await db.execute_fetchall(
            """SELECT COUNT(DISTINCT h.id) AS c
               FROM hosts h
               LEFT JOIN host_shares hs
                 ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
               WHERE h.created_by = ? OR hs.id IS NOT NULL""",
            (user["id"], user["id"]),
        )
        total = count_rows[0]["c"] if count_rows else 0
        rows = await db.execute_fetchall(
            """SELECT h.id, h.name, h.host, h.port, h.credential_id, h.username, h.auth_type, h.description,
                      h.aliases, h.remark,
                      h.host_type, h.host_version, h.host_shell, h.host_package_manager, h.created_at, h.created_by,
                      u.username AS created_by_username, u.display_name AS created_by_display_name,
                      CASE WHEN h.created_by = ? THEN 0 ELSE 1 END AS is_shared,
                      su.id AS shared_from_user_id, su.username AS shared_from_username, su.display_name AS shared_from_display_name,
                      c.code AS credential_code, c.name AS credential_name
               FROM hosts h
               LEFT JOIN host_shares hs
                 ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
               LEFT JOIN users su ON su.id = hs.owner_user_id
               LEFT JOIN credentials c ON h.credential_id = c.id
               LEFT JOIN users u ON h.created_by = u.id
               WHERE h.created_by = ? OR hs.id IS NOT NULL
               ORDER BY h.id LIMIT ? OFFSET ?""",
            (user["id"], user["id"], user["id"], page_size, (page - 1) * page_size),
        )
    host_items = [normalize_host_aliases_in_dict(dict(r)) for r in rows]
    await _attach_user_tags_to_hosts(db, host_items, int(user["id"]))
    return {
        "success": True,
        "hosts": host_items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats")
async def host_stats(user=Depends(get_current_user)):
    db = await get_db()
    if _is_admin_role(user.get("role")):
        cursor = await db.execute("SELECT COUNT(*) FROM hosts")
    else:
        cursor = await db.execute(
            """SELECT COUNT(DISTINCT h.id)
               FROM hosts h
               LEFT JOIN host_shares hs
                 ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
               WHERE h.created_by = ? OR hs.id IS NOT NULL""",
            (user["id"], user["id"]),
        )
    total = (await cursor.fetchone())[0]
    return {"success": True, "stats": {"total_hosts": total}}


def _is_owner_or_admin(host_row: dict, user: dict) -> bool:
    return _is_admin_role(user.get("role")) or (host_row.get("created_by") == user["id"])


async def _is_host_shared_with_user(db, host_id: int, user_id: int) -> bool:
    rows = await db.execute_fetchall(
        """SELECT 1 FROM host_shares
           WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL
           LIMIT 1""",
        (host_id, user_id),
    )
    return bool(rows)


async def _cleanup_shared_host_group_members(db, host_id: int, shared_user_id: int) -> None:
    """分享撤销后，清理接收方分组中的残留主机关联。"""
    await db.execute(
        """DELETE FROM host_group_members
           WHERE host_id = ?
             AND group_id IN (SELECT id FROM host_groups WHERE created_by = ?)""",
        (host_id, shared_user_id),
    )


async def _log_share_audit(
    db,
    *,
    actor_user_id: int,
    host_id: int | None,
    operation: str,
    params: dict | None = None,
    result: str = "success",
    source: str = "api",
) -> None:
    """记录主机分享审计日志（写入 operation_logs）。"""
    try:
        await db.execute(
            """INSERT INTO operation_logs (user_id, host_id, operation, params, result, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id,
                host_id,
                operation,
                json.dumps(params or {}, ensure_ascii=False),
                result,
                source,
            ),
        )
    except Exception:
        pass


async def _can_access_host(db, host_row: dict, user: dict) -> bool:
    if _is_owner_or_admin(host_row, user):
        return True
    hid = host_row.get("id")
    if not hid:
        return False
    return await _is_host_shared_with_user(db, int(hid), int(user["id"]))


def _check_host_alive(host: str, port: int, timeout: float = 3.0) -> bool:
    """TCP 探测主机:端口是否可达。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


@router.get("/shares/received")
async def list_received_host_shares(user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT hs.id, hs.host_id, hs.created_at,
                  h.name, h.host, h.port, h.aliases, h.remark,
                  ou.id AS owner_user_id, ou.username AS owner_username, ou.display_name AS owner_display_name
           FROM host_shares hs
           JOIN hosts h ON h.id = hs.host_id
           JOIN users ou ON ou.id = hs.owner_user_id
           WHERE hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
           ORDER BY hs.created_at DESC""",
        (user["id"],),
    )
    shares = [normalize_host_aliases_in_dict(dict(r)) for r in rows]
    await _attach_user_tags_to_hosts(db, shares, int(user["id"]))
    return {"success": True, "shares": shares}


@router.get("/shares/sent")
async def list_sent_host_shares(user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT hs.id, hs.host_id, hs.created_at, hs.shared_with_user_id,
                  h.name, h.host, h.port, h.aliases, h.remark,
                  ru.username AS shared_with_username, ru.display_name AS shared_with_display_name
           FROM host_shares hs
           JOIN hosts h ON h.id = hs.host_id
           JOIN users ru ON ru.id = hs.shared_with_user_id
           WHERE hs.owner_user_id = ? AND hs.revoked_at IS NULL
           ORDER BY hs.created_at DESC""",
        (user["id"],),
    )
    shares = [normalize_host_aliases_in_dict(dict(r)) for r in rows]
    await _attach_user_tags_to_hosts(db, shares, int(user["id"]))
    return {"success": True, "shares": shares}


@router.post("/{host_id}/shares")
async def share_host(host_id: int, req: HostShareCreateRequest, user=Depends(get_current_user)):
    db = await get_db()
    host_rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
    if not host_rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(host_rows[0])
    if not _is_owner_or_admin(host_row, user):
        raise HTTPException(status_code=403, detail="仅主机所有者可分享该主机")

    target_id = int(req.user_id) if req.user_id is not None else None
    target_username = (req.username or "").strip()
    if target_id is None and not target_username:
        raise HTTPException(status_code=400, detail="请提供 user_id 或 username")
    if target_id is not None:
        user_rows = await db.execute_fetchall(
            "SELECT id, username, display_name FROM users WHERE id = ?",
            (target_id,),
        )
    else:
        user_rows = await db.execute_fetchall(
            "SELECT id, username, display_name FROM users WHERE username = ?",
            (target_username,),
        )
    if not user_rows:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    target_user = dict(user_rows[0])
    target_id = int(target_user["id"])
    owner_id = int(host_row["created_by"])
    if target_id == owner_id:
        raise HTTPException(status_code=400, detail="无需将主机分享给自己")

    await db.execute(
        """INSERT INTO host_shares (host_id, owner_user_id, shared_with_user_id, created_by, created_at, revoked_at)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
           ON CONFLICT(host_id, shared_with_user_id) DO UPDATE SET
             owner_user_id = excluded.owner_user_id,
             created_by = excluded.created_by,
             created_at = CURRENT_TIMESTAMP,
             revoked_at = NULL""",
        (host_id, owner_id, target_id, user["id"]),
    )
    await _log_share_audit(
        db,
        actor_user_id=user["id"],
        host_id=host_id,
        operation="host_share_create",
        params={"host_id": host_id, "owner_user_id": owner_id, "shared_with_user_id": target_id},
    )
    await db.commit()
    return {
        "success": True,
        "host_id": host_id,
        "shared_with_user_id": target_id,
        "shared_with_username": target_user.get("username") or "",
        "shared_with_display_name": target_user.get("display_name") or "",
    }


@router.get("/{host_id}/shares")
async def list_host_shares(host_id: int, user=Depends(get_current_user)):
    db = await get_db()
    host_rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
    if not host_rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(host_rows[0])
    if not _is_owner_or_admin(host_row, user):
        raise HTTPException(status_code=403, detail="仅主机所有者可查看分享清单")
    rows = await db.execute_fetchall(
        """SELECT hs.id, hs.host_id, hs.owner_user_id, hs.shared_with_user_id, hs.created_at,
                  u.username AS shared_with_username, u.display_name AS shared_with_display_name
           FROM host_shares hs
           JOIN users u ON u.id = hs.shared_with_user_id
           WHERE hs.host_id = ? AND hs.revoked_at IS NULL
           ORDER BY hs.created_at DESC""",
        (host_id,),
    )
    return {"success": True, "shares": [dict(r) for r in rows]}


@router.delete("/{host_id}/shares/{target_user_id}")
async def revoke_host_share(host_id: int, target_user_id: int, user=Depends(get_current_user)):
    db = await get_db()
    host_rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
    if not host_rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(host_rows[0])
    is_owner = _is_owner_or_admin(host_row, user)
    is_self_revoke = int(target_user_id) == int(user["id"])
    if not is_owner and not is_self_revoke:
        raise HTTPException(status_code=403, detail="无权撤销该分享")
    rows = await db.execute_fetchall(
        """SELECT id FROM host_shares
           WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL""",
        (host_id, target_user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="分享记录不存在")
    await db.execute(
        "UPDATE host_shares SET revoked_at = CURRENT_TIMESTAMP WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL",
        (host_id, target_user_id),
    )
    await _cleanup_shared_host_group_members(db, host_id, target_user_id)
    await _log_share_audit(
        db,
        actor_user_id=user["id"],
        host_id=host_id,
        operation="host_share_revoke",
        params={"host_id": host_id, "target_user_id": target_user_id},
    )
    await db.commit()
    return {"success": True}


@router.get("/{host_id}/alive")
async def host_alive(host_id: int, user=Depends(get_current_user)):
    """探测主机是否存活（TCP 连接 SSH 端口，超时 3 秒）。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, host, port, created_by FROM hosts WHERE id = ?", (host_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    h = dict(rows[0])
    if not await _can_access_host(db, h, user):
        raise HTTPException(status_code=404, detail="主机不存在")
    alive = await asyncio.to_thread(
        _check_host_alive, h.get("host", ""), int(h.get("port") or 22), 3.0
    )
    return {"success": True, "alive": alive}


@router.get("/search")
async def search_hosts(
    query: str = Query(..., min_length=1, description="搜索关键字"),
    group_id: Optional[int] = Query(None, description="可选，按分组搜索"),
    tag_ids: Optional[list[int]] = Query(None, description="可选，按标签 ID 过滤（命中任一）"),
    regex: str = Query("", description="可选，正则精筛"),
    case_sensitive: bool = Query(False, description="regex 是否区分大小写"),
    limit: int = Query(50, ge=1, le=200, description="最多返回条数"),
    user=Depends(get_current_user),
):
    """高效主机检索：SQL 预筛 + 可选正则精筛。"""
    db = await get_db()
    q_raw = (query or "").strip()
    if not q_raw:
        raise HTTPException(status_code=400, detail="query 不能为空")
    tag_ids = sorted(set(int(x) for x in (tag_ids or []) if x is not None))

    sel_cols = """h.id, h.name, h.host, h.port, h.credential_id, h.username, h.auth_type, h.description,
                         h.aliases, h.remark,
                         h.host_type, h.host_version, h.host_shell, h.host_package_manager"""
    like = f"%{q_raw.lower()}%"
    search_where = """(
        LOWER(h.name) LIKE ?
        OR LOWER(h.host) LIKE ?
        OR CAST(h.port AS TEXT) LIKE ?
        OR LOWER(IFNULL(h.description,'')) LIKE ?
        OR LOWER(IFNULL(h.remark,'')) LIKE ?
        OR LOWER(IFNULL(h.aliases,'')) LIKE ?
        OR LOWER(IFNULL(h.host_type,'')) LIKE ?
        OR EXISTS (
            SELECT 1 FROM host_user_tags hutq
            JOIN host_tags tq ON tq.id = hutq.tag_id
            WHERE hutq.user_id = ? AND hutq.host_id = h.id
              AND tq.created_by = ? AND LOWER(tq.name) LIKE ?
        )
    )"""
    search_params: list = [like, like, like, like, like, like, like, user["id"], user["id"], like]
    if q_raw.isdigit():
        search_where = "(" + search_where + " OR h.id = ?)"
        try:
            search_params.append(int(q_raw))
        except ValueError:
            pass

    tag_filter_where = ""
    tag_filter_params: list = []
    if tag_ids:
        ph = ",".join(["?"] * len(tag_ids))
        tag_filter_where = f"EXISTS (SELECT 1 FROM host_user_tags hutf WHERE hutf.user_id = ? AND hutf.host_id = h.id AND hutf.tag_id IN ({ph}))"
        tag_filter_params = [user["id"], *tag_ids]

    def _combine_where(*parts: str) -> str:
        return " AND ".join([p for p in parts if p])

    if group_id is not None:
        if _is_admin_role(user.get("role")):
            where_clause = _combine_where("m.group_id = ?", search_where, tag_filter_where)
            params = [group_id, *search_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT {sel_cols}
                    FROM hosts h
                    INNER JOIN host_group_members m ON h.id = m.host_id
                    WHERE {where_clause}
                    ORDER BY h.name
                    LIMIT {int(limit)}""",
                params,
            )
        else:
            where_clause = _combine_where("m.group_id = ?", "(h.created_by = ? OR hs.id IS NOT NULL)", search_where, tag_filter_where)
            params = [user["id"], user["id"], group_id, user["id"], *search_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT {sel_cols}
                    FROM hosts h
                    INNER JOIN host_group_members m ON h.id = m.host_id
                    INNER JOIN host_groups hg ON hg.id = m.group_id AND hg.created_by = ?
                    LEFT JOIN host_shares hs
                      ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                    WHERE {where_clause}
                    ORDER BY h.name
                    LIMIT {int(limit)}""",
                params,
            )
    else:
        if _is_admin_role(user.get("role")):
            where_clause = _combine_where(search_where, tag_filter_where)
            params = [*search_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT {sel_cols}
                    FROM hosts h
                    WHERE {where_clause}
                    ORDER BY h.name
                    LIMIT {int(limit)}""",
                params,
            )
        else:
            where_clause = _combine_where("(h.created_by = ? OR hs.id IS NOT NULL)", search_where, tag_filter_where)
            params = [user["id"], user["id"], *search_params, *tag_filter_params]
            rows = await db.execute_fetchall(
                f"""SELECT DISTINCT {sel_cols}
                    FROM hosts h
                    LEFT JOIN host_shares hs
                      ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                    WHERE {where_clause}
                    ORDER BY h.name
                    LIMIT {int(limit)}""",
                params,
            )

    hosts = [normalize_host_aliases_in_dict(dict(r)) for r in rows]
    await _attach_user_tags_to_hosts(db, hosts, int(user["id"]))
    if regex:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            reg = re.compile(regex, flags)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"regex 非法: {e}")
        filtered: list[dict] = []
        for h in hosts:
            blob = " ".join([
                str(h.get("id") or ""),
                str(h.get("name") or ""),
                str(h.get("host") or ""),
                str(h.get("port") or ""),
                str(h.get("description") or ""),
                str(h.get("remark") or ""),
                " ".join(h.get("aliases") or []),
                " ".join(h.get("tag_names") or []),
            ])
            if reg.search(blob):
                filtered.append(h)
        hosts = filtered

    return {
        "success": True,
        "query": q_raw,
        "regex": regex or "",
        "group_id": group_id,
        "tag_ids": tag_ids,
        "count": len(hosts),
        "hosts": hosts,
    }


@router.get("/{host_id}")
async def get_host(host_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT h.*, c.code AS credential_code, c.name AS credential_name,
                  CASE WHEN h.created_by = ? THEN 0 ELSE 1 END AS is_shared,
                  su.id AS shared_from_user_id, su.username AS shared_from_username, su.display_name AS shared_from_display_name
           FROM hosts h
           LEFT JOIN credentials c ON h.credential_id = c.id
           LEFT JOIN host_shares hs
             ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
           LEFT JOIN users su ON su.id = hs.owner_user_id
           WHERE h.id = ?""",
        (user["id"], user["id"], host_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    h = dict(rows[0])
    if not await _can_access_host(db, h, user):
        raise HTTPException(status_code=404, detail="主机不存在")
    host_item = _host_row_to_dict(rows[0])
    await _attach_user_tags_to_hosts(db, [host_item], int(user["id"]))
    return {"success": True, "host": host_item}


def _make_inline_credential_code(host: str, port: int) -> str:
    return "host-{}-{}-{}".format(host.replace(".", "-"), port, secrets.token_hex(4))


@router.post("")
async def create_host(req: HostCreate, user=Depends(get_current_user)):
    try:
        db = await get_db()
        host_value = (req.host or "").strip()
        if not host_value:
            raise HTTPException(status_code=400, detail="主机地址不能为空")
        # 主机重复仅在「当前用户」范围内校验：不同用户允许添加同一 host:port。
        duplicate_rows = await db.execute_fetchall(
            """SELECT id FROM hosts
               WHERE created_by = ? AND port = ? AND lower(trim(host)) = lower(trim(?))
               LIMIT 1""",
            (user["id"], req.port, host_value),
        )
        if duplicate_rows:
            raise HTTPException(status_code=400, detail="同一用户下该主机地址和端口已存在")
        cred_id = None
        if req.credential_id:
            cred_rows = await db.execute_fetchall("SELECT id, created_by FROM credentials WHERE id = ?", (req.credential_id,))
            if not cred_rows:
                raise HTTPException(status_code=400, detail="所选凭证不存在")
            if not _is_admin_role(user.get("role")) and cred_rows[0]["created_by"] != user["id"]:
                raise HTTPException(status_code=403, detail="无权使用该凭证")
            cred_id = req.credential_id
        elif req.new_credential:
            nc = req.new_credential
            code = (nc.code or "").strip() or _make_inline_credential_code(req.host, req.port)
            name = (nc.name or "").strip() or ("{} 登录".format((req.name or req.host or "").strip()[:28]))
            desc = (nc.description or "").strip()
            username = (nc.username or "").strip()
            cred_type = (nc.type or "password").strip().lower()
            # 若提供了私钥或公钥却未声明 key_pair，按内容推断为公钥认证
            if cred_type not in ("key_pair", "key") and (nc.private_key or nc.public_key):
                cred_type = "key_pair"
            if cred_type in ("key_pair", "key"):
                priv = normalize_private_key_pem(nc.private_key) or ""
                if not priv:
                    raise HTTPException(status_code=400, detail="新建密钥凭证需填写私钥内容")
                pub = (nc.public_key or "").strip() or None
                await db.execute(
                    """INSERT INTO credentials (type, code, name, description, username, key_type, key_bits, public_key, private_key_enc, created_by)
                       VALUES ('key_pair', ?, ?, ?, ?, 'RSA', 2048, ?, ?, ?)""",
                    (code, name, desc, username, pub, priv, user["id"]),
                )
            else:
                pw = nc.password or ""
                if not username:
                    raise HTTPException(status_code=400, detail="新建密码凭证需填写用户名")
                if not pw:
                    raise HTTPException(status_code=400, detail="新建密码凭证需填写密码")
                await db.execute(
                    """INSERT INTO credentials (type, code, name, description, username, password_enc, created_by)
                       VALUES ('password', ?, ?, ?, ?, ?, ?)""",
                    (code, name, desc, username, pw, user["id"]),
                )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            cred_id = (await cur.fetchone())[0]
        else:
            raise HTTPException(status_code=400, detail="请选择已有凭证或新建凭证并关联")
        # 主机与凭证分离：仅存 credential_id；username 列保留兼容旧库 NOT NULL，新逻辑填空串
        aliases_json = serialize_host_aliases_for_db(req.aliases)
        remark_val = (req.remark or "").strip()
        await db.execute(
            """INSERT INTO hosts (name, host, port, credential_id, username, description, aliases, remark, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.name or host_value, host_value, req.port, cred_id, "", req.description or "", aliases_json, remark_val, user["id"]),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        new_id = (await cur.fetchone())[0]
        return {"success": True, "id": new_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="添加主机失败: {}".format(str(e)))


@router.put("/{host_id}")
async def update_host(
    host_id: int, req: HostUpdate, user=Depends(get_current_user)
):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(rows[0])
    if not _is_owner_or_admin(host_row, user):
        raise HTTPException(status_code=403, detail="仅主机所有者可修改主机信息")

    updates, params = [], []
    for field in ("name", "host", "port", "credential_id", "username", "auth_type", "key_path", "description", "host_type", "host_version", "host_shell", "host_package_manager"):
        v = getattr(req, field, None)
        if v is not None:
            updates.append(f"{field} = ?")
            params.append(v)
    if req.credential_id is not None and req.credential_id == 0:
        updates.append("credential_id = ?")
        params.append(None)
    if req.password is not None:
        updates.append("password_enc = ?")
        params.append(req.password)
    if req.aliases is not None:
        updates.append("aliases = ?")
        params.append(serialize_host_aliases_for_db(req.aliases))
    if req.remark is not None:
        updates.append("remark = ?")
        params.append((req.remark or "").strip())
    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(host_id)
        await db.execute(
            f"UPDATE hosts SET {', '.join(updates)} WHERE id = ?", params
        )
        await db.commit()
    return {"success": True}


@router.delete("/{host_id}")
async def delete_host(host_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(rows[0])
    if _is_owner_or_admin(host_row, user):
        await db.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        await db.commit()
        return {"success": True, "deleted": True}
    if not await _is_host_shared_with_user(db, host_id, user["id"]):
        raise HTTPException(status_code=404, detail="主机不存在")
    await db.execute(
        "UPDATE host_shares SET revoked_at = CURRENT_TIMESTAMP WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL",
        (host_id, user["id"]),
    )
    await _cleanup_shared_host_group_members(db, host_id, user["id"])
    await _log_share_audit(
        db,
        actor_user_id=user["id"],
        host_id=host_id,
        operation="host_share_detach",
        params={"host_id": host_id, "shared_user_id": user["id"]},
    )
    await db.commit()
    return {"success": True, "detached": True}


@router.post("/{host_id}/execute")
async def execute_command(
    host_id: int, req: ExecuteRequest, user=Depends(get_current_user)
):
    """在目标主机上执行 SSH 命令"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM hosts WHERE id = ?", (host_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(rows[0])
    if not await _can_access_host(db, host_row, user):
        raise HTTPException(status_code=404, detail="主机不存在")
    auth = await _resolve_host_auth(db, host_row)
    if not auth or not auth.get("username"):
        raise HTTPException(status_code=400, detail="主机未配置有效登录凭证")

    try:
        out, err, code = await run_ssh_command(
            host=host_row["host"],
            port=host_row["port"] or 22,
            username=auth["username"],
            auth_type=auth["auth_type"],
            password=auth.get("password"),
            key_path=auth.get("key_path"),
            private_key_pem=auth.get("private_key_pem"),
            command=req.command,
            timeout=req.timeout,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SSH 执行失败: {str(e)}")

    await db.execute(
        """INSERT INTO operation_logs (user_id, host_id, operation, params, result)
           VALUES (?, ?, ?, ?, ?)""",
        (user["id"], host_id, "execute", req.command, f"code={code}"),
    )
    await db.commit()

    return {
        "success": True,
        "stdout": out,
        "stderr": err,
        "exit_code": code,
    }


@router.post("/{host_id}/check-type")
async def check_host_type(host_id: int, user=Depends(get_current_user)):
    """检测主机操作系统类型、版本、Shell、包管理器并写回主机信息；供 AI 优化命令与脚本策略。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM hosts WHERE id = ?", (host_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(rows[0])
    if not _is_owner_or_admin(host_row, user):
        raise HTTPException(status_code=403, detail="仅主机所有者可更新主机系统信息")
    auth = await _resolve_host_auth(db, host_row)
    if not auth or not auth.get("username"):
        raise HTTPException(status_code=400, detail="主机未配置有效登录凭证")

    from services.host_detection import detect_host_env

    try:
        env = await detect_host_env(
            host=host_row["host"],
            port=int(host_row.get("port") or 22),
            username=auth["username"],
            auth_type=auth.get("auth_type") or "password",
            password=auth.get("password"),
            key_path=auth.get("key_path"),
            private_key_pem=auth.get("private_key_pem"),
            timeout=15,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"检测失败: {str(e)}")

    host_type = env.get("host_type") or "未知"
    host_version = env.get("host_version") or "未知"
    host_shell = env.get("shell") or None
    host_package_manager = env.get("package_manager") or None

    await db.execute(
        """UPDATE hosts SET host_type = ?, host_version = ?, host_shell = ?, host_package_manager = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (host_type, host_version, host_shell, host_package_manager, host_id),
    )
    await db.commit()

    await db.execute(
        """INSERT INTO operation_logs (user_id, host_id, operation, params, result, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user["id"], host_id, "check_host_type", "{}", f"type={host_type} version={host_version} shell={host_shell or '-'} pkg={host_package_manager or '-'}", "api"),
    )
    await db.commit()

    return {
        "success": True,
        "host_type": host_type,
        "host_version": host_version,
        "host_shell": host_shell,
        "host_package_manager": host_package_manager,
    }
