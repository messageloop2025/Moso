"""批量操作执行引擎：对多台 SSH 主机依次执行命令/上传/拉取/脚本/重启（参考 IOTHub）"""
import json
import asyncio
import logging
from pathlib import Path

from database import get_db
from api.hosts import _resolve_host_auth
from api.filesystem import resolve_fs_path, fs_read_file, get_user_fs_root, FS_DIR
from services.ssh_client import run_ssh_command, sftp_put_content
from services.sftp_transfer import sftp_push_path_sync, sftp_pull_path_sync

logger = logging.getLogger("edgeops.batch")

BATCH_OP_TYPES = frozenset({"run_command", "scp_push", "scp_pull", "run_script", "restart"})


async def _load_creator_fs_root(db, created_by: int | None) -> Path:
    if not created_by:
        return FS_DIR
    rows = await db.execute_fetchall(
        "SELECT id, username, role FROM users WHERE id = ?",
        (created_by,),
    )
    if not rows:
        return FS_DIR
    return get_user_fs_root(dict(rows[0]))


async def execute_batch(batch_id: int):
    """异步执行批量操作"""
    db = await get_db()

    rows = await db.execute_fetchall("SELECT * FROM batch_operations WHERE id = ?", (batch_id,))
    if not rows:
        return
    batch = dict(rows[0])
    op_type = batch["operation_type"]
    params = json.loads(batch["params"]) if isinstance(batch.get("params"), str) else (batch.get("params") or {})
    user_base = await _load_creator_fs_root(db, batch.get("created_by"))

    details = await db.execute_fetchall("""
        SELECT bd.id as detail_id, bd.host_id, h.*
        FROM batch_operation_details bd
        JOIN hosts h ON h.id = bd.host_id
        WHERE bd.batch_id = ? AND bd.status = 'pending'
    """, (batch_id,))

    for row in details:
        d = dict(row)
        detail_id = d["detail_id"]
        host_id = d["host_id"]

        check = await db.execute_fetchall("SELECT status FROM batch_operations WHERE id = ?", (batch_id,))
        if check and dict(check[0]).get("status") == "cancelled":
            break

        await db.execute(
            "UPDATE batch_operation_details SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=?",
            (detail_id,),
        )
        await db.commit()

        try:
            result = await _execute_single(
                db, d, op_type, params, batch_id=batch_id, user_fs_root=user_base
            )
            success = result.get("success") is True
            await db.execute(
                "UPDATE batch_operation_details SET status=?, result=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                ("success" if success else "failed", json.dumps(result, ensure_ascii=False), detail_id),
            )
            if success:
                await db.execute(
                    "UPDATE batch_operations SET success_count = success_count + 1, pending_count = pending_count - 1 WHERE id=?",
                    (batch_id,),
                )
            else:
                await db.execute(
                    "UPDATE batch_operations SET fail_count = fail_count + 1, pending_count = pending_count - 1 WHERE id=?",
                    (batch_id,),
                )
        except Exception as e:
            logger.exception("批量操作主机 %s 失败: %s", d.get("name") or d.get("host"), e)
            await db.execute(
                "UPDATE batch_operation_details SET status='failed', result=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps({"success": False, "error": str(e)}, ensure_ascii=False), detail_id),
            )
            await db.execute(
                "UPDATE batch_operations SET fail_count = fail_count + 1, pending_count = pending_count - 1 WHERE id=?",
                (batch_id,),
            )
        await db.commit()

        await asyncio.sleep(0.15)

    await db.execute(
        "UPDATE batch_operations SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
        (batch_id,),
    )
    await db.commit()
    logger.info("批量操作 #%s 已完成", batch_id)


def _transfer_timeout(params: dict, default: int = 300) -> int:
    try:
        timeout = int(params.get("timeout") or default)
    except (TypeError, ValueError):
        timeout = default
    return max(30, min(timeout, 3600))


