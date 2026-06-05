"""SSH 客户端：在远程主机上执行命令（支持密码、key_path、私钥 PEM 字符串）"""
import asyncio
import os
import time
from typing import Awaitable, Callable, Optional, Tuple

import paramiko

from services.paramiko_banner_fix import patch_banner_encoding, unpatch_banner_encoding
from services.ssh_connect import establish_ssh_client


def _run_ssh_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    command: str,
    timeout: int,
) -> Tuple[str, str, int]:
    """同步执行 SSH 命令，返回 (stdout, stderr, exit_code)。"""
    client = None
    try:
        patch_banner_encoding()
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
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code
    finally:
        unpatch_banner_encoding()
        if client:
            client.close()


async def run_ssh_command(
    host: str,
    port: int = 22,
    username: str = "",
    auth_type: str = "password",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    command: str = "",
    timeout: int = 30,
) -> Tuple[str, str, int]:
    """异步执行 SSH 命令（在线程池中运行 paramiko）。"""
    return await asyncio.to_thread(
        _run_ssh_sync,
        host,
        port,
        username,
        auth_type,
        password,
        key_path,
        private_key_pem,
        command,
        timeout,
    )


def _run_ssh_streaming_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    command: str,
    timeout: int,
    emit_line: Callable[[str, str], None],
    stdout_cap: int,
    stderr_cap: int,
) -> Tuple[str, str, int, bool]:
    """流式 SSH 执行：每读到一行就调 `emit_line(stream, line)`（'stdout' / 'stderr'）。
    stdout_cap / stderr_cap 超过时停止累积（但仍继续流式回调），最终返回的
    完整字符串会被截断以避免内存膨胀。最后一个返回值 timed_out。"""
    client = None
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_len = 0
    stderr_len = 0
    timed_out = False
    exit_code = -1
    try:
        patch_banner_encoding()
        client = establish_ssh_client(
            hostname=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password=password,
            key_path=key_path,
            private_key_pem=private_key_pem,
            timeout=min(30, timeout),
        )

        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport 未建立")
        channel = transport.open_session()
        channel.settimeout(0.0)  # 非阻塞
        channel.exec_command(command)

        out_line_buf = bytearray()
        err_line_buf = bytearray()
        deadline = time.time() + max(5, timeout)

        def _emit_stream_buf(buf: bytearray, stream: str) -> None:
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    return
                line_bytes = bytes(buf[:idx])
                # 去掉可能的 \r
                if line_bytes.endswith(b"\r"):
                    line_bytes = line_bytes[:-1]
                del buf[: idx + 1]
                try:
                    line = line_bytes.decode("utf-8", errors="replace")
                except Exception:
                    line = repr(line_bytes)
                try:
                    emit_line(stream, line)
                except Exception:
                    pass

        while True:
            did_read = False
            if channel.recv_ready():
                try:
                    chunk = channel.recv(65536)
                except Exception:
                    chunk = b""
                if chunk:
                    did_read = True
                    if stdout_len < stdout_cap:
                        stdout_parts.append(chunk.decode("utf-8", errors="replace"))
                        stdout_len += len(chunk)
                    out_line_buf.extend(chunk)
                    _emit_stream_buf(out_line_buf, "stdout")
            if channel.recv_stderr_ready():
                try:
                    chunk = channel.recv_stderr(65536)
                except Exception:
                    chunk = b""
                if chunk:
                    did_read = True
                    if stderr_len < stderr_cap:
                        stderr_parts.append(chunk.decode("utf-8", errors="replace"))
                        stderr_len += len(chunk)
                    err_line_buf.extend(chunk)
                    _emit_stream_buf(err_line_buf, "stderr")
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.time() > deadline:
                timed_out = True
                try:
                    channel.close()
                except Exception:
                    pass
                break
            if not did_read:
                time.sleep(0.03)

        if out_line_buf:
            tail = bytes(out_line_buf).decode("utf-8", errors="replace")
            if tail:
                try:
                    emit_line("stdout", tail)
                except Exception:
                    pass
        if err_line_buf:
            tail = bytes(err_line_buf).decode("utf-8", errors="replace")
            if tail:
                try:
                    emit_line("stderr", tail)
                except Exception:
                    pass

        try:
            exit_code = channel.recv_exit_status()
        except Exception:
            exit_code = -1

        out_full = "".join(stdout_parts)
        err_full = "".join(stderr_parts)
        return out_full, err_full, exit_code, timed_out
    finally:
        unpatch_banner_encoding()
        if client:
            try:
                client.close()
            except Exception:
                pass


