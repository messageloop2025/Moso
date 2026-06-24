"""本机管理 API：本机终端、本机文件系统、本机命令执行、Python 脚本执行、会话历史。仅管理员。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from database import get_db
from api.filesystem import get_user_fs_root
from services.terminal_input import expand_control_keys, is_control_only
from api.auth import get_current_user, require_admin, _is_admin_role, user_dict_for_websocket_from_token
from api.terminal import normalize_terminal_scope_id

logger = logging.getLogger("edgeops.local_host")

router = APIRouter(prefix="/api/local", tags=["本机管理"])

# Windows 下优先使用 ConPTY（pywinpty），与 Linux PTY 行为一致；无则回退 PIPE
try:
    from winpty import PtyProcess
    _has_pywinpty = True
except ImportError:
    PtyProcess = None  # type: ignore[misc, assignment]
    _has_pywinpty = False

# 本机文件系统：是否允许访问整个系统（任意绝对路径）；否则仅允许 LOCAL_ROOT 下
LOCAL_MANAGE_FULL_FS = getattr(config, "LOCAL_MANAGE_FULL_FS", False)
LOCAL_ROOT = getattr(config, "LOCAL_MANAGE_ROOT", None) or config.BASE_DIR
LOCAL_ROOT = Path(LOCAL_ROOT).resolve()
COMMAND_TIMEOUT = 300
SCRIPT_TIMEOUT = 300
BUFFER_MAX = 65536
PROCESS_MAX = 50  # 同时托管的子进程数量上限
PROCESS_STREAM_BUFFER_MAX = 2 * 1024 * 1024

# 内存中的本机“终端”会话：(user_id, scope_id, slot) -> { process, buffer, ws, task }
_local_sessions: dict[tuple[int, str, int], dict] = {}
# Windows ConPTY/pywinpty 异常断开记录窗口，仅作日志/诊断使用，不再触发自动降级到 PIPE。
_winpty_failure_times: list[float] = []
WINPTY_FAILURE_WINDOW_SEC = 120.0
WINPTY_FAILURE_THRESHOLD = 2
WINPTY_ABNORMAL_REASONS = {
    "pty_output_eof",
    "pty_process_exited",
    "pty_process_not_alive_before_write",
    "pty_write_failed",
    "websocket_send_failed",
    "websocket_send_timeout",
    "websocket_keepalive_timeout",
    "winpty_reader_dead",
    "unknown",
}


def _local_session_key(user_id: int, slot: int, scope_id: str | None = None) -> tuple[int, str, int]:
    return (user_id, normalize_terminal_scope_id(scope_id), max(0, min(int(slot), 31)))


def _local_scope_matches(session_scope: str, scope_id: str | None) -> bool:
    return session_scope == normalize_terminal_scope_id(scope_id)

# Windows 下本机终端独享的线程池：spawn / write / setwinsize 在这里执行，避免和 AI 工具
# 共用默认线程池（asyncio.to_thread 的 default executor）时被大量阻塞 I/O 拖死，
# 进而把 ConPTY 输出投递、PtyProcess.spawn 等关键操作排在长队列后端导致终端卡死。
# 仅在 Windows 平台创建并使用，不影响 Linux/Mac。
_LOCAL_TERM_EXECUTOR: ThreadPoolExecutor | None = None


def _get_local_term_executor() -> ThreadPoolExecutor | None:
    global _LOCAL_TERM_EXECUTOR
    if sys.platform != "win32":
        return None
    if _LOCAL_TERM_EXECUTOR is None:
        _LOCAL_TERM_EXECUTOR = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="edgeops-local-term",
        )
    return _LOCAL_TERM_EXECUTOR


async def _run_in_local_term_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Windows: 跑在本机终端专用线程池；Linux/Mac: 沿用 asyncio.to_thread，保持原行为不变。"""
    executor = _get_local_term_executor()
    if executor is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    if kwargs:
        call = partial(func, *args, **kwargs)
        return await loop.run_in_executor(executor, call)
    return await loop.run_in_executor(executor, func, *args)


def _local_proc_alive(proc) -> bool:
    """兼容 subprocess / asyncio subprocess / pywinpty 的进程存活判断。"""
    if proc is None:
        return False
    try:
        if hasattr(proc, "isalive"):
            return bool(proc.isalive())
    except Exception:
        return False
    try:
        if hasattr(proc, "poll"):
            return proc.poll() is None
    except Exception:
        pass
    return getattr(proc, "returncode", None) is None


def _terminate_local_proc(proc) -> None:
    """兼容关闭本机终端进程，避免 Windows pywinpty 没有 returncode 时重连失败。"""
    if proc is None:
        return
    terminated = False
    try:
        if hasattr(proc, "terminate"):
            proc.terminate()
            terminated = True
    except Exception:
        pass
    try:
        if hasattr(proc, "close"):
            proc.close()
            terminated = True
    except Exception:
        pass
    try:
        if hasattr(proc, "kill") and (not terminated or _local_proc_alive(proc)):
            proc.kill()
    except Exception:
        pass


def _windows_terminal_backend() -> str:
    backend = (getattr(config, "LOCAL_TERMINAL_WINDOWS_BACKEND", "auto") or "auto").strip().lower()
    if backend not in ("auto", "pywinpty", "pipe"):
        backend = "auto"
    return backend


def _should_use_winpty() -> bool:
    """Windows + 已安装 pywinpty 时一律走 ConPTY；不再自动降级到 PIPE（PIPE 模式下 cmd.exe
    无法真正交互，会让终端创建后没有任何输出）。仅当用户显式把 LOCAL_TERMINAL_WINDOWS_BACKEND
    设为 'pipe' 时才退化到 PIPE。"""
    if sys.platform != "win32" or not _has_pywinpty:
        return False
    return _windows_terminal_backend() != "pipe"


def _record_winpty_close_reason(reason: str) -> None:
    """记录 ConPTY 异常断开次数，仅作诊断/日志用途，不再触发自动降级到 PIPE。"""
    if reason not in WINPTY_ABNORMAL_REASONS:
        return
    now = time.monotonic()
    cutoff = now - WINPTY_FAILURE_WINDOW_SEC
    _winpty_failure_times[:] = [t for t in _winpty_failure_times if t >= cutoff]
    _winpty_failure_times.append(now)
    if len(_winpty_failure_times) >= WINPTY_FAILURE_THRESHOLD:
        logger.warning(
            "local shell winpty closed abnormally %d times in %.0fs window; latest reason=%s",
            len(_winpty_failure_times),
            WINPTY_FAILURE_WINDOW_SEC,
            reason,
        )


def _queue_winpty_output(queue: asyncio.Queue, item: str | None) -> None:
    """线程安全投递 pywinpty 输出；满队列时把最旧片段与新片段合并，保留输出顺序，
    避免大量输出时丢字符导致前端渲染错乱或回调异常导致连接悬挂。
    None 是 EOF 哨兵，遇到满队列时直接清空所有数据片段后投入。"""
    try:
        queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass
    if item is None:
        # EOF：把队列里的字符串片段全部排空，确保下游能尽快感知。
        try:
            while True:
                queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        return
    merged_chunks: list[str] = []
    try:
        # 最多吞 4 个最旧片段做合并，避免无限期阻塞读线程。
        for _ in range(4):
            head = queue.get_nowait()
            if head is None:
                # 已有 EOF 在前面，直接保留 EOF，丢弃当前片段（避免在 EOF 之后投入新数据）。
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                return
            if isinstance(head, str):
                merged_chunks.append(head)
    except asyncio.QueueEmpty:
        pass
    if isinstance(item, str):
        merged_chunks.append(item)
    merged = "".join(merged_chunks)
    try:
        queue.put_nowait(merged)
    except asyncio.QueueFull:
        # 极端情况下仍然失败：放弃合并块以避免读线程被永久阻塞，记录一次警告。
        logger.warning("local shell winpty output queue still full after merge; dropping %d chars", len(merged))


