"""主机分组 API（多级分组、服务器树）"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from database import get_db
from api.auth import get_current_user, _is_admin_role
from api.hosts import normalize_host_aliases_in_dict, _attach_user_tags_to_hosts

router = APIRouter(prefix="/api/host-groups", tags=["主机分组"])


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    parent_id: Optional[int] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None


@router.get("")
async def list_groups(user=Depends(get_current_user)):
    db = await get_db()
    if _is_admin_role(user.get("role")):
        rows = await db.execute_fetchall(
            "SELECT id, name, description, parent_id, created_by, created_at FROM host_groups ORDER BY COALESCE(parent_id, 0), id"
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, name, description, parent_id, created_by, created_at FROM host_groups WHERE created_by = ? ORDER BY COALESCE(parent_id, 0), id",
            (user["id"],),
        )
    return {"success": True, "groups": [dict(r) for r in rows]}


def _can_access_group(group_row: dict, user: dict) -> bool:
    return _is_admin_role(user.get("role")) or (group_row.get("created_by") == user["id"])


async def _can_access_host(db, host_row: dict, user: dict) -> bool:
    if _is_admin_role(user.get("role")) or (host_row.get("created_by") == user["id"]):
        return True
    rows = await db.execute_fetchall(
        """SELECT 1 FROM host_shares
           WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL
           LIMIT 1""",
        (host_row.get("id"), user["id"]),
    )
    return bool(rows)


def _body_fields_set(body) -> set:
    return set(getattr(body, "model_fields_set", None) or getattr(body, "__fields_set__", set()) or set())


async def _validate_parent_group(db, *, user: dict, owner_id: int, group_id: Optional[int], parent_id: Optional[int]) -> Optional[int]:
    if parent_id is None:
        return None
    if group_id is not None and parent_id == group_id:
        raise HTTPException(status_code=400, detail="不能将分组移动到自身下")
    parent_rows = await db.execute_fetchall("SELECT id, parent_id, created_by FROM host_groups WHERE id = ?", (parent_id,))
    if not parent_rows:
        raise HTTPException(status_code=404, detail="父分组不存在")
    parent = dict(parent_rows[0])
    if not _can_access_group(parent, user):
        raise HTTPException(status_code=404, detail="父分组不存在")
    if parent.get("created_by") != owner_id:
        raise HTTPException(status_code=403, detail="分组只能移动到同一用户的分组下")
    if group_id is None:
        return parent_id
    rows = await db.execute_fetchall("SELECT id, parent_id FROM host_groups WHERE created_by = ?", (owner_id,))
    parent_map = {int(r["id"]): r["parent_id"] for r in rows}
    current = parent_id
    visited = set()
    while current is not None:
        if current == group_id:
            raise HTTPException(status_code=400, detail="不能将分组移动到其子分组下")
        if current in visited:
            break
        visited.add(current)
        current = parent_map.get(current)
    return parent_id


@router.get("/tree")
async def tree_groups(user=Depends(get_current_user)):
    """返回以用户为顶级节点的树：by_user 每项含 user_id、username、display_name、tree（该用户的分组树）、ungrouped_hosts（该用户未分组主机）。"""
    db = await get_db()
    members = await db.execute_fetchall("SELECT host_id, group_id FROM host_group_members")

    def build_tree_for_user(uid: int, groups: list, host_rows: list) -> dict:
        hosts_by_id = {h["id"]: dict(h) for h in host_rows}
        group_ids = {g["id"] for g in groups}
        group_to_host_ids = {}
        for m in members:
            gid = m["group_id"]
            hid = m["host_id"]
            if gid in group_ids and hid in hosts_by_id:
                group_to_host_ids.setdefault(gid, []).append(hid)
        grouped_ids = set()
        group_to_hosts = {}
        for gid, hid_list in group_to_host_ids.items():
            unique_ids = []
            seen_ids = set()
            for hid in hid_list:
                if hid in seen_ids:
                    continue
                seen_ids.add(hid)
                unique_ids.append(hid)
            grouped_ids.update(unique_ids)
            group_to_hosts[gid] = [hosts_by_id[hid] for hid in unique_ids]

        def build_node(g: dict) -> dict:
            return {**g, "children": [], "hosts": group_to_hosts.get(g["id"], [])}

        by_id = {g["id"]: build_node(g) for g in groups}
        root = []
        for g in groups:
            node = by_id[g["id"]]
            pid = g.get("parent_id")
            if not pid:
                root.append(node)
            else:
                parent = by_id.get(pid)
                if parent:
                    parent["children"].append(node)
                else:
                    root.append(node)
        ungrouped = [hosts_by_id[hid] for hid in hosts_by_id if hid not in grouped_ids]
        return {"tree": root, "ungrouped_hosts": ungrouped}

    def normalize_owner_id(value):
        try:
            if value is None:
                return None
            value = int(value)
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

    def owner_meta(owner_id, users_by_id: dict) -> dict:
        if owner_id is None:
            return {"user_id": None, "username": "", "display_name": "未设置归属的主机/分组"}
        user_row = users_by_id.get(owner_id)
        if user_row:
            return {
                "user_id": owner_id,
                "username": user_row.get("username") or "",
                "display_name": user_row.get("display_name") or user_row.get("username") or ("用户 " + str(owner_id)),
            }
        return {"user_id": owner_id, "username": "", "display_name": "原所属用户已不存在（ID: " + str(owner_id) + "）"}

    if _is_admin_role(user.get("role")):
        all_user_rows = await db.execute_fetchall("SELECT id, username, display_name FROM users")
        users_by_id = {int(r["id"]): dict(r) for r in all_user_rows}
        all_groups = [dict(r) for r in await db.execute_fetchall(
            "SELECT id, name, description, parent_id, created_by, created_at FROM host_groups ORDER BY COALESCE(created_by, 0), id"
        )]
        all_hosts = [
            normalize_host_aliases_in_dict(dict(r))
            for r in await db.execute_fetchall(
                "SELECT id, name, host, port, credential_id, created_by, aliases, remark FROM hosts ORDER BY COALESCE(name, host), id"
            )
        ]
        await _attach_user_tags_to_hosts(db, all_hosts, int(user["id"]))
        all_hosts_by_id = {int(h["id"]): h for h in all_hosts if h.get("id") is not None}
        groups_by_owner = {}
        hosts_by_owner = {}
        for g in all_groups:
            owner_id = normalize_owner_id(g.get("created_by"))
            groups_by_owner.setdefault(owner_id, []).append(g)
        for h in all_hosts:
            owner_id = normalize_owner_id(h.get("created_by"))
            hosts_by_owner.setdefault(owner_id, []).append(h)
        owner_ids = set(groups_by_owner.keys()) | set(hosts_by_owner.keys())
        by_user = []
        for owner_id in sorted(owner_ids, key=lambda x: (x is None, x if x is not None else 0)):
            groups = groups_by_owner.get(owner_id, [])
            hosts = list(hosts_by_owner.get(owner_id, []))
            # 管理员视角：只要主机被加入了该 owner 的分组，就应在该分组下显示，
            # 不应再受主机 created_by 归属限制。
            if groups:
                owner_group_ids = {int(g["id"]) for g in groups if g.get("id") is not None}
                known_host_ids = {int(h["id"]) for h in hosts if h.get("id") is not None}
                for m in members:
                    gid = m["group_id"]
                    hid = m["host_id"]
                    if gid not in owner_group_ids or hid is None:
                        continue
                    host_obj = all_hosts_by_id.get(int(hid))
                    if not host_obj or int(hid) in known_host_ids:
                        continue
                    hosts.append(host_obj)
                    known_host_ids.add(int(hid))
            if not groups and not hosts:
                continue
            meta = owner_meta(owner_id, users_by_id)
            out = build_tree_for_user(owner_id, groups, hosts)
            by_user.append({
                "user_id": meta["user_id"],
                "username": meta["username"],
                "display_name": meta["display_name"],
                "tree": out["tree"],
                "ungrouped_hosts": out["ungrouped_hosts"],
            })
        return {"success": True, "by_user": by_user, "tree": by_user[0]["tree"] if len(by_user) == 1 else [], "ungrouped_hosts": by_user[0]["ungrouped_hosts"] if len(by_user) == 1 else []}
    else:
        uid = user["id"]
        urows = await db.execute_fetchall("SELECT id, username, display_name FROM users WHERE id = ?", (uid,))
        u = dict(urows[0]) if urows else {"username": "", "display_name": ""}
        groups = await db.execute_fetchall(
            "SELECT id, name, description, parent_id, created_by, created_at FROM host_groups WHERE created_by = ? ORDER BY id",
            (uid,),
        )
        hosts = await db.execute_fetchall(
            """SELECT DISTINCT h.id, h.name, h.host, h.port, h.credential_id, h.aliases, h.remark, h.created_by,
                         CASE WHEN h.created_by = ? THEN 0 ELSE 1 END AS is_shared,
                         su.id AS shared_from_user_id, su.username AS shared_from_username, su.display_name AS shared_from_display_name
               FROM hosts h
               LEFT JOIN host_shares hs
                 ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
               LEFT JOIN users su ON su.id = hs.owner_user_id
               WHERE h.created_by = ? OR hs.id IS NOT NULL
               ORDER BY h.name, h.id""",
            (uid, uid, uid),
        )
        normalized_hosts = [normalize_host_aliases_in_dict(dict(h)) for h in hosts]
        await _attach_user_tags_to_hosts(db, normalized_hosts, int(user["id"]))
        out = build_tree_for_user(uid, [dict(g) for g in groups], normalized_hosts)
        by_user = [{
            "user_id": uid,
            "username": u.get("username") or "",
            "display_name": u.get("display_name") or u.get("username") or "",
            "tree": out["tree"],
            "ungrouped_hosts": out["ungrouped_hosts"],
        }]
        return {"success": True, "by_user": by_user, "tree": out["tree"], "ungrouped_hosts": out["ungrouped_hosts"]}


@router.get("/{group_id}")
async def get_group(group_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM host_groups WHERE id = ?", (group_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="分组不存在")
    if not _can_access_group(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="分组不存在")
    members = await db.execute_fetchall(
        "SELECT host_id FROM host_group_members WHERE group_id = ?", (group_id,)
    )
    group = dict(rows[0])
    group["host_ids"] = [m["host_id"] for m in members]
    return {"success": True, "group": group}


@router.post("")
async def create_group(body: GroupCreate, user=Depends(get_current_user)):
    db = await get_db()
    try:
        parent_id = await _validate_parent_group(db, user=user, owner_id=user["id"], group_id=None, parent_id=body.parent_id)
        cursor = await db.execute(
            "INSERT INTO host_groups (name, description, parent_id, created_by) VALUES (?, ?, ?, ?)",
            (body.name, body.description or "", parent_id, user["id"]),
        )
        await db.commit()
        return {"success": True, "id": cursor.lastrowid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{group_id}")
async def update_group(group_id: int, body: GroupUpdate, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="分组不存在")
    group_row = dict(rows[0])
    if not _can_access_group(group_row, user):
        raise HTTPException(status_code=404, detail="分组不存在")
    fields_set = _body_fields_set(body)
    updates, params = [], []
    for f in ("name", "description", "parent_id"):
        if f not in fields_set:
            continue
        v = getattr(body, f, None)
        if f == "parent_id":
            v = await _validate_parent_group(db, user=user, owner_id=group_row["created_by"], group_id=group_id, parent_id=v)
        updates.append(f"{f} = ?")
        params.append(v)
    if updates:
        params.append(group_id)
        await db.execute(f"UPDATE host_groups SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
    return {"success": True}


@router.delete("/{group_id}")
async def delete_group(group_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="分组不存在")
    if not _can_access_group(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="分组不存在")
    await db.execute("DELETE FROM host_group_members WHERE group_id = ?", (group_id,))
    await db.execute("DELETE FROM host_groups WHERE id = ?", (group_id,))
    await db.commit()
    return {"success": True}


@router.get("/{group_id}/hosts")
async def get_group_hosts(group_id: int, user=Depends(get_current_user)):
    db = await get_db()
    grows = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
    if not grows:
        raise HTTPException(status_code=404, detail="分组不存在")
    if not _can_access_group(dict(grows[0]), user):
        raise HTTPException(status_code=404, detail="分组不存在")
    rows = await db.execute_fetchall(
        "SELECT host_id FROM host_group_members WHERE group_id = ?", (group_id,)
    )
    host_ids = [r["host_id"] for r in rows]
    if not host_ids:
        return {"success": True, "hosts": []}
    placeholders = ",".join("?" * len(host_ids))
    if _is_admin_role(user.get("role")):
        hosts = await db.execute_fetchall(
            f"SELECT id, name, host, port, credential_id FROM hosts WHERE id IN ({placeholders})",
            host_ids,
        )
    else:
        hosts = await db.execute_fetchall(
            f"""SELECT DISTINCT h.id, h.name, h.host, h.port, h.credential_id
                FROM hosts h
                LEFT JOIN host_shares hs
                  ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                WHERE h.id IN ({placeholders}) AND (h.created_by = ? OR hs.id IS NOT NULL)""",
            [user["id"], *host_ids, user["id"]],
        )
    return {"success": True, "hosts": [dict(h) for h in hosts]}


@router.post("/{group_id}/hosts")
async def add_hosts_to_group(
    group_id: int,
    body: dict,
    user=Depends(get_current_user),
):
    """将主机加入目标分组。每个用户视角内同一主机仅保留一个分组归属。"""
    host_ids = body.get("host_ids") or []
    if not host_ids:
        return {"success": True}
    db = await get_db()
    grows = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
    if not grows:
        raise HTTPException(status_code=404, detail="分组不存在")
    if not _can_access_group(dict(grows[0]), user):
        raise HTTPException(status_code=404, detail="分组不存在")
    group_owner = grows[0]["created_by"]
    host_rows = await db.execute_fetchall(
        "SELECT id, created_by FROM hosts WHERE id IN ({})".format(",".join("?" * len(host_ids))),
        host_ids,
    )
    for r in host_rows:
        if not await _can_access_host(db, dict(r), user):
            raise HTTPException(status_code=403, detail="无权将该主机加入分组")
    placeholders = ",".join("?" * len(host_ids))
    await db.execute(
        f"""DELETE FROM host_group_members
            WHERE host_id IN ({placeholders})
              AND group_id IN (SELECT id FROM host_groups WHERE created_by = ?)""",
        [*host_ids, group_owner],
    )
    for hid in host_ids:
        try:
            await db.execute(
                "INSERT OR IGNORE INTO host_group_members (host_id, group_id) VALUES (?, ?)",
                (hid, group_id),
            )
        except Exception:
            pass
    await db.commit()
    return {"success": True}


@router.delete("/{group_id}/hosts/{host_id}")
async def remove_host_from_group(
    group_id: int, host_id: int, user=Depends(get_current_user)
):
    db = await get_db()
    grows = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
    if not grows:
        raise HTTPException(status_code=404, detail="分组不存在")
    if not _can_access_group(dict(grows[0]), user):
        raise HTTPException(status_code=404, detail="分组不存在")
    await db.execute(
        "DELETE FROM host_group_members WHERE group_id = ? AND host_id = ?",
        (group_id, host_id),
    )
    await db.commit()
    return {"success": True}
