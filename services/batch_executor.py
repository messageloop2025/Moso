"""批量操作执行引擎：对多台 SSH 主机依次执行命令/上传/脚本/重启（参考 IOTHub）"""
import json
import asyncio
import logging
from pathlib import Path

import config
from database import get_db
from api.hosts import _resolve_host_auth
from api.filesystem import resolve_fs_path, fs_read_file
from services.ssh_client import run_ssh_command, sftp_put_content

logger = logging.getLogger("edgeops.batch")


async def execute_batch(batch_id: int):
    """异步执行批量操作"""
    db = await get_db()

    rows = await db.execute_fetchall("SELECT * FROM batch_operations WHERE id = ?", (batch_id,))
    if not rows:
        return
    batch = dict(rows[0])
    op_type = batch["operation_type"]
    params = json.loads(batch["params"]) if isinstance(batch.get("params"), str) else (batch.get("params") or {})

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
            result = await _execute_single(db, d, op_type, params)
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


async def _execute_single(db, host_row: dict, op_type: str, params: dict) -> dict:
    """对单台主机执行操作"""
    auth = await _resolve_host_auth(db, host_row)
    if not auth or not auth.get("username"):
        return {"success": False, "error": "主机未配置有效登录凭证"}

    host = host_row.get("host") or ""
    port = int(host_row.get("port") or 22)
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
        if content is None and local_path:
            try:
                fp = resolve_fs_path(local_path)
                content_b = fp.read_bytes()
            except Exception as e:
                return {"success": False, "error": f"读取本地文件失败: {e}"}
        else:
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
            timeout=timeout,
        )
        if err:
            return {"success": False, "error": err}
        return {"success": True, "message": f"已写入 {remote_path}"}

    if op_type == "run_script":
        script_path = (params.get("script_path") or "").strip()
        if not script_path:
            return {"success": False, "error": "未提供 script_path（文件系统相对路径，如 scripts/restart.sh）"}
        try:
            res = fs_read_file(script_path)
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