def _resolve_path(relative: str) -> Path:
    """将相对路径解析为绝对路径。全系统模式下可传绝对路径；否则必须落在 LOCAL_ROOT 内。Windows 下驱动器路径（如 C:/）始终允许。"""
    raw = (relative or "").strip().replace("\\", "/")
    if raw:
        # Windows 驱动器路径（C:、C:/、C:/path）始终允许，便于根目录列出驱动器后进入
        if sys.platform == "win32" and len(raw) >= 2 and raw[1] == ":":
            return Path(raw).resolve()
        if LOCAL_MANAGE_FULL_FS:
            if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
                return Path(raw).resolve()
            raw = raw.lstrip("/")
    path = raw.lstrip("/") if raw else ""
    if not path:
        return LOCAL_ROOT
    resolved = (LOCAL_ROOT / path).resolve()
    if not LOCAL_MANAGE_FULL_FS:
        try:
            resolved.relative_to(LOCAL_ROOT)
        except ValueError:
            raise HTTPException(status_code=400, detail="路径不允许访问")
    return resolved


# ── 供 AI Skills 调用的实现（仅管理员通过 execute_tool 调用）──
async def run_local_command_impl(command: str, timeout: int = 60, cwd: str | None = None) -> tuple[str, str, int]:
    """在本机执行 shell 命令，返回 (stdout, stderr, returncode)。"""
    cmd = (command or "").strip()
    if not cmd:
        return "", "命令为空", -1
    timeout = max(1, min(timeout or 60, COMMAND_TIMEOUT))
    cwd_str = str(LOCAL_ROOT)
    if cwd:
        try:
            cwd_str = str(_resolve_path(cwd))
        except HTTPException:
            pass
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd_str,
            env=os.environ.copy(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "命令执行超时", -1
    except Exception as e:
        return "", str(e), -1


async def run_local_script_impl(code: str = "", script_path: str = "", timeout: int = 120) -> tuple[str, str, int]:
    """在本机执行 Python 代码或脚本文件，返回 (stdout, stderr, returncode)。支持 requests/urllib/curl 等（需本机已安装）。"""
    code = (code or "").strip()
    path = (script_path or "").strip()
    if not code and not path:
        return "", "请提供 code 或 script_path", -1
    timeout = max(1, min(timeout or 120, SCRIPT_TIMEOUT))
    if path:
        fp = _resolve_path(path)
        if not fp.is_file():
            return "", "脚本文件不存在", -1
        code = fp.read_text(encoding="utf-8", errors="replace")
    try:
        proc = await asyncio.create_subprocess_shell(
            f'"{sys.executable}" -c {repr(code)}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(LOCAL_ROOT),
            env=os.environ.copy(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "脚本执行超时", -1
    except Exception as e:
        return "", str(e), -1


def _path_display(p: Path) -> str:
    """返回用于返回给调用方的路径字符串（全系统模式下为绝对路径）。"""
    if LOCAL_MANAGE_FULL_FS:
        return str(p.resolve()).replace("\\", "/")
    try:
        return str(p.relative_to(LOCAL_ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _windows_drives() -> list[dict]:
    """Windows：返回所有可用驱动器列表，作为「根目录」的虚拟子项。"""
    import string
    items = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            items.append({
                "name": f"{letter}:",
                "path": f"{letter}:/",
                "is_dir": True,
                "size": None,
            })
    return items


async def local_fs_list_impl(path: str = "") -> list[dict]:
    """列出本机目录下的条目。Windows 根路径（空或 /）返回所有驱动器；Linux 根路径返回操作系统根目录 /。"""
    def _list() -> list[dict]:
        raw = (path or "").strip().replace("\\", "/").strip("/")
        if sys.platform == "win32" and raw == "":
            return _windows_drives()
        if sys.platform != "win32" and raw == "":
            root = Path("/")
            if not root.is_dir():
                return []
            items = []
            for p in sorted(root.iterdir()):
                try:
                    stat = p.stat()
                    items.append({
                        "name": p.name,
                        "path": str(p).replace("\\", "/"),
                        "is_dir": p.is_dir(),
                        "size": stat.st_size if p.is_file() else None,
                    })
                except OSError:
                    pass
            return items
        root = _resolve_path(path)
        if not root.is_dir():
            return []
        items = []
        for p in sorted(root.iterdir()):
            try:
                stat = p.stat()
                items.append({
                    "name": p.name,
                    "path": _path_display(p),
                    "is_dir": p.is_dir(),
                    "size": stat.st_size if p.is_file() else None,
                })
            except OSError:
                pass
        return items
    return await asyncio.to_thread(_list)


async def local_fs_read_impl(path: str) -> str:
    """读取本机文件内容。"""
    def _read() -> str:
        fp = _resolve_path(path)
        if not fp.is_file():
            raise ValueError("不是文件或不存在")
        return fp.read_text(encoding="utf-8", errors="replace")
    return await asyncio.to_thread(_read)


async def local_fs_write_impl(path: str, content: str) -> None:
    """写入本机文件。"""
    def _write() -> None:
        fp = _resolve_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8", errors="replace")
    await asyncio.to_thread(_write)


async def local_fs_mkdir_impl(path: str) -> None:
    """创建目录（含父目录）。"""
    def _mkdir() -> None:
        fp = _resolve_path(path)
        fp.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_mkdir)


async def local_fs_delete_impl(path: str, recursive: bool = False) -> None:
    """删除文件或目录。recursive=True 时递归删除非空目录。"""
    def _delete() -> None:
        import shutil
        fp = _resolve_path(path)
        if not fp.exists():
            raise ValueError("路径不存在")
        if fp.is_file():
            fp.unlink()
        elif fp.is_dir():
            if recursive:
                shutil.rmtree(fp)
            else:
                fp.rmdir()
    await asyncio.to_thread(_delete)


async def local_fs_rename_impl(src: str, dst: str) -> None:
    """移动/重命名文件或目录。"""
    def _rename() -> None:
        s = _resolve_path(src)
        d = _resolve_path(dst)
        if not s.exists():
            raise ValueError("源路径不存在")
        s.rename(d)
    await asyncio.to_thread(_rename)


async def local_fs_truncate_impl(path: str, size: int = 0) -> None:
    """将文件截断为指定长度（字节）；size=0 表示清空文件。"""
    def _truncate() -> None:
        fp = _resolve_path(path)
        if not fp.is_file():
            raise ValueError("不是文件或不存在")
        with open(fp, "r+b") as f:
            f.truncate(max(0, size))
    await asyncio.to_thread(_truncate)


def _b64_encode(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _b64_decode(s: str) -> bytes:
    import base64
    return base64.b64decode(s)


async def local_fs_read_binary_impl(path: str, offset: int = 0, size: int | None = None) -> str:
    """从文件指定偏移读取二进制内容，返回 base64 字符串。size 为空则读到末尾。"""
    def _read_binary() -> str:
        fp = _resolve_path(path)
        if not fp.is_file():
            raise ValueError("不是文件或不存在")
        with open(fp, "rb") as f:
            f.seek(max(0, offset))
            data = f.read(size) if size is not None and size > 0 else f.read()
        return _b64_encode(data)
    return await asyncio.to_thread(_read_binary)


async def local_fs_write_binary_impl(
    path: str, content_b64: str, offset: int | None = None, truncate: bool = False
) -> None:
    """写入二进制内容（content 为 base64）。offset 为 None 时：truncate=True 先清空再写，否则追加。offset 非空时从该偏移写入。"""
    def _write_binary() -> None:
        fp = _resolve_path(path)
        data = _b64_decode(content_b64)
        fp.parent.mkdir(parents=True, exist_ok=True)
        if offset is not None:
            with open(fp, "r+b") as f:
                f.seek(max(0, offset))
                f.write(data)
        else:
            mode = "wb" if truncate else "ab"
            with open(fp, mode) as f:
                f.write(data)
    await asyncio.to_thread(_write_binary)


# ── 本机进程托管（供 AI 调用）──
_managed_processes: dict[int, dict] = {}  # pid -> {proc, stdin_queue, stdout_buf, stderr_buf, ...}


async def _drain_stream(stream: asyncio.StreamReader, buf: list[bytes]) -> None:
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf.append(chunk)
            total = sum(len(x) for x in buf)
            while total > PROCESS_STREAM_BUFFER_MAX and buf:
                dropped = buf.pop(0)
                total -= len(dropped)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    except asyncio.CancelledError:
        pass


async def process_start_impl(command: str, cwd: str | None = None, env: dict | None = None) -> dict:
    """启动本机子进程（shell 命令），返回 pid、说明。进程 stdin/stdout/stderr 可后续读写/等待。"""
    if len(_managed_processes) >= PROCESS_MAX:
        raise ValueError(f"托管进程数已达上限 {PROCESS_MAX}")
    cmd = (command or "").strip()
    if not cmd:
        raise ValueError("命令为空")
    cwd_str = str(LOCAL_ROOT)
    if cwd:
        try:
            cwd_str = str(_resolve_path(cwd))
        except HTTPException:
            pass
    env_full = os.environ.copy()
    if env:
        env_full.update(env)
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd_str,
        env=env_full,
    )
    pid = proc.pid
    stdout_buf: list[bytes] = []
    stderr_buf: list[bytes] = []
    _managed_processes[pid] = {
        "proc": proc,
        "command": cmd,
        "stdout_buf": stdout_buf,
        "stderr_buf": stderr_buf,
        "stdin_closed": False,
    }
    asyncio.create_task(_drain_stream(proc.stdout, stdout_buf))
    asyncio.create_task(_drain_stream(proc.stderr, stderr_buf))
    return {"pid": pid, "command": cmd, "message": "进程已启动"}


def _get_managed(pid: int) -> dict:
    if pid not in _managed_processes:
        raise ValueError("进程不存在或已退出")
    return _managed_processes[pid]


def _maybe_cleanup_managed(pid: int, d: dict) -> None:
    """进程已退出且 stdout/stderr 缓冲都读空后，自动回收托管记录。"""
    try:
        proc = d.get("proc")
        if not proc or proc.returncode is None:
            return
        if d.get("stdout_buf") or d.get("stderr_buf"):
            return
        _managed_processes.pop(pid, None)
    except Exception:
        pass


async def process_terminate_impl(pid: int, force: bool = False) -> None:
    """终止本机托管进程。force=True 使用 SIGKILL/强制结束。"""
    d = _get_managed(pid)
    proc = d["proc"]
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        pass
    finally:
        _managed_processes.pop(pid, None)


async def process_wait_impl(pid: int, timeout: float | None = None) -> dict:
    """等待托管进程结束，返回 returncode。超时后若未结束则返回 timeout=True。"""
    d = _get_managed(pid)
    proc = d["proc"]
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=timeout or 3600)
    except asyncio.TimeoutError:
        return {"returncode": None, "timeout": True}
    # 不立即移除：允许调用方在 wait 后继续读取 stdout/stderr。
    _maybe_cleanup_managed(pid, d)
    return {"returncode": code, "timeout": False}


async def process_stdin_write_impl(pid: int, data: str) -> None:
    """向托管进程的标准输入写入内容（文本）。"""
    d = _get_managed(pid)
    proc = d["proc"]
    if proc.stdin is None or d.get("stdin_closed"):
        raise ValueError("进程 stdin 不可用或已关闭")
    proc.stdin.write(data.encode("utf-8", errors="replace"))
    await proc.stdin.drain()


async def process_stdin_close_impl(pid: int) -> None:
    """关闭托管进程的标准输入（EOF）。"""
    d = _get_managed(pid)
    if d.get("stdin_closed"):
        return
    proc = d["proc"]
    if proc.stdin:
        proc.stdin.close()
        await proc.stdin.wait_closed()
    d["stdin_closed"] = True


async def process_stdout_read_impl(pid: int, max_bytes: int = 65536) -> str:
    """读取托管进程至今的标准输出（已缓冲部分），返回 base64。"""
    d = _get_managed(pid)
    buf = d["stdout_buf"]
    data = b"".join(buf)
    buf.clear()
    if max_bytes and len(data) > max_bytes:
        data = data[:max_bytes]
    _maybe_cleanup_managed(pid, d)
    return _b64_encode(data)


async def process_stderr_read_impl(pid: int, max_bytes: int = 65536) -> str:
    """读取托管进程至今的标准错误（已缓冲部分），返回 base64。"""
    d = _get_managed(pid)
    buf = d["stderr_buf"]
    data = b"".join(buf)
    buf.clear()
    if max_bytes and len(data) > max_bytes:
        data = data[:max_bytes]
    _maybe_cleanup_managed(pid, d)
    return _b64_encode(data)


async def process_list_impl() -> list[dict]:
    """列出当前托管的进程（pid、command 需在 start 时已存）。"""
    out = []
    for pid, d in list(_managed_processes.items()):
        proc = d["proc"]
        out.append({
            "pid": pid,
            "command": d.get("command", ""),
            "returncode": proc.returncode if proc.returncode is not None else None,
            "alive": proc.returncode is None,
        })
    return out


# ── 本机文件系统 ──
class LocalFsListRequest(BaseModel):
    path: str = ""


class LocalFsReadRequest(BaseModel):
    path: str
    encoding: str = "utf-8"


class LocalFsWriteRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"


class LocalFsMkdirRequest(BaseModel):
    path: str


@router.get("/fs/list")
async def local_fs_list(path: str = "", user=Depends(require_admin)):
    """列出本机目录下的条目。Windows 根路径返回所有驱动器；Linux 根路径返回 /；否则为 LOCAL_ROOT 或绝对路径。"""
    raw = (path or "").strip().replace("\\", "/").strip("/")
    if sys.platform == "win32" and raw == "":
        items = [{"name": x["name"], "path": x["path"], "is_dir": x["is_dir"], "size": x["size"], "mtime": None} for x in _windows_drives()]
        return {"success": True, "path": path or "/", "items": items, "root": "/"}
    if sys.platform != "win32" and raw == "":
        root = Path("/")
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="不是目录")
        items = []
        try:
            for p in sorted(root.iterdir()):
                try:
                    stat = p.stat()
                    items.append({
                        "name": p.name,
                        "path": str(p).replace("\\", "/"),
                        "is_dir": p.is_dir(),
                        "size": stat.st_size if p.is_file() else None,
                        "mtime": stat.st_mtime,
                    })
                except OSError:
                    pass
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"success": True, "path": path or "/", "items": items, "root": "/"}
    root = _resolve_path(path)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")
    items = []
    try:
        for p in sorted(root.iterdir()):
            try:
                stat = p.stat()
                items.append({
                    "name": p.name,
                    "path": _path_display(p),
                    "is_dir": p.is_dir(),
                    "size": stat.st_size if p.is_file() else None,
                    "mtime": stat.st_mtime,
                })
            except OSError:
                pass
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"success": True, "path": path or "/", "items": items, "root": str(LOCAL_ROOT)}