async def _execute_single(
    db,
    host_row: dict,
    op_type: str,
    params: dict,
    *,
    batch_id: int,
    user_fs_root: Path,
) -> dict:
    """对单台主机执行操作"""
    auth = await _resolve_host_auth(db, host_row)
    if not auth or not auth.get("username"):
        return {"success": False, "error": "主机未配置有效登录凭证"}

    host = host_row.get("host") or ""
    port = int(host_row.get("port") or 22)
    host_id = int(host_row.get("host_id") or host_row.get("id") or 0)
    timeout = int(params.get("timeout") or 30)

    if op_type == "run_command":
        command = (params.get("command") or "").strip()
        if not command:
            return {"success": False, "error": "未提供 command"}
        try:
            out, err, code = await run_ssh_command(
                host=host,
                port=port,
                username=auth["username"],
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=command,
                timeout=timeout,
            )
            return {"success": code == 0, "stdout": out, "stderr": err, "exit_code": code}
        except Exception as e:
            return {"success": False, "error": str(e)}

    if op_type == "scp_push":
        remote_path = (params.get("remote_path") or "").strip()
        if not remote_path:
            return {"success": False, "error": "未提供 remote_path"}
        content = params.get("content")
        local_path = (params.get("local_path") or "").strip()
        xfer_timeout = _transfer_timeout(params, 300)
        if content is None and local_path:
            try:
                fp = resolve_fs_path(local_path, user_fs_root)
                if not fp.exists():
                    return {"success": False, "error": f"本地路径不存在: {local_path}"}
                recursive = bool(params.get("recursive")) or fp.is_dir()
                if fp.is_dir() and not recursive:
                    return {"success": False, "error": "本地路径为目录，请设置 params.recursive=true"}
                result = await asyncio.to_thread(
                    sftp_push_path_sync,
                    host=host,
                    port=port,
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    local_path=str(fp.resolve()),
                    remote_path=remote_path,
                    recursive=recursive,
                    timeout=xfer_timeout,
                )
                if not result.success:
                    return {"success": False, "error": result.error or "上传失败"}
                return {
                    "success": True,
                    "message": f"已上传至 {result.resolved_remote_path or remote_path}",
                    "remote_path": result.resolved_remote_path or remote_path,
                    "local_path": local_path,
                    "bytes_transferred": result.bytes_transferred,
                    "files_transferred": result.files_transferred,
                    "recursive": recursive,
                }
            except Exception as e:
                return {"success": False, "error": f"读取本地文件失败: {e}"}
        content_b = (content if isinstance(content, str) else str(content or "")).encode("utf-8", errors="replace")
        err = await sftp_put_content(
            host=host,
            port=port,
            username=auth.get("username") or "",
            auth_type=auth.get("auth_type") or "password",
            password=auth.get("password"),
            key_path=auth.get("key_path"),
            private_key_pem=auth.get("private_key_pem"),
            remote_path=remote_path,
            content=content_b,
            timeout=min(xfer_timeout, 120),
        )
        if err:
            return {"success": False, "error": err}
        return {"success": True, "message": f"已写入 {remote_path}", "bytes_transferred": len(content_b)}

    if op_type == "scp_pull":
        remote_path = (params.get("remote_path") or "").strip()
        if not remote_path:
            return {"success": False, "error": "未提供 remote_path"}
        recursive = bool(params.get("recursive"))
        xfer_timeout = _transfer_timeout(params, 300)
        local_base = (params.get("local_path") or f"batch_pulls/{batch_id}").strip().replace("\\", "/").strip("/")
        if not local_base:
            local_base = f"batch_pulls/{batch_id}"
        remote_name = Path(remote_path.replace("\\", "/").rstrip("/")).name or ("tree" if recursive else "pull.bin")
        if recursive:
            dest_rel = f"{local_base}/{host_id}"
        else:
            dest_rel = f"{local_base}/{host_id}/{remote_name}"
        try:
            dest_abs = resolve_fs_path(dest_rel, user_fs_root)
            if recursive:
                dest_abs.mkdir(parents=True, exist_ok=True)
            else:
                dest_abs.parent.mkdir(parents=True, exist_ok=True)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        try:
            max_bytes = int(params.get("max_bytes") or 0)
        except (TypeError, ValueError):
            max_bytes = 0
        try:
            max_tree = int(params.get("max_tree_bytes") or 0)
        except (TypeError, ValueError):
            max_tree = 0
        if max_bytes < 0:
            max_bytes = 0
        if max_tree < 0:
            max_tree = 0

        result = await asyncio.to_thread(
            sftp_pull_path_sync,
            host=host,
            port=port,
            username=auth.get("username") or "",
            auth_type=auth.get("auth_type") or "password",
            password=auth.get("password"),
            key_path=auth.get("key_path"),
            private_key_pem=auth.get("private_key_pem"),
            remote_path=remote_path,
            local_path=str(dest_abs),
            recursive=recursive,
            max_bytes=max_bytes,
            max_tree_bytes=max_tree,
            timeout=xfer_timeout,
        )
        if not result.success:
            return {"success": False, "error": result.error or "下载失败"}
        return {
            "success": True,
            "message": f"已拉取到 {dest_rel}",
            "remote_path": remote_path,
            "local_path": dest_rel,
            "bytes_transferred": result.bytes_transferred,
            "files_transferred": result.files_transferred,
            "recursive": recursive,
            "host_id": host_id,
        }

    if op_type == "run_script":
        script_path = (params.get("script_path") or "").strip()
        if not script_path:
            return {"success": False, "error": "未提供 script_path（文件系统相对路径，如 scripts/restart.sh）"}
        try:
            res = fs_read_file(script_path, user_fs_root)
            script_content = (res.get("content") or "").strip()
        except Exception as e:
            return {"success": False, "error": f"读取脚本文件失败: {e}"}
        if not script_content:
            return {"success": False, "error": "脚本内容为空"}
        remote_path = (params.get("remote_path") or "").strip() or "/tmp/edgeops_batch_script.sh"
        content_b = script_content.encode("utf-8", errors="replace")
        err = await sftp_put_content(
            host=host, port=port,
            username=auth.get("username") or "",
            auth_type=auth.get("auth_type") or "password",
            password=auth.get("password"),
            key_path=auth.get("key_path"),
            private_key_pem=auth.get("private_key_pem"),
            remote_path=remote_path,
            content=content_b,
            timeout=timeout,
        )
        if err:
            return {"success": False, "error": f"上传脚本失败: {err}"}
        run_cmd = f"chmod +x {remote_path} && {remote_path}" if params.get("executable", True) else f"bash {remote_path}"
        try:
            out, err_out, code = await run_ssh_command(
                host=host, port=port,
                username=auth["username"],
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=run_cmd,
                timeout=timeout,
            )
            return {"success": code == 0, "stdout": out, "stderr": err_out, "exit_code": code}
        except Exception as e:
            return {"success": False, "error": str(e)}

    if op_type == "restart":
        command = (params.get("command") or "sudo reboot").strip() or "sudo reboot"
        try:
            out, err, code = await run_ssh_command(
                host=host, port=port,
                username=auth["username"],
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=command,
                timeout=min(timeout, 10),
            )
            return {"success": code == 0, "stdout": out, "stderr": err, "exit_code": code}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"未知操作类型: {op_type}"}
