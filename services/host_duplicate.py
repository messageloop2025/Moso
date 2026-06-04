"""主机重复检测：同一所有者下地址（忽略大小写/首尾空白）+ 端口一致视为重复。"""

from typing import Any, Optional

DUPLICATE_HOST_SQL = """
    SELECT h.id, h.name, h.host, h.port, h.created_by,
           u.username AS created_by_username,
           u.display_name AS created_by_display_name
    FROM hosts h
    LEFT JOIN users u ON h.created_by = u.id
    WHERE h.created_by = ? AND h.port = ? AND lower(trim(h.host)) = lower(trim(?))
    LIMIT 1
"""


def normalize_host_address(host: str) -> str:
    return (host or "").strip()


def normalize_host_port(port: Any = None) -> int:
    if port is None or port == "":
        return 22
    try:
        p = int(port)
    except (TypeError, ValueError):
        return 22
    return p if 1 <= p <= 65535 else 22


def row_to_duplicate_host_payload(row) -> dict:
    d = dict(row)
    return {
        "id": d.get("id"),
        "name": d.get("name") or "",
        "host": d.get("host") or "",
        "port": d.get("port") if d.get("port") is not None else 22,
        "created_by": d.get("created_by"),
        "created_by_username": d.get("created_by_username") or "",
        "created_by_display_name": d.get("created_by_display_name") or "",
    }


async def find_duplicate_host_for_owner(
    db,
    *,
    owner_user_id: int,
    host: str,
    port: Any = None,
) -> Optional[dict]:
    host_value = normalize_host_address(host)
    if not host_value:
        return None
    port_value = normalize_host_port(port)
    rows = await db.execute_fetchall(
        DUPLICATE_HOST_SQL,
        (owner_user_id, port_value, host_value),
    )
    if not rows:
        return None
    return row_to_duplicate_host_payload(rows[0])


def host_duplicate_error_detail(existing: dict, message: str | None = None) -> dict:
    return {
        "code": "host_duplicate",
        "message": message or "同一所有者下该主机地址和端口已存在",
        "existing_host": existing,
    }