@router.post("/fs/read")
async def local_fs_read(req: LocalFsReadRequest, user=Depends(require_admin)):
    """读取本机文件内容（文本）。大于 2MB 或二进制文件不预览，返回提示。"""
    fp = _resolve_path(req.path)
    if not fp.is_file():
        raise HTTPException(status_code=400, detail="不是文件或不存在")
    try:
        stat = fp.stat()
        if stat.st_size > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="文本过大或非文本文件")
        data = fp.read_bytes()
        if b"\x00" in data[:65536]:
            raise HTTPException(status_code=400, detail="文本过大或非文本文件")
        content = data.decode(req.encoding or "utf-8", errors="strict")
    except HTTPException:
        raise
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文本过大或非文本文件")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"success": True, "path": req.path, "content": content}


@router.post("/fs/write")
async def local_fs_write(req: LocalFsWriteRequest, user=Depends(require_admin)):
    """写入本机文件（覆盖）。"""
    fp = _resolve_path(req.path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        fp.write_text(req.content, encoding=req.encoding or "utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"success": True, "path": req.path}


@router.post("/fs/mkdir")
async def local_fs_mkdir(req: LocalFsMkdirRequest, user=Depends(require_admin)):
    """在本机创建目录。"""
    fp = _resolve_path(req.path)
    try:
        fp.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"success": True, "path": req.path}


class LocalFsDeleteRequest(BaseModel):
    path: str


class LocalFsRenameRequest(BaseModel):
    path: str
    new_path: str


@router.delete("/fs/delete")
async def local_fs_delete(path: str = "", user=Depends(require_admin)):
    """删除本机文件或目录（含非空目录）。"""
    path = (path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="请指定路径")
    try:
        await local_fs_delete_impl(path, recursive=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


@router.post("/fs/rename")
async def local_fs_rename(req: LocalFsRenameRequest, user=Depends(require_admin)):
    """本机重命名/移动文件或目录。"""
    new_path = (req.new_path or "").strip()
    if not new_path:
        raise HTTPException(status_code=400, detail="请指定新路径")
    try:
        await local_fs_rename_impl(req.path, new_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "path": req.path, "new_path": new_path}


@router.post("/fs/upload")
async def local_fs_upload(
    path: str = Form(""),
    file: UploadFile = File(...),
    user=Depends(require_admin),
):
    """本机管理上传：统一落到 web/fs/<username>/local/YYYY/MM/DD/uuid+功能名.ext，避免散落。"""
    # path 仅作为“日期目录下的子目录”使用，禁止绝对路径/上跳，便于多文件按目录组织。
    raw_subdir = (path or "").replace("\\", "/").strip().lstrip("/")
    clean_parts: list[str] = []
    if raw_subdir:
        for seg in raw_subdir.split("/"):
            s = re.sub(r"[^A-Za-z0-9._-]+", "_", (seg or "").strip())
            s = s.strip("._-")
            if not s or s in (".", ".."):
                continue
            clean_parts.append(s[:64])
    safe_subdir = "/".join(clean_parts[:8]) if clean_parts else ""
    name = (file.filename or "upload.bin").replace("\\", "/").strip().split("/")[-1]
    if not name or ".." in name:
        raise HTTPException(status_code=400, detail="文件名无效")
    try:
        now = datetime.now()
        date_dir = f"local/{now.year:04d}/{now.month:02d}/{now.day:02d}"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem or "upload").strip("._-") or "upload"
        stem = stem[:48]
        suffix = Path(name).suffix
        if len(suffix) > 16 or (suffix and not re.fullmatch(r"\.[A-Za-z0-9._-]+", suffix)):
            suffix = ".bin"
        final_name = f"{uuid4().hex}_{stem}{suffix or '.bin'}"
        rel_dir = f"{date_dir}/{safe_subdir}" if safe_subdir else date_dir
        target = (get_user_fs_root(user) / rel_dir / final_name).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        content = await file.read()
        target.write_bytes(content)
        return {
            "success": True,
            "path": str(target).replace("\\", "/"),
            "managed_relative_path": f"{rel_dir}/{final_name}",
            "requested_name": name,
            "requested_subdir": raw_subdir,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fs/download")
async def local_fs_download(path: str = "", user=Depends(require_admin)):
    """下载本机文件。"""
    path = (path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="请指定文件路径")
    fp = _resolve_path(path)
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(fp), filename=fp.name)


# ── 本机命令执行 ──
class LocalExecuteRequest(BaseModel):
    command: str
    timeout: int = 60
    session_id: int | None = None
    cwd: str | None = None


@router.post("/execute")
async def local_execute(req: LocalExecuteRequest, user=Depends(require_admin)):
    """在本机执行一条 shell 命令，返回 stdout、stderr、returncode。"""
    cmd = (req.command or "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="命令不能为空")
    timeout = max(1, min(req.timeout or 60, COMMAND_TIMEOUT))
    cwd = None
    if req.cwd:
        try:
            cwd = str(_resolve_path(req.cwd))
        except HTTPException:
            pass
    if not cwd:
        cwd = str(LOCAL_ROOT)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=os.environ.copy(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        code = proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise HTTPException(status_code=408, detail="命令执行超时")
    except Exception as e:
        logger.exception("local execute: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    # 可选：写入会话日志
    if req.session_id and req.session_id > 0:
        db = await get_db()
        await db.execute(
            "INSERT INTO local_shell_logs (session_id, kind, content) VALUES (?, ?, ?)",
            (req.session_id, "command", cmd),
        )
        await db.execute(
            "INSERT INTO local_shell_logs (session_id, kind, content) VALUES (?, ?, ?)",
            (req.session_id, "stdout", out[:50000]),
        )
        if err:
            await db.execute(
                "INSERT INTO local_shell_logs (session_id, kind, content) VALUES (?, ?, ?)",
                (req.session_id, "stderr", err[:50000]),
            )
        await db.execute("UPDATE local_shell_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (req.session_id,))
        await db.commit()
    return {"success": True, "stdout": out, "stderr": err, "returncode": code}


# ── Python 脚本执行 ──
class LocalRunScriptRequest(BaseModel):
    code: str = ""
    script_path: str = ""
    timeout: int = 120
    session_id: int | None = None


@router.post("/run-script")
async def local_run_script(req: LocalRunScriptRequest, user=Depends(require_admin)):
    """在本机执行 Python 代码（code 直接传入）或脚本文件（script_path 相对 LOCAL_ROOT）。支持网络请求（requests/urllib/curl 等需本机已安装）。"""
    code = (req.code or "").strip()
    path = (req.script_path or "").strip()
    if not code and not path:
        raise HTTPException(status_code=400, detail="请提供 code 或 script_path")
    timeout = max(1, min(req.timeout or 120, SCRIPT_TIMEOUT))
    if path:
        fp = _resolve_path(path)
        if not fp.is_file():
            raise HTTPException(status_code=400, detail="脚本文件不存在")
        code = fp.read_text(encoding="utf-8", errors="replace")
    # 使用当前进程的 Python 执行（子进程会继承环境，可调用 requests/curl 等）
    try:
        proc = await asyncio.create_subprocess_shell(
            f'"{sys.executable}" -c {repr(code)}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(LOCAL_ROOT),
            env=os.environ.copy(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        code_ret = proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise HTTPException(status_code=408, detail="脚本执行超时")
    except Exception as e:
        logger.exception("local run-script: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    if req.session_id and req.session_id > 0:
        db = await get_db()
        await db.execute(
            "INSERT INTO local_shell_logs (session_id, kind, content) VALUES (?, ?, ?)",
            (req.session_id, "script", req.code or f"@file:{path}"),
        )
        await db.execute(
            "INSERT INTO local_shell_logs (session_id, kind, content) VALUES (?, ?, ?)",
            (req.session_id, "stdout", out[:50000]),
        )
        if err:
            await db.execute(
                "INSERT INTO local_shell_logs (session_id, kind, content) VALUES (?, ?, ?)",
                (req.session_id, "stderr", err[:50000]),
            )
        await db.execute("UPDATE local_shell_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (req.session_id,))
        await db.commit()
    return {"success": True, "stdout": out, "stderr": err, "returncode": code_ret}


# ── 会话历史 ──
@router.get("/sessions")
async def local_sessions_list(user=Depends(require_admin)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, title, created_at, updated_at FROM local_shell_sessions WHERE user_id = ? ORDER BY updated_at DESC",
        (user["id"],),
    )
    return {"success": True, "sessions": [dict(r) for r in rows]}


@router.post("/sessions")
async def local_sessions_create(
    title: str = "本机会话",
    user=Depends(require_admin),
):
    db = await get_db()
    await db.execute(
        "INSERT INTO local_shell_sessions (user_id, title) VALUES (?, ?)",
        (user["id"], title or "本机会话"),
    )
    await db.commit()
    cursor = await db.execute("SELECT last_insert_rowid()")
    row = await cursor.fetchone()
    sid = row[0] if row else None
    return {"success": True, "id": sid, "title": title or "本机会话"}


@router.get("/sessions/{session_id}/logs")
async def local_sessions_logs(session_id: int, user=Depends(require_admin)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, session_id FROM local_shell_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    logs = await db.execute_fetchall(
        "SELECT id, kind, content, created_at FROM local_shell_logs WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    return {"success": True, "logs": [dict(r) for r in logs]}


# ── 本机终端 WebSocket（持久化交互：Linux 用 PTY，Windows 优先 ConPTY/pywinpty，无则 PIPE）──
def _local_shell_cmd():
    """返回本机交互 shell 命令：Windows 为 cmd.exe /K，Linux 为 bash -i。"""
    if sys.platform == "win32":
        return "cmd.exe /K"
    return "exec bash -i 2>/dev/null || exec sh -i"


async def _run_local_shell_pty_unix(ws: WebSocket, buffer: list, buffer_size: list, cwd: str, session_key: tuple):
    """Linux：使用 PTY，使 Ctrl+C 等控制字符正确传递给 shell。"""
    import pty
    import fcntl
    import struct
    import termios
    master, slave = pty.openpty()
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-i"] if os.path.exists("/bin/bash") else ["sh", "-i"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
    except Exception as e:
        os.close(master)
        try:
            asyncio.get_event_loop().create_task(ws.send_text(f"\r\n[启动 shell 失败] {e}\r\n"))
        except Exception:
            pass
        return
    if session_key in _local_sessions:
        _local_sessions[session_key]["proc"] = proc
        _local_sessions[session_key]["master_fd"] = master

    loop = asyncio.get_event_loop()
    closed = [False]  # use list to allow closure update

    out_queue: asyncio.Queue = asyncio.Queue(maxsize=128)

    def on_master_readable():
        if closed[0]:
            return
        try:
            data = os.read(master, 4096)
            if not data:
                closed[0] = True
                return
            text = data.decode("utf-8", errors="replace")
            buffer.append(text)
            buffer_size[0] += len(text)
            session = _local_sessions.get(session_key)
            if session is not None:
                session["last_output_at"] = time.time()
            while buffer_size[0] > BUFFER_MAX and buffer:
                first = buffer.pop(0)
                buffer_size[0] -= len(first)
            try:
                out_queue.put_nowait(text)
            except asyncio.QueueFull:
                pass
        except OSError:
            closed[0] = True
        except Exception as e:
            logger.exception("local shell pty read: %s", e)

    loop.add_reader(master, on_master_readable)

    async def drain_out():
        while not closed[0]:
            try:
                text = await asyncio.wait_for(out_queue.get(), timeout=0.5)
                await ws.send_text(text)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    async def read_ws():
        try:
            while not closed[0]:
                raw = await ws.receive_text()
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and obj.get("type") == "input" and "data" in obj:
                        raw = obj["data"]
                    elif isinstance(obj, dict) and obj.get("type") == "resize":
                        rows = int(obj.get("rows") or 24)
                        cols = int(obj.get("cols") or 80)
                        try:
                            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                        except Exception:
                            pass
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
                if not closed[0] and proc.poll() is None:
                    try:
                        os.write(master, raw.encode("utf-8", errors="replace"))
                    except (OSError, BrokenPipeError):
                        break
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as e:
            logger.exception("local shell read_ws: %s", e)
        finally:
            closed[0] = True
            loop.remove_reader(master)
            try:
                os.close(master)
            except OSError:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except Exception:
                    pass

    drain_task = loop.create_task(drain_out())
    try:
        await read_ws()
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass


def _winpty_read_thread(
    proc: "PtyProcess",
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    session_ref: dict | None = None,
) -> None:
    """在后台线程中从 Windows PTY 读数据并放入 asyncio 队列。
    退出时把退出原因写回 session，供 _local_terminal_io_ready / 诊断接口判断半死状态。"""
    exit_reason = "process_exited"
    try:
        while proc.isalive():
            try:
                data = proc.read(4096)
                loop.call_soon_threadsafe(_queue_winpty_output, queue, data)
            except EOFError:
                logger.warning("local shell winpty read EOF")
                exit_reason = "eof"
                return
            except Exception as e:
                logger.warning("local shell winpty read stopped: %s", e)
                exit_reason = f"exception:{type(e).__name__}"
                return
    finally:
        if isinstance(session_ref, dict):
            session_ref["reader_alive"] = False
            session_ref["reader_exit_reason"] = exit_reason
            session_ref["reader_exit_at"] = time.time()
        loop.call_soon_threadsafe(_queue_winpty_output, queue, None)


async def _run_local_shell_pty_win(ws: WebSocket, buffer: list, buffer_size: list, cwd: str, session_key: tuple):
    """Windows：使用 pywinpty（ConPTY），与 Linux PTY 行为一致，支持回显、历史、Ctrl+C。
    所有阻塞调用（spawn/write/setwinsize）走本机终端专用线程池，避免被 AI 工具的默认线程池拖死。"""
    import threading
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    try:
        proc = await _run_in_local_term_thread(
            PtyProcess.spawn,
            "cmd.exe /K",
            cwd=cwd,
            env=env,
            dimensions=(24, 80),
        )
    except Exception as e:
        logger.warning("local shell winpty spawn failed: %s", e)
        await ws.send_text(f"\r\n[启动 shell 失败] {e}\r\n")
        return
    session = _local_sessions.get(session_key)
    if session is not None:
        session["proc"] = proc
        session["reader_alive"] = True
        session["reader_exit_reason"] = None
        session["reader_exit_at"] = None
    loop = asyncio.get_event_loop()
    # 输出队列扩容到 4096，配合 _queue_winpty_output 的合并策略，能缓冲约 16MB 突发输出。
    out_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    reader = threading.Thread(
        target=_winpty_read_thread,
        args=(proc, out_queue, loop, session),
        daemon=True,
        name=f"edgeops-winpty-reader-{session_key[0]}-{session_key[2]}",
    )
    reader.start()
    disconnect_reason = "unknown"
    KEEPALIVE_SEC = 15.0

    WS_SEND_TIMEOUT_SEC = 10.0

    async def _safe_ws_send_text(text: str) -> bool:
        try:
            await asyncio.wait_for(ws.send_text(text), timeout=WS_SEND_TIMEOUT_SEC)
            return True
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    async def _safe_ws_send_json(payload: dict) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=WS_SEND_TIMEOUT_SEC)
            return True
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    async def drain_out():
        nonlocal disconnect_reason
        last_send_at = loop.time()
        while True:
            try:
                text = await asyncio.wait_for(out_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if not proc.isalive():
                    disconnect_reason = "pty_process_exited"
                    break
                sess = _local_sessions.get(session_key)
                if sess is not None and sess.get("reader_alive") is False:
                    disconnect_reason = "winpty_reader_dead"
                    break
                # WS 心跳：长时间没有 PTY 输出时主动发一帧，提前发现客户端/IOCP 已死。
                # 并且对 send_json 加超时，避免 IOCP 完成回调卡住时 drain_out 永久阻塞。
                now = loop.time()
                if now - last_send_at >= KEEPALIVE_SEC:
                    ok = await _safe_ws_send_json({"type": "keepalive", "ts": time.time()})
                    if not ok:
                        disconnect_reason = "websocket_keepalive_timeout"
                        break
                    last_send_at = now
                continue
            if text is None:
                disconnect_reason = "pty_output_eof"
                break
            buffer.append(text)
            buffer_size[0] += len(text)
            sess = _local_sessions.get(session_key)
            if sess is not None:
                sess["last_output_at"] = time.time()
            while buffer_size[0] > BUFFER_MAX and buffer:
                first = buffer.pop(0)
                buffer_size[0] -= len(first)
            ok = await _safe_ws_send_text(text)
            if not ok:
                disconnect_reason = "websocket_send_timeout"
                break
            last_send_at = loop.time()

    async def read_ws():
        nonlocal disconnect_reason
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and obj.get("type") == "input" and "data" in obj:
                        raw = obj["data"]
                    elif isinstance(obj, dict) and obj.get("type") == "resize":
                        rows = max(1, min(int(obj.get("rows") or 24), 2048))
                        cols = max(1, min(int(obj.get("cols") or 80), 512))
                        try:
                            await _run_in_local_term_thread(proc.setwinsize, rows, cols)
                        except Exception:
                            pass
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
                if not proc.isalive():
                    disconnect_reason = "pty_process_not_alive_before_write"
                    break
                sess = _local_sessions.get(session_key)
                if sess is not None and sess.get("reader_alive") is False:
                    disconnect_reason = "winpty_reader_dead"
                    break
                try:
                    await _run_in_local_term_thread(proc.write, raw)
                except (EOFError, OSError, BrokenPipeError):
                    disconnect_reason = "pty_write_failed"
                    break
        except (WebSocketDisconnect, RuntimeError):
            disconnect_reason = "websocket_disconnected"
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("local shell winpty read_ws: %s", e)

    drain_task = asyncio.create_task(drain_out())
    read_task = asyncio.create_task(read_ws())
    try:
        done, pending = await asyncio.wait({drain_task, read_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        sess = _local_sessions.get(session_key) or {}
        logger.warning(
            "local shell winpty session closing: user_id=%s scope=%s slot=%s reason=%s proc_alive=%s reader_alive=%s reader_exit=%s",
            session_key[0],
            session_key[1],
            session_key[2],
            disconnect_reason,
            _local_proc_alive(proc),
            sess.get("reader_alive"),
            sess.get("reader_exit_reason"),
        )
        _record_winpty_close_reason(disconnect_reason)
        try:
            await ws.send_json({"type": "closed", "reason": disconnect_reason, "slot": session_key[2]})
        except Exception:
            pass
        _terminate_local_proc(proc)
        # 等读线程自然退出，避免悬挂线程持续占用 ConPTY 句柄影响下次新建。
        try:
            reader.join(timeout=2.0)
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


async def _run_local_shell_pty(ws: WebSocket, buffer: list, buffer_size: list, cwd: str, session_key: tuple):
    """在子进程中启动持久化 shell，与 WebSocket 桥接。Linux 用 PTY，Windows 优先 ConPTY（pywinpty），否则 PIPE。"""
    if sys.platform != "win32":
        await _run_local_shell_pty_unix(ws, buffer, buffer_size, cwd, session_key)
        return
    if _should_use_winpty():
        await _run_local_shell_pty_win(ws, buffer, buffer_size, cwd, session_key)
        return
    # Windows 无 pywinpty 时回退：PIPE，无回显/历史
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe",
                "/K",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=env,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                _local_shell_cmd(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=env,
            )
        if session_key in _local_sessions:
            _local_sessions[session_key]["proc"] = proc
    except Exception as e:
        await ws.send_text(f"\r\n[启动 shell 失败] {e}\r\n")
        return

    async def read_out():
        try:
            while proc.returncode is None or proc.stdout:
                data = await proc.stdout.read(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                buffer.append(text)
                buffer_size[0] += len(text)
                session = _local_sessions.get(session_key)
                if session is not None:
                    session["last_output_at"] = time.time()
                while buffer_size[0] > BUFFER_MAX and buffer:
                    first = buffer.pop(0)
                    buffer_size[0] -= len(first)
                try:
                    await ws.send_text(text)
                except Exception:
                    break
        except (BrokenPipeError, ConnectionResetError, WebSocketDisconnect):
            pass
        except Exception as e:
            logger.exception("local shell read_out: %s", e)

    async def read_ws():
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and obj.get("type") == "input" and "data" in obj:
                        raw = obj["data"]
                    elif isinstance(obj, dict) and obj.get("type") == "resize":
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
                if proc.stdin and proc.returncode is None:
                    try:
                        if sys.platform == "win32":
                            raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
                        proc.stdin.write(raw.encode("utf-8", errors="replace"))
                        await proc.stdin.drain()
                    except Exception:
                        break
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as e:
            logger.exception("local shell read_ws: %s", e)

    read_task = asyncio.create_task(read_out())
    try:
        await read_ws()
    finally:
        read_task.cancel()
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except Exception:
                pass


LOCAL_TERMINAL_CONNECT_WAIT_MAX_SEC = 5.0
LOCAL_TERMINAL_CONNECT_POLL_SEC = 0.25


def _local_terminal_io_ready(session: dict | None) -> bool:
    """本机控制台会话已可写入（PTY master 或 ConPTY/pywinpty/stdin 就绪）。
    Windows pywinpty 路径下若读线程已死，即使进程还在也视为不可用——继续写入只会让前端
    看不到任何输出，体感上就是“终端没了”。"""
    if not session:
        return False
    if session.get("master_fd") is not None:
        return True
    proc = session.get("proc")
    if proc is None:
        return False
    if hasattr(proc, "write"):
        # pywinpty 专属：reader 线程必须健在。
        if session.get("reader_alive") is False:
            return False
        return _local_proc_alive(proc)
    if getattr(proc, "stdin", None) and proc.returncode is None:
        return True
    return False


def _local_session_alive(session: dict | None) -> bool:
    """判断本机终端 session 是否仍可供 AI 读写，顺手剔除半死 PTY 的依据。"""
    if not session:
        return False
    return _local_terminal_io_ready(session)


def _local_terminal_backend_name(use_pty: bool) -> str:
    if sys.platform != "win32":
        return "pty"
    return "winpty" if use_pty else "pipe"


def _local_session_pending(session: dict | None) -> bool:
    """WebSocket 已建立但 shell 子进程尚未挂载到 session 的短暂状态。"""
    return bool(session and session.get("proc") is None and session.get("master_fd") is None and session.get("ws") is not None)


def _drop_local_session_if_stale(
    user_id: int, slot: int, session: dict | None = None, scope_id: str | None = None
) -> bool:
    """若本机终端 session 已失效则清理，返回是否已清理。"""
    key = _local_session_key(user_id, slot, scope_id)
    session = session if session is not None else _local_sessions.get(key)
    if _local_session_alive(session):
        return False
    if _local_session_pending(session):
        return False
    if session is not None and _local_sessions.get(key) is session:
        _local_sessions.pop(key, None)
    if session:
        _terminate_local_proc(session.get("proc"))
    return True


def next_local_terminal_slot(user_id: int, scope_id: str | None = None) -> int:
    """为 AI/前端创建本机控制台预分配一个当前 scope 内未占用的 slot。"""
    scope_norm = normalize_terminal_scope_id(scope_id)
    used: set[int] = set()
    for (uid, session_scope, slot), session in list(_local_sessions.items()):
        if uid != user_id or session_scope != scope_norm:
            continue
        if _drop_local_session_if_stale(uid, slot, session, session_scope):
            continue
        used.add(slot)
    for slot in range(32):
        if slot not in used:
            return slot
    return 31


def default_local_terminal_slot(user_id: int, scope_id: str | None = None) -> int:
    """默认写入最近使用/最近创建的 AI 本机控制台，避免误写隐藏的旧 slot。"""
    scope_norm = normalize_terminal_scope_id(scope_id)
    candidates: list[tuple[float, int]] = []
    fallback: list[tuple[float, int]] = []
    for (uid, session_scope, slot), session in list(_local_sessions.items()):
        if uid != user_id or session_scope != scope_norm:
            continue
        if _drop_local_session_if_stale(uid, slot, session, session_scope) or not _local_session_alive(session):
            continue
        ts = float(session.get("last_used_at") or session.get("connected_at") or 0.0)
        item = (ts, slot)
        if (session.get("created_by") or "").strip().lower() == "ai":
            candidates.append(item)
        fallback.append(item)
    if candidates:
        return max(candidates)[1]
    if fallback:
        return max(fallback)[1]
    return 0


async def wait_for_local_terminal_ready(
    user_id: int,
    slot: int,
    scope_id: str | None = None,
    *,
    max_wait_sec: float = LOCAL_TERMINAL_CONNECT_WAIT_MAX_SEC,
    poll_interval_sec: float = LOCAL_TERMINAL_CONNECT_POLL_SEC,
) -> bool:
    """前端刚打开本机控制台时，shell 子进程可能尚未挂到 session，轮询避免首包写入失败。"""
    slot = max(0, min(int(slot), 31))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.5, min(max_wait_sec, 30.0))
    key = _local_session_key(user_id, slot, scope_id)
    while loop.time() < deadline:
        session = _local_sessions.get(key)
        if session and _local_terminal_io_ready(session):
            return True
        await asyncio.sleep(poll_interval_sec)
    return False


async def send_to_local_terminal(
    user_id: int, slot: int, text: str, scope_id: str | None = None
) -> bool:
    """向本机管理中的终端会话写入文本（供 AI 通过 execute_tool 按 scope 调用）。支持 <Ctrl+C> 等控制键占位符。"""
    slot = max(0, min(int(slot), 31))
    key = _local_session_key(user_id, slot, scope_id)
    session = _local_sessions.get(key)
    if not session or _drop_local_session_if_stale(user_id, slot, session, scope_id):
        return False
    session["last_used_at"] = time.time()
    text = expand_control_keys((text or "").rstrip())
    # 仅当非纯控制键时补换行，否则 Ctrl+C 等会多出一个回车
    if not is_control_only(text):
        if sys.platform == "win32":
            if not text.endswith("\r\n"):
                text = text + "\r\n" if text else "\r\n"
        else:
            if not text.endswith("\n"):
                text = text + "\n" if text else "\n"
    master_fd = session.get("master_fd")
    proc = session.get("proc")
    if master_fd is not None:
        try:
            os.write(master_fd, text.encode("utf-8", errors="replace"))
            return True
        except (OSError, BrokenPipeError):
            return False
    if proc is not None:
        try:
            if hasattr(proc, "write"):
                if not _local_proc_alive(proc):
                    _drop_local_session_if_stale(user_id, slot, session, scope_id)
                    return False
                if session.get("reader_alive") is False:
                    _drop_local_session_if_stale(user_id, slot, session, scope_id)
                    return False
                # ConPTY/pywinpty：必须整行带 \r\n 一次性写入，否则命令不会执行（见 pywinpty#545）
                # 走本机终端专用线程池，避免与 AI 文件 I/O 抢默认线程池。
                await _run_in_local_term_thread(proc.write, text)
                return True
            if getattr(proc, "stdin", None) and proc.returncode is None:
                raw = text
                if sys.platform == "win32":
                    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
                proc.stdin.write(raw.encode("utf-8", errors="replace"))
                await proc.stdin.drain()
                return True
        except (BrokenPipeError, ConnectionResetError, EOFError, OSError):
            _drop_local_session_if_stale(user_id, slot, session, scope_id)
            return False
    return False


@router.websocket("/ws")
async def local_terminal_ws(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token") or ws.query_params.get("Authorization", "").replace("Bearer ", "")
    user = await user_dict_for_websocket_from_token(token)
    if not user or not _is_admin_role(user.get("role")):
        await ws.send_json({"type": "error", "message": "需要管理员权限"})
        await ws.close()
        return
    user_id = user["id"]
    slot = 0
    scope_id = normalize_terminal_scope_id(None)
    session_obj = None
    session_key: tuple[int, str, int] | None = None
    try:
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send_json({"type": "error", "message": "首帧须为 JSON: { type: init }"})
            return
        if msg.get("type") != "init":
            await ws.send_json({"type": "error", "message": "需要 type: init"})
            return
        if isinstance(msg.get("slot"), (int, float)):
            slot = max(0, min(int(msg["slot"]), 31))
        scope_id = normalize_terminal_scope_id(msg.get("scope_id"))
        created_by = (msg.get("created_by") or "user").strip().lower()
        if created_by not in ("user", "ai"):
            created_by = "user"
        session_key = _local_session_key(user_id, slot, scope_id)
        old = _local_sessions.pop(session_key, None)
        if old and _local_proc_alive(old.get("proc")):
            logger.warning(
                "local ws replacing existing session: user_id=%s scope=%s slot=%s",
                user_id, scope_id, slot,
            )
            _terminate_local_proc(old.get("proc"))
        buffer: list[str] = []
        buffer_size = [0]
        cwd = str(LOCAL_ROOT)
        use_pty = (sys.platform != "win32") or _should_use_winpty()
        backend = _local_terminal_backend_name(use_pty)
        logger.warning(
            "local ws starting session: user_id=%s scope=%s slot=%s platform=%s backend=%s",
            user_id, scope_id, slot, sys.platform, backend,
        )
        await ws.send_json({
            "type": "ready", "slot": slot, "scope_id": scope_id,
            "cwd": cwd, "platform": sys.platform, "pty": use_pty, "backend": backend,
        })
        now_ts = time.time()
        session_obj = {
            "buffer": buffer,
            "buffer_size": buffer_size,
            "ws": ws,
            "created_by": created_by,
            "connected_at": now_ts,
            "last_used_at": now_ts,
            "platform": sys.platform,
            "pty": use_pty,
            "backend": backend,
            "last_output_at": None,
            # Windows pywinpty 路径会在 _run_local_shell_pty_win 里设置；其它平台保持为 None。
            "reader_alive": None,
            "reader_exit_reason": None,
            "reader_exit_at": None,
        }
        _local_sessions[session_key] = session_obj
        await _run_local_shell_pty(ws, buffer, buffer_size, cwd, session_key)
    except WebSocketDisconnect:
        logger.warning("local ws disconnected: user_id=%s slot=%s", user_id, slot)
        pass
    except Exception as e:
        logger.exception("local ws: %s", e)
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if session_key is not None and session_obj is not None and _local_sessions.get(session_key) is session_obj:
            _local_sessions.pop(session_key, None)
        logger.warning("local ws session removed: user_id=%s scope=%s slot=%s", user_id, scope_id, slot)


def get_local_terminals_for_user(user_id: int, scope_id: str | None = None) -> list[dict]:
    """供 AI list_terminals（scope=local）调用：返回该用户当前 scope 内本机控制台 slot 列表。"""
    scope_norm = normalize_terminal_scope_id(scope_id)
    out = []
    for (uid, session_scope, slot), session in list(_local_sessions.items()):
        if uid != user_id or session_scope != scope_norm:
            continue
        if _drop_local_session_if_stale(uid, slot, session, session_scope) or not _local_session_alive(session):
            continue
        out.append({
            "slot": slot,
            "scope_id": session_scope,
            "connected": True,
            "created_by": session.get("created_by") or "user",
            "pty": bool(session.get("pty")),
            "backend": session.get("backend") or _local_terminal_backend_name(bool(session.get("pty"))),
            "platform": session.get("platform") or sys.platform,
            "connected_at": session.get("connected_at"),
            "last_used_at": session.get("last_used_at"),
            "last_output_at": session.get("last_output_at"),
            "buffer_chars": sum(len(x) for x in (session.get("buffer") or [])),
            "reader_alive": session.get("reader_alive"),
            "reader_exit_reason": session.get("reader_exit_reason"),
        })
    out.sort(key=lambda x: x["slot"])
    return out


def resolve_local_slot(
    user_id: int,
    scope_id: str | None = None,
    requested_slot: int | None = None,
    default_terminal_slot: int | None = None,
) -> tuple[int | None, str | None]:
    """解析本机管理控制台 slot（与 execute_tool 内逻辑一致）。"""
    items = get_local_terminals_for_user(user_id, scope_id)
    slots = {int(it["slot"]): it for it in items if it.get("slot") is not None}
    if not slots:
        return None, "当前页面 scope 内没有本机控制台，请先 create_local_console 或在本机管理页打开控制台"
    if requested_slot is not None:
        try:
            requested_slot = int(requested_slot)
        except (TypeError, ValueError):
            return None, "slot 须为整数"
        if requested_slot not in slots:
            labels = ", ".join(f"slot={s}" for s in sorted(slots.keys()))
            return None, f"slot {requested_slot} 不存在于当前页面。可用：{labels}"
        return requested_slot, None
    if default_terminal_slot is not None and default_terminal_slot in slots:
        return default_terminal_slot, None
    ai_items = [s for s, it in slots.items() if (it.get("created_by") or "") == "ai"]
    if ai_items:
        return min(ai_items), None
    return min(slots.keys()), None


def get_local_terminal_buffer(
    user_id: int, slot: int, scope_id: str | None = None
) -> tuple[str, bool]:
    """获取本机管理某控制台的输出缓冲（供 AI execute_tool 按 scope 调用）。返回 (buffer_text, connected)。"""
    slot = max(0, min(slot, 31))
    key = _local_session_key(user_id, slot, scope_id)
    session = _local_sessions.get(key)
    if not session:
        return "", False
    buf = "".join(session.get("buffer") or [])
    if _drop_local_session_if_stale(user_id, slot, session, scope_id) or not _local_session_alive(session):
        return buf, False
    return buf, True


@router.get("/buffer")
async def local_buffer(slot: int = 0, scope_id: str | None = None, user=Depends(require_admin)):
    """获取本机某控制台的输出缓冲（供前端/Log 展示）。"""
    slot = max(0, min(slot, 31))
    scope_id = normalize_terminal_scope_id(scope_id)
    key = _local_session_key(user["id"], slot, scope_id)
    session = _local_sessions.get(key)
    if not session:
        return {"success": True, "buffer": "", "connected": False, "slot": slot, "scope_id": scope_id}
    buf = "".join(session.get("buffer") or [])
    connected = not _drop_local_session_if_stale(user["id"], slot, session, scope_id) and _local_session_alive(session)
    return {
        "success": True,
        "buffer": buf,
        "connected": connected,
        "slot": slot,
        "scope_id": scope_id,
        "backend": session.get("backend") or _local_terminal_backend_name(bool(session.get("pty"))),
        "last_output_at": session.get("last_output_at"),
        "reader_alive": session.get("reader_alive"),
        "reader_exit_reason": session.get("reader_exit_reason"),
    }


def _local_term_executor_stats() -> dict | None:
    if sys.platform != "win32":
        return None
    ex = _LOCAL_TERM_EXECUTOR
    if ex is None:
        return {"created": False}
    return {
        "created": True,
        "max_workers": getattr(ex, "_max_workers", None),
        "pending_work": getattr(getattr(ex, "_work_queue", None), "qsize", lambda: None)(),
        "threads": len(getattr(ex, "_threads", []) or []),
    }


@router.get("/terminals")
async def local_terminals(scope_id: str | None = None, user=Depends(require_admin)):
    """诊断：列出当前用户本机终端后端会话。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    return {
        "success": True,
        "terminals": get_local_terminals_for_user(user["id"], scope_id),
        "scope_id": scope_id,
        "windows_backend": _windows_terminal_backend() if sys.platform == "win32" else None,
        "winpty_active": _should_use_winpty() if sys.platform == "win32" else None,
        "winpty_failures_in_window": len(_winpty_failure_times) if sys.platform == "win32" else 0,
        "local_term_executor": _local_term_executor_stats(),
    }


@router.get("/ping")
async def local_ping():
    """无数据库依赖的本机管理探针：用于判断事件循环/HTTP 是否被 AI 执行卡住。"""
    return {
        "success": True,
        "time": time.time(),
        "platform": sys.platform,
        "sessions": len(_local_sessions),
        "managed_processes": len(_managed_processes),
        "windows_backend": _windows_terminal_backend() if sys.platform == "win32" else None,
        "winpty_active": _should_use_winpty() if sys.platform == "win32" else None,
        "winpty_failures_in_window": len(_winpty_failure_times) if sys.platform == "win32" else 0,
        "local_term_executor": _local_term_executor_stats(),
    }
