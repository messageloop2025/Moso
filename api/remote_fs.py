"""远程文件系统 API：通过 SSH/SFTP 列出、读取、上传主机上的文件。路径禁止 .. 逃逸。"""
import asyncio
import logging
from io import BytesIO
from typing import Optional
from urllib.parse import quote

import paramiko
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from database import get_db
from api.auth import get_current_user, _is_admin_role
from api.hosts import _resolve_host_auth
from services.paramiko_banner_fix import patch_banner_encoding, unpatch_banner_encoding
from services.ssh_connect import establish_ssh_client


def _connect_ssh_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    timeout: int,
) -> paramiko.SSHClient:
    patch_banner_encoding()
    try:
        client = establish_ssh_client(
            hostname=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password=password,
            key_path=key_path,
            private_key_pem=private_key_pem,
            timeout=timeout,
        )
    finally:
        unpatch_banner_encoding()
    return client


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

logger = logging.getLogger("edgeops.remote_fs")
router = APIRouter(prefix="/api/remote-fs", tags=["远程文件系统"])


def _norm_path(path: str) -> str:
    """规范化路径，禁止 .. 逃逸。"""
    if not path or path == "/":
        return "/"
    parts = [p for p in path.replace("\\", "/").strip("/").split("/") if p and p != "."]
    out = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
            continue
        out.append(p)
    return "/" + "/".join(out) if out else "/"


def _sftp_list_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    timeout: int = 30,
) -> list:
    """同步 SFTP 列目录，返回 [{"name", "path", "dir", "size"}, ...]，目录在前。"""
    client = None
    try:
        client = _connect_ssh_sync(
            host, port, username, auth_type, password, key_path, private_key_pem, timeout,
        )
        sftp = client.open_sftp()
        try:
            items = sftp.listdir_attr(remote_path)
        except FileNotFoundError:
            return []
        result = []
        for attr in items:
            name = attr.filename
            if name in (".", ".."):
                continue
            full = remote_path.rstrip("/") + "/" + name
            is_dir = attr.st_mode is not None and (attr.st_mode & 0o170000) == 0o040000
            result.append({
                "name": name,
                "path": full,
                "dir": is_dir,
                "size": attr.st_size if not is_dir else 0,
            })
        result.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        return result
    finally:
        unpatch_banner_encoding()
        try:
            client.close()
        except Exception:
            pass


def _sftp_read_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    timeout: int = 30,
    max_size: int = 2 * 1024 * 1024,
) -> tuple[Optional[bytes], Optional[str]]:
    """同步 SFTP 读文件。返回 (content, None) 或 (None, error_message)。"""
    client = None
    try:
        client = _connect_ssh_sync(
            host, port, username, auth_type, password, key_path, private_key_pem, timeout,
        )
        sftp = client.open_sftp()
        try:
            stat = sftp.stat(remote_path)
            if stat.st_size > max_size:
                return None, "文本过大或非文本文件"
            with sftp.open(remote_path, "rb") as f:
                data = f.read()
            return data, None
        except FileNotFoundError:
            return None, "文件不存在"
    except Exception as e:
        return None, str(e)
    finally:
        unpatch_banner_encoding()
        try:
            client.close()
        except Exception:
            pass


def _sftp_upload_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    content: bytes,
    timeout: int = 30,
    max_size: int = 100 * 1024 * 1024,
) -> Optional[str]:
    """通过 SFTP 上传 content 到远程路径。成功返回 None，失败返回错误信息。"""
    if len(content) > max_size:
        return "文件过大，单文件限制 100MB"
    client = None
    try:
        client = _connect_ssh_sync(
            host, port, username, auth_type, password, key_path, private_key_pem, timeout,
        )
        sftp = client.open_sftp()
        try:
            sftp.putfo(BytesIO(content), remote_path)
            return None
        except FileNotFoundError:
            return "目标目录不存在或无权限"
    except Exception as e:
        return str(e)
    finally:
        unpatch_banner_encoding()
        try:
            client.close()
        except Exception:
            pass


def _sftp_remove_recursive(sftp, remote_path: str) -> Optional[str]:
    """递归删除目录或删除文件。成功返回 None，失败返回错误信息。"""
    try:
        stat = sftp.stat(remote_path)
        if (stat.st_mode & 0o170000) == 0o040000:  # dir
            for name in sftp.listdir(remote_path):
                if name in (".", ".."):
                    continue
                full = (remote_path.rstrip("/") + "/" + name)
                err = _sftp_remove_recursive(sftp, full)
                if err:
                    return err
            sftp.rmdir(remote_path)
        else:
            sftp.remove(remote_path)
        return None
    except Exception as e:
        return str(e)


