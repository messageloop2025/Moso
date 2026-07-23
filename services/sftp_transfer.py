"""SFTP 文件/目录传输：流式读写、进度回调、目录递归、可协作取消。"""
from __future__ import annotations

import asyncio
import os
import posixpath
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import config
from services.paramiko_banner_fix import patch_banner_encoding, unpatch_banner_encoding
from services.ssh_connect import establish_ssh_client

ProgressEmit = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


@dataclass
class SftpTransferResult:
    success: bool
    error: Optional[str] = None
    bytes_transferred: int = 0
    files_transferred: int = 0
    duration_sec: float = 0.0
    interrupted: bool = False
    resolved_remote_path: Optional[str] = None


@dataclass
class _ProgressState:
    direction: str
    started_at: float = field(default_factory=time.time)
    total_bytes: int = 0
    transferred_bytes: int = 0
    files_total: int = 0
    file_index: int = 0
    current_file: str = ""
    last_emit_at: float = 0.0
    last_emit_pct: float = -1.0
    emit: Optional[ProgressEmit] = None
    cancel: Optional[CancelCheck] = None

    def check_cancel(self) -> bool:
        return bool(self.cancel and self.cancel())

    def _speed_bps(self) -> float:
        elapsed = max(0.001, time.time() - self.started_at)
        return self.transferred_bytes / elapsed

    def _eta_sec(self) -> Optional[float]:
        speed = self._speed_bps()
        if self.total_bytes <= 0 or speed <= 0:
            return None
        remain = max(0, self.total_bytes - self.transferred_bytes)
        return remain / speed

    def emit_progress(self, *, force: bool = False, phase: str = "running") -> None:
        if not self.emit:
            return
        now = time.time()
        pct = (
            round(100.0 * self.transferred_bytes / self.total_bytes, 1)
            if self.total_bytes > 0
            else 0.0
        )
        if not force:
            if now - self.last_emit_at < 0.45 and abs(pct - self.last_emit_pct) < 0.8:
                return
        self.last_emit_at = now
        self.last_emit_pct = pct
        eta = self._eta_sec()
        self.emit(
            {
                "kind": "transfer_progress",
                "phase": phase,
                "direction": self.direction,
                "transferred": self.transferred_bytes,
                "total": self.total_bytes,
                "percent": pct,
                "file": self.current_file,
                "file_index": self.file_index,
                "files_total": self.files_total,
                "elapsed_sec": round(now - self.started_at, 1),
                "speed_bps": int(self._speed_bps()),
                "eta_sec": round(eta, 1) if eta is not None else None,
            }
        )


def _is_dir_mode(st_mode: Optional[int]) -> bool:
    return st_mode is not None and stat.S_ISDIR(st_mode)


def _sftp_mkdir_p(sftp, remote_dir: str) -> None:
    parts = [p for p in remote_dir.replace("\\", "/").split("/") if p]
    cur = ""
    for part in parts:
        cur = f"{cur}/{part}" if cur else f"/{part}"
        try:
            sftp.mkdir(cur)
        except OSError:
            pass


def expand_sftp_tilde(sftp, remote_path: str) -> str:
    """将 ~/path 展开为 SFTP 会话 home 下的绝对路径（paramiko 不自动展开 ~）。"""
    p = (remote_path or "").replace("\\", "/").strip()
    if not p or p == "/":
        return p or "/"
    if p == "~":
        return sftp.normalize(".").replace("\\", "/")
    if p.startswith("~/"):
        home = sftp.normalize(".").replace("\\", "/").rstrip("/")
        suffix = p[2:].lstrip("/")
        return f"{home}/{suffix}" if suffix else home
    return p


def resolve_remote_push_target(
    sftp,
    remote_path: str,
    local_name: str,
) -> str:
    """解析单文件上传目标：展开 ~；remote 以 / 结尾或已存在为目录时追加 local 文件名。"""
    remote = expand_sftp_tilde(sftp, remote_path).replace("\\", "/")
    if remote.endswith("/"):
        return posixpath.join(remote.rstrip("/"), local_name)
    try:
        st = sftp.stat(remote)
        if _is_dir_mode(st.st_mode):
            return posixpath.join(remote.rstrip("/"), local_name)
    except OSError:
        pass
    return remote


def _local_tree_plan(local_root: Path) -> tuple[list[tuple[Path, str]], int]:
    """返回 [(本地绝对路径, 相对 posix 路径), ...] 与总字节。"""
    files: list[tuple[Path, str]] = []
    total = 0
    root = local_root.resolve()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            abs_p = Path(dirpath) / fname
            rel = abs_p.relative_to(root).as_posix()
            try:
                total += abs_p.stat().st_size
            except OSError:
                pass
            files.append((abs_p, rel))
    return files, total