async def run_ssh_command_streaming(
    *,
    host: str,
    port: int = 22,
    username: str = "",
    auth_type: str = "password",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    command: str = "",
    timeout: int = 30,
    on_line: Optional[Callable[[str, str], Awaitable[None]]] = None,
    stdout_cap: int = 2_000_000,
    stderr_cap: int = 200_000,
) -> Tuple[str, str, int, bool]:
    """异步版流式 SSH。`on_line(stream, line)` 异步回调，每完整一行触发一次。
    返回 (stdout, stderr, exit_code, timed_out)。不传 on_line 时退化为普通缓冲式。"""
    if on_line is None:
        out, err, code = await run_ssh_command(
            host=host, port=port, username=username,
            auth_type=auth_type, password=password,
            key_path=key_path, private_key_pem=private_key_pem,
            command=command, timeout=timeout,
        )
        return out, err, code, False

    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    def _sync_emit(stream: str, line: str) -> None:
        try:
            loop.call_soon_threadsafe(events.put_nowait, (stream, line))
        except RuntimeError:
            pass

    async def _runner() -> Tuple[str, str, int, bool]:
        try:
            return await asyncio.to_thread(
                _run_ssh_streaming_sync,
                host, port, username, auth_type,
                password, key_path, private_key_pem,
                command, timeout, _sync_emit,
                stdout_cap, stderr_cap,
            )
        finally:
            loop.call_soon_threadsafe(events.put_nowait, SENTINEL)

    runner_task = asyncio.create_task(_runner())
    try:
        while True:
            ev = await events.get()
            if ev is SENTINEL:
                break
            stream, line = ev
            try:
                await on_line(stream, line)
            except Exception:
                pass
    finally:
        # 即使我们被取消，也要收口 runner_task
        if not runner_task.done():
            try:
                await asyncio.wait_for(runner_task, timeout=5)
            except Exception:
                runner_task.cancel()
    return await runner_task


def _sftp_put_sync(
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
) -> Optional[str]:
    """通过 SFTP 将 content 写入远程文件。成功返回 None，失败返回错误信息。"""
    from io import BytesIO
    client = None
    try:
        patch_banner_encoding()
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
        sftp = client.open_sftp()
        sftp.putfo(BytesIO(content), remote_path)
        sftp.close()
        return None
    except Exception as e:
        return str(e)
    finally:
        unpatch_banner_encoding()
        if client:
            client.close()


async def sftp_put_content(
    host: str,
    port: int = 22,
    username: str = "",
    auth_type: str = "password",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    remote_path: str = "",
    content: bytes = b"",
    timeout: int = 30,
) -> Optional[str]:
    """异步：通过 SFTP 将 content 写入远程文件。成功返回 None，失败返回错误信息。"""
    return await asyncio.to_thread(
        _sftp_put_sync,
        host,
        port,
        username,
        auth_type,
        password,
        key_path,
        private_key_pem,
        remote_path,
        content,
        timeout,
    )


def _sftp_get_to_local_path_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    local_path: str,
    max_bytes: int,
    timeout: int,
    chunk_size: int = 1024 * 1024,
) -> Optional[str]:
    """SFTP 拉取远程文件到本地路径（流式写入）。成功返回 None，失败返回错误信息。"""
    client = None
    try:
        patch_banner_encoding()
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
        sftp = client.open_sftp()
        try:
            try:
                st = sftp.stat(remote_path)
            except FileNotFoundError:
                return "远程文件不存在"
            except Exception as e:
                return str(e)
            if (st.st_mode is not None) and ((st.st_mode & 0o170000) == 0o040000):
                return "远程路径为目录，请指定文件路径"
            if st.st_size is not None and int(st.st_size) > max_bytes:
                return f"远程文件过大（{st.st_size} 字节 > 上限 {max_bytes}）"
            read_so_far = 0
            with sftp.open(remote_path, "rb") as rf:
                with open(local_path, "wb") as wf:
                    while True:
                        chunk = rf.read(chunk_size)
                        if not chunk:
                            break
                        read_so_far += len(chunk)
                        if read_so_far > max_bytes:
                            wf.flush()
                            try:
                                os.unlink(local_path)
                            except OSError:
                                pass
                            return f"传输超过上限 {max_bytes} 字节（已中止）"
                        wf.write(chunk)
            return None
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    except Exception as e:
        try:
            if local_path and os.path.exists(local_path):
                os.unlink(local_path)
        except OSError:
            pass
        return str(e)
    finally:
        unpatch_banner_encoding()
        if client:
            try:
                client.close()
            except Exception:
                pass


def _sftp_put_from_path_sync(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    local_path: str,
    remote_path: str,
    timeout: int = 30,
) -> Optional[str]:
    """SFTP 从本地路径上传单个文件（流式读取，不经内存缓冲）。成功返回 None，失败返回错误信息。"""
    client = None
    try:
        patch_banner_encoding()
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
        sftp = client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
            return None
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    except Exception as e:
        return str(e)
    finally:
        unpatch_banner_encoding()
        if client:
            try:
                client.close()
            except Exception:
                pass


async def sftp_put_from_path(
    host: str,
    port: int = 22,
    username: str = "",
    auth_type: str = "password",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    local_path: str = "",
    remote_path: str = "",
    timeout: int = 30,
) -> Optional[str]:
    """异步：SFTP 从本地路径流式上传单个文件。成功返回 None，失败返回错误信息。"""
    return await asyncio.to_thread(
        _sftp_put_from_path_sync,
        host,
        port,
        username,
        auth_type,
        password,
        key_path,
        private_key_pem,
        local_path,
        remote_path,
        timeout,
    )


async def sftp_get_to_local_path(
    host: str,
    port: int = 22,
    username: str = "",
    auth_type: str = "password",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    remote_path: str = "",
    local_path: str = "",
    max_bytes: int = 200 * 1024 * 1024,
    timeout: int = 120,
) -> Optional[str]:
    """异步：SFTP 将远程文件流式写入 local_path。成功返回 None，失败返回错误信息。"""
    return await asyncio.to_thread(
        _sftp_get_to_local_path_sync,
        host,
        port,
        username,
        auth_type,
        password,
        key_path,
        private_key_pem,
        remote_path,
        local_path,
        max_bytes,
        timeout,
    )
