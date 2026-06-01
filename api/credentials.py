"""凭证管理 API：用户名/密码 与 用户名/公钥私钥 分离配置（设计.md）"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user, require_admin, _is_admin_role
from services.keygen import generate_rsa_key, generate_ecc_key
from services.credential_utils import normalize_private_key_pem

router = APIRouter(prefix="/api/credentials", tags=["凭证管理"])


class CredentialPasswordCreate(BaseModel):
    type: str = "password"
    code: str
    name: str
    description: str = ""
    username: str
    password: str


class CredentialKeyCreate(BaseModel):
    type: str = "key_pair"
    code: str
    name: str
    description: str = ""
    username: str
    key_type: str = "RSA"
    key_bits: int = 2048
    public_key: Optional[str] = None
    private_key: Optional[str] = None


class CredentialUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    public_key: Optional[str] = None
    private_key: Optional[str] = None


class GenerateKeyRequest(BaseModel):
    key_type: str = "RSA"
    key_bits: int = 2048


def _row_to_dict(row, mask_secret: bool = True):
    d = dict(row)
    if mask_secret:
        if d.get("password_enc"):
            d["password_enc"] = "***"
        if d.get("private_key_enc"):
            d["private_key_enc"] = "***"
    return d


@router.get("")
async def list_credentials(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: Optional[str] = Query(None, description="password | key_pair，不传则返回全部"),
    user=Depends(get_current_user),
):
    db = await get_db()
    base = """FROM credentials c LEFT JOIN users u ON c.created_by = u.id"""
    where = " WHERE 1=1"
    params = []
    if not _is_admin_role(user.get("role")):
        where += " AND c.created_by = ?"
        params.append(user["id"])
    if type and type.strip().lower() in ("password", "key_pair"):
        where += " AND c.type = ?"
        params.append(type.strip().lower())
    count_sql = "SELECT COUNT(*) AS n " + base + where
    count_rows = await db.execute_fetchall(count_sql, params)
    total = count_rows[0][0] if count_rows else 0
    offset = (page - 1) * page_size
    params.extend([page_size, offset])
    rows = await db.execute_fetchall(
        """SELECT c.id, c.type, c.code, c.name, c.description, c.username, c.key_type, c.key_bits,
                  c.created_at, c.updated_at, c.created_by,
                  u.username AS created_by_username, u.display_name AS created_by_display_name
           """ + base + where + """ ORDER BY c.id LIMIT ? OFFSET ?""",
        params,
    )
    return {
        "success": True,
        "credentials": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _can_access_credential(cred_row: dict, user: dict) -> bool:
    return _is_admin_role(user.get("role")) or (cred_row.get("created_by") == user["id"])


@router.get("/{credential_id}")
async def get_credential(credential_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM credentials WHERE id = ?", (credential_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if not _can_access_credential(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="凭证不存在")
    return {"success": True, "credential": _row_to_dict(rows[0])}


@router.post("")
async def create_credential(
    body: dict,
    user=Depends(get_current_user),
):
    db = await get_db()
    cred_type = body.get("type", "password")
    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="配置编号和配置名必填")

    try:
        if cred_type == "password":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            if not username:
                raise HTTPException(status_code=400, detail="密码型凭证需填写用户名")
            await db.execute(
                """INSERT INTO credentials (type, code, name, description, username, password_enc, created_by)
                   VALUES ('password', ?, ?, ?, ?, ?, ?)""",
                (code, name, description, username, password, user["id"]),
            )
        else:
            username = (body.get("username") or "").strip()
            key_type = (body.get("key_type") or "RSA").upper()
            key_bits = int(body.get("key_bits") or 2048)
            public_key = body.get("public_key") or ""
            private_key = normalize_private_key_pem(body.get("private_key")) or ""
            if not username and not (public_key and private_key):
                raise HTTPException(status_code=400, detail="密钥型需提供用户名及公钥/私钥，或使用生成接口")
            await db.execute(
                """INSERT INTO credentials (type, code, name, description, username, key_type, key_bits, public_key, private_key_enc, created_by)
                   VALUES ('key_pair', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (code, name, description, username, key_type, key_bits, public_key or None, private_key or None, user["id"]),
            )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        cid = (await cur.fetchone())[0]
        return {"success": True, "id": cid}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{credential_id}")
async def update_credential(
    credential_id: int,
    body: dict,
    user=Depends(get_current_user),
):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, type, created_by FROM credentials WHERE id = ?", (credential_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if not _can_access_credential(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="凭证不存在")

    updates, params = [], []
    for field in ("code", "name", "description", "username"):
        if field in body and body[field] is not None:
            updates.append(f"{field} = ?")
            params.append(body[field])
    if "password" in body and body["password"] is not None:
        updates.append("password_enc = ?")
        params.append(body["password"])
    if "public_key" in body and body["public_key"] is not None:
        updates.append("public_key = ?")
        params.append(body["public_key"])
    if "private_key" in body and body["private_key"] is not None:
        updates.append("private_key_enc = ?")
        params.append(normalize_private_key_pem(body["private_key"]))
    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(credential_id)
        await db.execute(f"UPDATE credentials SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
    return {"success": True}


@router.delete("/{credential_id}")
async def delete_credential(credential_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM credentials WHERE id = ?", (credential_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if not _can_access_credential(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="凭证不存在")
    cursor = await db.execute("SELECT COUNT(*) FROM hosts WHERE credential_id = ?", (credential_id,))
    if (await cursor.fetchone())[0] > 0:
        raise HTTPException(status_code=400, detail="该凭证已被主机引用，请先解除关联")
    await db.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))
    await db.commit()
    return {"success": True}


@router.post("/generate-key")
async def generate_key(
    req: GenerateKeyRequest,
    user=Depends(get_current_user),
):
    """生成新的 RSA 或 ECC 密钥对。"""
    key_type = (req.key_type or "RSA").upper()
    key_bits = req.key_bits or 2048
    if key_type == "RSA":
        private_pem, public_pem = generate_rsa_key(key_bits)
    elif key_type in ("ECC", "EC"):
        curve = "secp256r1" if key_bits <= 256 else ("secp384r1" if key_bits <= 384 else "secp521r1")
        private_pem, public_pem = generate_ecc_key(curve)
    else:
        raise HTTPException(status_code=400, detail="key_type 支持 RSA / ECC")
    return {"success": True, "private_key": private_pem, "public_key": public_pem}