def _cap_exceeded(size: int, cap: int) -> bool:
    """cap <= 0 表示不限制。"""
    return cap > 0 and size > cap


def _remote_tree_plan(
    sftp,
    remote_root: str,
    *,
    max_files: int,
    max_file_bytes: int,
    max_tree_bytes: int,
) -> tuple[list[tuple[str, str]], int, Optional[str]]:
    """返回 [(远端绝对路径, 相对 posix 路径), ...]、总字节、错误信息。"""
    remote_root = remote_root.replace("\\", "/").rstrip("/") or "/"
    out: list[tuple[str, str]] = []
    total = 0
    file_count = 0

    def _walk(rpath: str, rel: str) -> Optional[str]:
        nonlocal total, file_count
        try:
            entries = sftp.listdir_attr(rpath)
        except FileNotFoundError:
            return "远程路径不存在"
        except Exception as e:
            return str(e)
        for ent in entries:
            name = ent.filename
            if name in (".", ".."):
                continue
            child_rel = f"{rel}/{name}" if rel else name
            child_remote = posixpath.join(rpath, name) if rpath != "/" else f"/{name}"
            if _is_dir_mode(ent.st_mode):
                err = _walk(child_remote, child_rel)
                if err:
                    return err
            else:
                sz = int(ent.st_size or 0)
                if _cap_exceeded(sz, max_file_bytes):
                    return f"远程文件过大：{child_remote}（{sz} > {max_file_bytes}）"
                total += sz
                file_count += 1
                if max_files > 0 and file_count > max_files:
                    return f"远程目录文件数超过上限 {max_files}"
                if _cap_exceeded(total, max_tree_bytes):
                    return f"远程目录总大小超过上限 {max_tree_bytes} 字节"
                out.append((child_remote, child_rel))
        return None

    err = _walk(remote_root, "")
    if err:
        return [], 0, err
    return out, total, None


def _open_sftp_client(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    timeout: int,
):
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
    return client, client.open_sftp()


def _close_sftp(client, sftp) -> None:
    try:
        if sftp:
            sftp.close()
    except Exception:
        pass
    unpatch_banner_encoding()
    if client:
        try:
            client.close()
        except Exception:
            pass