def _sftp_remove_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    timeout: int = 30,
) -> Optional[str]:
    """删除文件或目录（递归）。成功返回 None，失败返回错误信息。"""
    client = None
    try:
        client = _connect_ssh_sync(
            host, port, username, auth_type, password, key_path, private_key_pem, timeout,
        )
        sftp = client.open_sftp()
        try:
            return _sftp_remove_recursive(sftp, remote_path)
        finally:
            sftp.close()
    except Exception as e:
        return str(e)
    finally:
        unpatch_banner_encoding()
        try:
            client.close()
        except Exception:
            pass


def _sftp_rename_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    old_path: str,
    new_path: str,
    timeout: int = 30,
) -> Optional[str]:
    """重命名/移动。成功返回 None，失败返回错误信息。"""
    client = None
    try:
        client = _connect_ssh_sync(
            host, port, username, auth_type, password, key_path, private_key_pem, timeout,
        )
        sftp = client.open_sftp()
        try:
            sftp.rename(old_path, new_path)
            return None
        except Exception as e:
            return str(e)
    except Exception as e:
        return str(e)
    finally:
        unpatch_banner_encoding()
        try:
            client.close()
        except Exception:
            pass


def _sftp_mkdir_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    timeout: int = 30,
) -> Optional[str]:
    """在主机上创建目录（仅最后一级，父目录须已存在）。成功返回 None。"""
    client = None
    try:
        client = _connect_ssh_sync(
            host, port, username, auth_type, password, key_path, private_key_pem, timeout,
        )
        sftp = client.open_sftp()
        try:
            sftp.mkdir(remote_path)
            return None
        except Exception as e:
            return str(e)
    except Exception as e:
        return str(e)
    finally:
        unpatch_banner_encoding()
        try:
            client.close()
        except Exception:
            pass


def _sftp_copy_recursive(sftp, src: str, dest: str) -> Optional[str]:
    """递归复制。dest 为目标路径（新文件或新目录名）。"""
    try:
        stat = sftp.stat(src)
        if (stat.st_mode & 0o170000) == 0o040000:
            sftp.mkdir(dest)
            for name in sftp.listdir(src):
                if name in (".", ".."):
                    continue
                err = _sftp_copy_recursive(
                    sftp,
                    src.rstrip("/") + "/" + name,
                    dest.rstrip("/") + "/" + name,
                )
                if err:
                    return err
        else:
            with sftp.open(src, "rb") as f:
                data = f.read()
            with sftp.open(dest, "wb") as f:
                f.write(data)
        return None
    except Exception as e:
        return str(e)


def _sftp_copy_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    src_path: str,
    dest_path: str,
    timeout: int = 30,
) -> Optional[str]:
    """复制到目标路径。成功返回 None，失败返回错误信息。"""
    client = None
    try:
        client = _connect_ssh_sync(
            host, port, username, auth_type, password, key_path, private_key_pem, timeout,
        )
        sftp = client.open_sftp()
        try:
            return _sftp_copy_recursive(sftp, src_path, dest_path)
        finally:
            sftp.close()
    except Exception as e:
        return str(e)
    finally:
        unpatch_banner_encoding()
        try:
            client.close()
        except Exception:
            pass


@router.get("/list")
async def remote_list(host_id: int, path: str = "/", user=Depends(get_current_user)):
    """列出主机上某目录下的项（目录在上、文件在下）。"""
    path = _norm_path(path)
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
        items = await asyncio.to_thread(
            _sftp_list_sync,
            host_row["host"],
            host_row.get("port") or 22,
            auth["username"],
            auth.get("auth_type") or "password",
            auth.get("password"),
            auth.get("key_path"),
            auth.get("private_key_pem"),
            path,
            30,
        )
        return {"success": True, "path": path, "items": items}
    except Exception as e:
        logger.exception("remote_fs list: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mkdir")
async def remote_mkdir(host_id: int, path: str, user=Depends(get_current_user)):
    """在主机上创建目录。path 为新目录的完整路径，父目录须已存在。"""
    remote_path = _norm_path(path)
    if remote_path == "/" or not remote_path:
        raise HTTPException(status_code=400, detail="请指定目录路径")
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
    err = await asyncio.to_thread(
        _sftp_mkdir_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        remote_path,
        30,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"success": True, "path": remote_path}