def sftp_push_path_sync(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    local_path: str,
    remote_path: str,
    recursive: bool,
    timeout: int = 300,
    progress_emit: Optional[ProgressEmit] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> SftpTransferResult:
    started = time.time()
    local_p = Path(local_path).resolve()
    if not local_p.exists():
        return SftpTransferResult(False, error=f"本地路径不存在: {local_path}")

    client = None
    sftp = None
    state = _ProgressState(direction="push", emit=progress_emit, cancel=cancel_check)
    bytes_done = 0
    files_done = 0

    try:
        client, sftp = _open_sftp_client(
            host=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password=password,
            key_path=key_path,
            private_key_pem=private_key_pem,
            timeout=timeout,
        )

        if local_p.is_file():
            state.files_total = 1
            state.total_bytes = local_p.stat().st_size
            state.file_index = 1
            state.current_file = local_p.name
            state.emit_progress(force=True, phase="start")
            if state.check_cancel():
                return SftpTransferResult(False, error="传输已取消", interrupted=True, duration_sec=time.time() - started)

            remote_file = resolve_remote_push_target(sftp, remote_path, local_p.name)
            parent = posixpath.dirname(remote_file)
            if parent and parent not in ("", "/"):
                _sftp_mkdir_p(sftp, parent)

            file_base = transferred = [0]

            def _cb(tx: int, total: int) -> None:
                file_base[0] = tx
                state.transferred_bytes = bytes_done + tx
                state.emit_progress()

            sftp.put(str(local_p), remote_file, callback=_cb)
            bytes_done += local_p.stat().st_size
            files_done = 1
            resolved_remote = remote_file
        elif local_p.is_dir():
            if not recursive:
                return SftpTransferResult(False, error="本地路径为目录，请设置 recursive=true")
            plan, total = _local_tree_plan(local_p)
            max_files = int(getattr(config, "SCP_TRANSFER_MAX_FILES", 5000))
            if len(plan) > max_files:
                return SftpTransferResult(False, error=f"本地目录文件数超过上限 {max_files}")
            state.files_total = len(plan)
            state.total_bytes = total
            remote_base = expand_sftp_tilde(sftp, remote_path).replace("\\", "/").rstrip("/")
            _sftp_mkdir_p(sftp, remote_base)
            state.emit_progress(force=True, phase="start")
            resolved_remote = remote_base

            for idx, (abs_f, rel) in enumerate(plan, start=1):
                if state.check_cancel():
                    return SftpTransferResult(
                        False,
                        error="传输已取消",
                        interrupted=True,
                        bytes_transferred=bytes_done,
                        files_transferred=files_done,
                        duration_sec=time.time() - started,
                    )
                state.file_index = idx
                state.current_file = rel
                remote_file = posixpath.join(remote_base, rel)
                _sftp_mkdir_p(sftp, posixpath.dirname(remote_file))

                def _cb(tx: int, _total: int, _base=bytes_done) -> None:
                    state.transferred_bytes = _base + tx
                    state.emit_progress()

                try:
                    fsize = abs_f.stat().st_size
                except OSError:
                    fsize = 0
                sftp.put(str(abs_f), remote_file, callback=_cb)
                bytes_done += fsize
                files_done += 1
                state.transferred_bytes = bytes_done
                state.emit_progress(force=True)
        else:
            return SftpTransferResult(False, error="本地路径无效")

        state.emit_progress(force=True, phase="done")
        return SftpTransferResult(
            True,
            bytes_transferred=bytes_done,
            files_transferred=files_done,
            duration_sec=round(time.time() - started, 2),
            resolved_remote_path=resolved_remote,
        )
    except FileNotFoundError as e:
        return SftpTransferResult(
            False,
            error=f"远程路径不存在或无法访问（SFTP 不识别 ~，请用绝对路径或目录末尾加 /）：{e}",
            bytes_transferred=bytes_done,
            files_transferred=files_done,
            duration_sec=round(time.time() - started, 2),
        )
    except Exception as e:
        return SftpTransferResult(
            False,
            error=str(e),
            bytes_transferred=bytes_done,
            files_transferred=files_done,
            duration_sec=round(time.time() - started, 2),
        )
    finally:
        _close_sftp(client, sftp)


def sftp_pull_path_sync(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    local_path: str,
    recursive: bool,
    max_bytes: int,
    max_tree_bytes: int,
    timeout: int = 300,
    progress_emit: Optional[ProgressEmit] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> SftpTransferResult:
    started = time.time()
    client = None
    sftp = None
    state = _ProgressState(direction="pull", emit=progress_emit, cancel=cancel_check)
    bytes_done = 0
    files_done = 0
    local_root = Path(local_path).resolve()
    remote_norm = remote_path.replace("\\", "/")

    try:
        client, sftp = _open_sftp_client(
            host=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password=password,
            key_path=key_path,
            private_key_pem=private_key_pem,
            timeout=timeout,
        )
        try:
            st = sftp.stat(remote_norm)
        except FileNotFoundError:
            return SftpTransferResult(False, error="远程路径不存在")
        except Exception as e:
            return SftpTransferResult(False, error=str(e))

        if _is_dir_mode(st.st_mode):
            if not recursive:
                return SftpTransferResult(False, error="远程路径为目录，请设置 recursive=true")
            max_files = int(getattr(config, "SCP_TRANSFER_MAX_FILES", 5000))
            plan, total, plan_err = _remote_tree_plan(
                sftp,
                remote_norm,
                max_files=max_files,
                max_file_bytes=max_bytes,
                max_tree_bytes=max_tree_bytes,
            )
            if plan_err:
                return SftpTransferResult(False, error=plan_err)
            state.files_total = len(plan)
            state.total_bytes = total
            local_root.mkdir(parents=True, exist_ok=True)
            state.emit_progress(force=True, phase="start")

            for idx, (rfile, rel) in enumerate(plan, start=1):
                if state.check_cancel():
                    return SftpTransferResult(
                        False,
                        error="传输已取消",
                        interrupted=True,
                        bytes_transferred=bytes_done,
                        files_transferred=files_done,
                        duration_sec=time.time() - started,
                    )
                state.file_index = idx
                state.current_file = rel
                dest = local_root / rel.replace("/", os.sep)
                dest.parent.mkdir(parents=True, exist_ok=True)
                file_base = bytes_done

                def _read_cb(tx: int, _total: int, _base=file_base) -> None:
                    state.transferred_bytes = _base + tx
                    state.emit_progress()

                sftp.get(rfile, str(dest), callback=_read_cb)
                try:
                    fsize = dest.stat().st_size
                except OSError:
                    fsize = 0
                bytes_done += fsize
                files_done += 1
                state.transferred_bytes = bytes_done
                state.emit_progress(force=True)
        else:
            if st.st_size is not None and _cap_exceeded(int(st.st_size), max_bytes):
                return SftpTransferResult(
                    False,
                    error=f"远程文件过大（{st.st_size} 字节 > 上限 {max_bytes}）",
                )
            state.files_total = 1
            state.total_bytes = int(st.st_size or 0)
            state.file_index = 1
            state.current_file = Path(remote_norm).name
            local_root.parent.mkdir(parents=True, exist_ok=True)
            state.emit_progress(force=True, phase="start")
            if state.check_cancel():
                return SftpTransferResult(False, error="传输已取消", interrupted=True, duration_sec=time.time() - started)

            def _read_cb(tx: int, _total: int) -> None:
                state.transferred_bytes = tx
                state.emit_progress()
                if state.check_cancel():
                    raise InterruptedError("传输已取消")

            try:
                # 与 scp_push 的 sftp.put 对称：用 get + callback，调用卡进度一致
                sftp.get(remote_norm, str(local_root), callback=_read_cb)
            except InterruptedError:
                try:
                    if local_root.exists():
                        local_root.unlink()
                except OSError:
                    pass
                return SftpTransferResult(
                    False,
                    error="传输已取消",
                    interrupted=True,
                    bytes_transferred=state.transferred_bytes,
                    files_transferred=0,
                    duration_sec=round(time.time() - started, 2),
                )
            try:
                bytes_done = local_root.stat().st_size
            except OSError:
                bytes_done = int(state.transferred_bytes or 0)
            if _cap_exceeded(bytes_done, max_bytes):
                try:
                    if local_root.exists():
                        local_root.unlink()
                except OSError:
                    pass
                return SftpTransferResult(
                    False,
                    error=f"传输超过上限 {max_bytes} 字节（已中止）",
                    bytes_transferred=bytes_done,
                )
            state.transferred_bytes = bytes_done
            files_done = 1

        state.emit_progress(force=True, phase="done")
        return SftpTransferResult(
            True,
            bytes_transferred=bytes_done,
            files_transferred=files_done,
            duration_sec=round(time.time() - started, 2),
        )
    except ValueError as e:
        try:
            if local_root.is_file() and local_root.exists():
                local_root.unlink()
        except OSError:
            pass
        return SftpTransferResult(
            False,
            error=str(e),
            bytes_transferred=bytes_done,
            files_transferred=files_done,
            duration_sec=round(time.time() - started, 2),
        )
    except Exception as e:
        try:
            if local_root.is_file() and local_root.exists():
                local_root.unlink()
        except OSError:
            pass
        return SftpTransferResult(
            False,
            error=str(e),
            bytes_transferred=bytes_done,
            files_transferred=files_done,
            duration_sec=round(time.time() - started, 2),
        )
    finally:
        _close_sftp(client, sftp)


async def run_sftp_push_async(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    local_path: str,
    remote_path: str,
    recursive: bool,
    timeout: int,
    stream_callback: Optional[Any] = None,
    cancel_event: Optional[threading.Event] = None,
) -> SftpTransferResult:
    return await _run_transfer_async(
        sftp_push_path_sync,
        stream_callback=stream_callback,
        cancel_event=cancel_event,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        password=password,
        key_path=key_path,
        private_key_pem=private_key_pem,
        local_path=local_path,
        remote_path=remote_path,
        recursive=recursive,
        timeout=timeout,
    )


async def run_sftp_pull_async(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    local_path: str,
    recursive: bool,
    max_bytes: int,
    max_tree_bytes: int,
    timeout: int,
    stream_callback: Optional[Any] = None,
    cancel_event: Optional[threading.Event] = None,
) -> SftpTransferResult:
    return await _run_transfer_async(
        sftp_pull_path_sync,
        stream_callback=stream_callback,
        cancel_event=cancel_event,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        password=password,
        key_path=key_path,
        private_key_pem=private_key_pem,
        remote_path=remote_path,
        local_path=local_path,
        recursive=recursive,
        max_bytes=max_bytes,
        max_tree_bytes=max_tree_bytes,
        timeout=timeout,
    )


async def _run_transfer_async(
    sync_fn: Callable[..., SftpTransferResult],
    *,
    stream_callback: Optional[Any],
    cancel_event: Optional[threading.Event],
    **kwargs: Any,
) -> SftpTransferResult:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _emit(ev: dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ev)
        except RuntimeError:
            pass

    def _cancel() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    async def _drain() -> None:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.25)
                if stream_callback is not None:
                    try:
                        await stream_callback(ev)
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                if worker.done():
                    break

    worker = asyncio.create_task(
        asyncio.to_thread(
            sync_fn,
            progress_emit=_emit,
            cancel_check=_cancel,
            **kwargs,
        )
    )
    drainer = asyncio.create_task(_drain())
    try:
        result = await worker
    finally:
        drainer.cancel()
        try:
            await drainer
        except asyncio.CancelledError:
            pass
        while not queue.empty():
            ev = queue.get_nowait()
            if stream_callback is not None:
                try:
                    await stream_callback(ev)
                except Exception:
                    pass
    return result