@router.get("/read")
async def remote_read(host_id: int, path: str, user=Depends(get_current_user)):
    """读取主机上的文件内容（文本，最大约 2MB）。"""
    path = _norm_path(path)
    if path == "/":
        raise HTTPException(status_code=400, detail="请指定文件路径")
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
    data, err = await asyncio.to_thread(
        _sftp_read_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        path,
        30,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    if data and (b"\x00" in data[:65536] or len(data) > 2 * 1024 * 1024):
        raise HTTPException(status_code=400, detail="文本过大或非文本文件")
    try:
        text = (data or b"").decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文本过大或非文本文件")
    return {"success": True, "path": path, "content": text}


@router.get("/download")
async def remote_download(host_id: int, path: str, user=Depends(get_current_user)):
    """下载主机上的文件（二进制流）。"""
    path = _norm_path(path)
    if path == "/":
        raise HTTPException(status_code=400, detail="请指定文件路径")
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
    data, err = await asyncio.to_thread(
        _sftp_read_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        path,
        30,
        max_size=50 * 1024 * 1024,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    return Response(content=data or b"", media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})  # noqa: E501


@router.post("/upload")
async def remote_upload(
    host_id: int = Form(...),
    path: str = Form("/"),
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """上传文件到主机指定目录。path 为目标目录（如 /tmp），file 为上传的文件。"""
    dir_path = _norm_path(path)
    if not file.filename or file.filename.strip() == "":
        raise HTTPException(status_code=400, detail="缺少文件名")
    filename = file.filename.replace("\\", "/").strip().split("/")[-1]
    if not filename or ".." in filename:
        raise HTTPException(status_code=400, detail="文件名无效")
    remote_path = (dir_path.rstrip("/") + "/" + filename) if dir_path != "/" else ("/" + filename)
    remote_path = _norm_path(remote_path)
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
    content = await file.read()
    err = await asyncio.to_thread(
        _sftp_upload_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        remote_path,
        content,
        30,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"success": True, "path": remote_path}


class WriteBody(BaseModel):
    content: str


@router.post("/write")
async def remote_write(
    host_id: int,
    path: str,
    body: WriteBody,
    user=Depends(get_current_user),
):
    """将文本内容写回主机上的文件。path 为完整文件路径，仅支持 2MB 以内。"""
    remote_path = _norm_path(path)
    if remote_path == "/":
        raise HTTPException(status_code=400, detail="请指定文件路径")
    raw = (body.content or "").encode("utf-8")
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大，仅支持 2MB 以内")
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
    err = await asyncio.to_thread(
        _sftp_upload_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        remote_path,
        raw,
        30,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"success": True, "path": remote_path}


@router.delete("/delete")
async def remote_delete(
    host_id: int,
    path: str,
    user=Depends(get_current_user),
):
    """删除主机上的文件或目录（递归）。"""
    remote_path = _norm_path(path)
    if remote_path == "/":
        raise HTTPException(status_code=400, detail="禁止删除根目录")
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
    err = await asyncio.to_thread(
        _sftp_remove_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        remote_path,
        30,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"success": True}


class RenameBody(BaseModel):
    new_path: str


@router.post("/rename")
async def remote_rename(
    host_id: int,
    path: str,
    body: RenameBody,
    user=Depends(get_current_user),
):
    """重命名或移动。path 为原路径，new_path 为新完整路径。"""
    old_path = _norm_path(path)
    new_path = _norm_path((body.new_path or "").strip())
    if old_path == "/" or not new_path:
        raise HTTPException(status_code=400, detail="路径无效")
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
    err = await asyncio.to_thread(
        _sftp_rename_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        old_path,
        new_path,
        30,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"success": True, "path": new_path}


class CopyBody(BaseModel):
    dest_dir: str
    move: bool = False


@router.post("/copy")
async def remote_copy(
    host_id: int,
    path: str,
    body: CopyBody,
    user=Depends(get_current_user),
):
    """复制到目标目录；若 move 为 true 则复制后删除源。path 为源路径，dest_dir 为目标目录。"""
    src_path = _norm_path(path)
    dest_dir = _norm_path((body.dest_dir or "").strip())
    if src_path == "/" or not (body.dest_dir or "").strip():
        raise HTTPException(status_code=400, detail="路径无效")
    name = src_path.rsplit("/", 1)[-1] if "/" in src_path else src_path
    dest_path = (dest_dir.rstrip("/") + "/" + name) if dest_dir != "/" else ("/" + name)
    dest_path = _norm_path(dest_path)
    if src_path == dest_path:
        raise HTTPException(status_code=400, detail="目标与源相同")
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
    err = await asyncio.to_thread(
        _sftp_copy_sync,
        host_row["host"],
        host_row.get("port") or 22,
        auth["username"],
        auth.get("auth_type") or "password",
        auth.get("password"),
        auth.get("key_path"),
        auth.get("private_key_pem"),
        src_path,
        dest_path,
        30,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    if body.move:
        err2 = await asyncio.to_thread(
            _sftp_remove_sync,
            host_row["host"],
            host_row.get("port") or 22,
            auth["username"],
            auth.get("auth_type") or "password",
            auth.get("password"),
            auth.get("key_path"),
            auth.get("private_key_pem"),
            src_path,
            30,
        )
        if err2:
            raise HTTPException(status_code=400, detail="复制成功但删除源失败: " + err2)
    return {"success": True, "path": dest_path}
