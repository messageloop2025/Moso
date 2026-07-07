"""文件系统 API：web/fs 目录下列表、读写、上传下载、tgz 打包解压、复制/移动。路径均相对 fs 根，禁止 .. 逃逸。"""
import asyncio
import os
import re
import shutil
import stat
import tarfile
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import config
from api.auth import (
    assert_user_active,
    authenticate_bearer_credentials,
    get_current_user,
    _is_admin_role,
)

logger = logging.getLogger("edgeops.filesystem")
router = APIRouter(prefix="/api/fs", tags=["文件系统"])

FS_DIR = Path(config.FS_DIR)
FS_DIR.mkdir(parents=True, exist_ok=True)

# 兼容：无 base 时使用的全局根（仅 AI skill 等未传 user 时）
FS_ROOT = FS_DIR


def _safe_username(name: str) -> str:
    """只保留安全字符，避免路径逃逸。"""
    if not name or not isinstance(name, str):
        return "default"
    s = "".join(c for c in name.strip() if c.isalnum() or c in "._-")[:64]
    return s or "default"


def get_user_fs_root(user: dict, username_override: Optional[str] = None) -> Path:
    """获取当前用户（或管理员指定用户）的文件系统根目录：web/fs/[username]。"""
    if _is_admin_role(user.get("role")) and username_override and username_override.strip():
        effective = _safe_username(username_override.strip())
    else:
        effective = _safe_username(user.get("username") or "default")
    root = FS_DIR / effective
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_fs_relative(path: str) -> str:
    """将 web 虚拟地址或带前缀的路径规范为相对 fs 根的路径（无前导 / 或 fs/）。"""
    path = (path or "").strip().replace("\\", "/").lstrip("/")
    for prefix in ("fs/", "web/fs/", "static/fs/"):
        if path.lower().startswith(prefix):
            path = path[len(prefix):].lstrip("/")
            break
    return path


_FS_WEB_MARKERS = ("web/fs/", "static/fs/")


def _fs_workspace_username(base: Path) -> str:
    """base 为 web/fs/<username> 时返回 username，否则空（不做用户名前缀剥离）。"""
    try:
        base_resolved = base.resolve()
        fs_root = FS_DIR.resolve()
        if base_resolved.parent == fs_root:
            return base_resolved.name
    except OSError:
        pass
    return ""


def _strip_embedded_fs_web_path(path: str, username: str = "") -> str:
    """从混合路径中提取 web/fs 之后的相对段（兼容 AI 误传绝对路径）。"""
    s = (path or "").strip().replace("\\", "/")
    if not s:
        return ""
    lower = s.lower()
    for marker in _FS_WEB_MARKERS:
        idx = lower.find(marker)
        if idx >= 0:
            tail = s[idx + len(marker):].lstrip("/")
            if username and tail.lower().startswith(username.lower() + "/"):
                tail = tail[len(username):].lstrip("/")
            elif username and tail.lower() == username.lower():
                tail = ""
            return tail
    if lower.startswith("fs/"):
        tail = s[3:].lstrip("/")
        if username and tail.lower().startswith(username.lower() + "/"):
            tail = tail[len(username):].lstrip("/")
        return tail
    return s


def _looks_like_os_absolute_path(path: str) -> bool:
    s = (path or "").strip().replace("\\", "/")
    if not s:
        return False
    if len(s) >= 2 and s[1] == ":":
        return True
    if s.startswith("/"):
        return True
    first = s.split("/")[0].lower()
    return first in ("home", "opt", "var", "usr", "root", "etc", "private", "users", "windows", "program files")


def fs_path_usage_hint() -> str:
    return (
        "路径须为相对当前用户「文件系统」工作区根（如 chats/…/file.txt、scripts/a.sh），"
        "不要使用操作系统绝对路径，也不要写 web/fs/ 或用户名前缀。"
    )


def coerce_fs_relative_path(raw: str, base: Optional[Path] = None) -> str:
    """规范为相对用户 fs 根的路径；若误传 OS 绝对路径则尽量归位，无法归位则报错。"""
    s = (raw or "").strip().replace("\\", "/")
    if not s or s in (".", "/"):
        return ""

    base = base or FS_DIR
    username = _fs_workspace_username(base)

    is_win_abs = len(s) >= 2 and s[1] == ":"
    if Path(s).is_absolute() or is_win_abs:
        try:
            resolved = Path(s).resolve()
            rel = resolved.relative_to(base.resolve())
            return str(rel).replace("\\", "/")
        except (ValueError, OSError):
            s = _strip_embedded_fs_web_path(s, username)
            s = _normalize_fs_relative(s.lstrip("/"))
            if username and s.lower().startswith(username.lower() + "/"):
                s = s[len(username):].lstrip("/")
            elif username and s.lower() == username.lower():
                s = ""
            if _looks_like_os_absolute_path(s):
                raise ValueError(f"{fs_path_usage_hint()} 收到：{str(raw)[:160]}")
            return s

    s = s.lstrip("/")
    s = _strip_embedded_fs_web_path(s, username)
    s = _normalize_fs_relative(s)
    if username and s.lower().startswith(username.lower() + "/"):
        s = s[len(username):].lstrip("/")
    elif username and s.lower() == username.lower():
        s = ""
    if _looks_like_os_absolute_path(s):
        raise ValueError(f"{fs_path_usage_hint()} 收到：{str(raw)[:160]}")
    return s


def _safe_upload_relative_name(name: str) -> str:
    """multipart 中的文件名可能含子目录（拖放文件夹）；禁止 .. 与绝对路径逃逸。"""
    raw = (name or "").strip().replace("\\", "/").lstrip("/")
    parts = [p for p in raw.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError("非法上传路径")
    return "/".join(parts) if parts else "upload"


def resolve_fs_path(relative: str, base: Optional[Path] = None) -> Path:
    """将相对路径解析为绝对路径，且必须落在 base 内；base 默认为 FS_DIR（兼容旧调用）。"""
    base = base or FS_DIR
    relative = coerce_fs_relative_path(relative, base)
    if not relative:
        return base
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError("路径不允许访问")
    return resolved


def _resolve_path(relative: str, base: Path) -> Path:
    try:
        return resolve_fs_path(relative, base)
    except ValueError:
        raise HTTPException(status_code=400, detail="路径不允许访问")


# ── 同步辅助（供 AI skill 与 API 共用；base 为对应用户的 fs 根）──
def fs_list_dir(relative: str, base: Optional[Path] = None) -> dict:
    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if not root.is_dir():
        raise ValueError("不是目录")
    items = []
    for p in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            stat = p.stat()
            items.append({
                "name": p.name,
                "path": str(p.relative_to(base)).replace("\\", "/"),
                "dir": p.is_dir(),
                "size": stat.st_size if p.is_file() else 0,
                "mtime": stat.st_mtime,
            })
        except OSError:
            continue
    return {"success": True, "path": relative or "/", "items": items}


async def fs_list_dir_async(relative: str, base: Optional[Path] = None) -> dict:
    return await asyncio.to_thread(fs_list_dir, relative, base)


def _normalize_extension_set(extensions) -> set[str]:
    out: set[str] = set()
    if not extensions:
        return out
    items = extensions if isinstance(extensions, (list, tuple, set)) else str(extensions).split(",")
    for raw in items:
        s = str(raw or "").strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        out.add(s)
    return out


def _compile_fs_search_regex(pattern: str, *, label: str) -> re.Pattern[str] | None:
    raw = (pattern or "").strip()
    if not raw:
        return None
    if len(raw) > int(getattr(config, "FS_SEARCH_MAX_REGEX_LEN", 500)):
        raise ValueError(f"{label} 正则过长")
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{label} 正则无效: {exc}") from exc


def _parse_optional_mtime(value, *, label: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return float(s)
    except ValueError:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError as exc:
        raise ValueError(f"{label} 时间格式无效（支持 Unix 秒或 ISO8601）: {value}") from exc


def fs_search_files(
    relative_root: str = "",
    base: Optional[Path] = None,
    *,
    name_regex: str = "",
    path_regex: str = "",
    extensions=None,
    min_bytes: int | None = None,
    max_bytes: int | None = None,
    min_mtime: float | None = None,
    max_mtime: float | None = None,
    modified_after: str = "",
    modified_before: str = "",
    recursive: bool = True,
    files_only: bool = True,
    limit: int | None = None,
) -> dict:
    """在用户文件系统工作区内搜索。各筛选条件均为可选，可单独或任意组合（多条件 AND）；未传条件时列出根目录下文件。"""
    base = base or FS_DIR
    rel_root = coerce_fs_relative_path(relative_root or "", base)
    root = resolve_fs_path(rel_root, base) if rel_root else base.resolve()
    if not root.exists():
        raise ValueError("搜索根路径不存在")

    name_re = _compile_fs_search_regex(name_regex, label="name_regex")
    path_re = _compile_fs_search_regex(path_regex, label="path_regex")
    ext_set = _normalize_extension_set(extensions)

    eff_min_mtime = _parse_optional_mtime(min_mtime if min_mtime is not None else modified_after, label="min_mtime")
    eff_max_mtime = _parse_optional_mtime(max_mtime if max_mtime is not None else modified_before, label="max_mtime")

    try:
        min_b = int(min_bytes) if min_bytes is not None else None
    except (TypeError, ValueError):
        min_b = None
    try:
        max_b = int(max_bytes) if max_bytes is not None else None
    except (TypeError, ValueError):
        max_b = None
    if min_b is not None and min_b < 0:
        min_b = 0
    if max_b is not None and max_b < 0:
        max_b = 0

    max_results = int(getattr(config, "FS_SEARCH_MAX_RESULTS", 500))
    try:
        req_limit = int(limit) if limit is not None else max_results
    except (TypeError, ValueError):
        req_limit = max_results
    req_limit = max(1, min(req_limit, max_results))

    max_scan = int(getattr(config, "FS_SEARCH_MAX_SCAN", 50000))
    filters_applied: dict = {}
    if name_re:
        filters_applied["name_regex"] = name_regex.strip()
    if path_re:
        filters_applied["path_regex"] = path_regex.strip()
    if ext_set:
        filters_applied["extensions"] = sorted(ext_set)
    if min_b is not None:
        filters_applied["min_bytes"] = min_b
    if max_b is not None:
        filters_applied["max_bytes"] = max_b
    if eff_min_mtime is not None:
        filters_applied["min_mtime"] = eff_min_mtime
    if eff_max_mtime is not None:
        filters_applied["max_mtime"] = eff_max_mtime

    hits: list[dict] = []
    scanned = 0
    truncated_scan = False

    def _iter_candidates():
        if root.is_file():
            yield root
            return
        if recursive:
            yield from root.rglob("*")
        else:
            yield from root.iterdir()

    for candidate in _iter_candidates():
        scanned += 1
        if scanned > max_scan:
            truncated_scan = True
            break
        if files_only and not candidate.is_file():
            continue
        try:
            rel = str(candidate.relative_to(base)).replace("\\", "/")
            st = candidate.stat()
        except OSError:
            continue
        if name_re and not name_re.search(candidate.name):
            continue
        if path_re and not path_re.search(rel):
            continue
        if ext_set:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in ext_set:
                continue
        if min_b is not None and st.st_size < min_b:
            continue
        if max_b is not None and st.st_size > max_b:
            continue
        if eff_min_mtime is not None and st.st_mtime < eff_min_mtime:
            continue
        if eff_max_mtime is not None and st.st_mtime > eff_max_mtime:
            continue
        hits.append(
            {
                "id": len(hits) + 1,
                "name": candidate.name,
                "path": rel,
                "dir": candidate.is_dir(),
                "size": st.st_size if candidate.is_file() else 0,
                "mtime": st.st_mtime,
            }
        )
        if len(hits) >= req_limit:
            break

    return {
        "success": True,
        "root": rel_root or "/",
        "count": len(hits),
        "items": hits,
        "scanned": scanned,
        "truncated_results": len(hits) >= req_limit,
        "truncated_scan": truncated_scan,
        "limit": req_limit,
        "filters_applied": filters_applied,
        "filter_logic": "and",
        "usage": (
            "items[].id 为本次搜索内序号（从 1 递增，仅当次结果有效）。"
            "向用户展示时请保留 id；用户指「2 号」「id=3」时，用对应项的 path 执行 fs_read_file、"
            "fs_delete、fs_copy、http_upload 等后续操作。"
        ),
    }


async def fs_search_files_async(
    relative_root: str = "",
    base: Optional[Path] = None,
    **kwargs,
) -> dict:
    return await asyncio.to_thread(fs_search_files, relative_root, base, **kwargs)


def fs_read_file(
    relative: str,
    base: Optional[Path] = None,
    *,
    offset: int = 0,
    size: Optional[int] = None,
) -> dict:
    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if not root.is_file():
        raise ValueError("文件不存在或不是文件")
    raw = root.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("文本过大或非文本文件")
    if b"\x00" in raw[:65536]:
        raise ValueError("文本过大或非文本文件")
    try:
        text = raw.decode("utf-8", errors="strict")
        try:
            off = max(0, int(offset or 0))
        except (TypeError, ValueError):
            off = 0
        if size is None:
            sliced = text[off:]
        else:
            try:
                length = max(0, int(size))
            except (TypeError, ValueError):
                length = 0
            sliced = text[off : off + length]
        return {
            "success": True,
            "path": relative,
            "content": sliced,
            "offset": off,
            "size": len(sliced),
            "total_chars": len(text),
            "truncated": (off + len(sliced)) < len(text),
        }
    except UnicodeDecodeError:
        raise ValueError("文本过大或非文本文件")


async def fs_read_file_async(
    relative: str,
    base: Optional[Path] = None,
    *,
    offset: int = 0,
    size: Optional[int] = None,
) -> dict:
    return await asyncio.to_thread(fs_read_file, relative, base, offset=offset, size=size)


def fs_write_file(
    relative: str,
    content: str,
    base: Optional[Path] = None,
    *,
    mode: str = "overwrite",
    offset: Optional[int] = None,
    replace_length: int = 0,
) -> dict:
    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if root == base:
        raise ValueError("不能覆盖根路径")
    root.parent.mkdir(parents=True, exist_ok=True)
    text = content or ""
    mode_val = (mode or "overwrite").strip().lower()
    if mode_val not in ("overwrite", "append", "insert", "replace"):
        mode_val = "overwrite"
    if mode_val == "overwrite" and offset is None:
        root.write_text(text, encoding="utf-8")
        return {"success": True, "path": relative, "mode": mode_val}
    old = ""
    if root.exists():
        raw = root.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("文本过大，不支持定位写")
        if b"\x00" in raw[:65536]:
            raise ValueError("目标文件不是文本文件")
        try:
            old = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError("目标文件不是 UTF-8 文本文件")
    if mode_val == "append" and offset is None:
        merged = old + text
    else:
        try:
            pos = int(offset if offset is not None else len(old))
        except (TypeError, ValueError):
            pos = len(old)
        pos = max(0, min(pos, len(old)))
        try:
            rl = max(0, int(replace_length or 0))
        except (TypeError, ValueError):
            rl = 0
        if mode_val == "insert":
            rl = 0
        elif mode_val == "replace" and rl == 0:
            rl = len(text)
        merged = old[:pos] + text + old[pos + rl :]
    root.write_text(merged, encoding="utf-8")
    return {"success": True, "path": relative, "mode": mode_val}


async def fs_write_file_async(
    relative: str,
    content: str,
    base: Optional[Path] = None,
    *,
    mode: str = "overwrite",
    offset: Optional[int] = None,
    replace_length: int = 0,
) -> dict:
    """异步包装写文件，避免 AI 写大文件时阻塞事件循环与本机终端 WebSocket。"""
    return await asyncio.to_thread(
        fs_write_file,
        relative,
        content,
        base,
        mode=mode,
        offset=offset,
        replace_length=replace_length,
    )


def fs_read_binary(
    relative: str,
    base: Optional[Path] = None,
    *,
    offset: int = 0,
    size: Optional[int] = None,
    encoding: str = "base64",
) -> dict:
    import base64

    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if not root.is_file():
        raise ValueError("文件不存在或不是文件")
    raw = root.read_bytes()
    total = len(raw)
    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0
    if off > total:
        off = total
    if size is None:
        chunk = raw[off:]
    else:
        try:
            n = max(0, int(size))
        except (TypeError, ValueError):
            n = 0
        chunk = raw[off : off + n]
    enc = (encoding or "base64").strip().lower()
    if enc not in ("base64", "hex"):
        raise ValueError("encoding 仅支持 base64 或 hex")
    if enc == "hex":
        content_encoded = chunk.hex()
    else:
        content_encoded = base64.b64encode(chunk).decode("ascii")
    return {
        "success": True,
        "path": relative,
        "content": content_encoded,
        "encoding": enc,
        "offset": off,
        "size": len(chunk),
        "total_bytes": total,
        "truncated": (off + len(chunk)) < total,
    }


async def fs_read_binary_async(
    relative: str,
    base: Optional[Path] = None,
    *,
    offset: int = 0,
    size: Optional[int] = None,
    encoding: str = "base64",
) -> dict:
    return await asyncio.to_thread(fs_read_binary, relative, base, offset=offset, size=size, encoding=encoding)


def fs_write_binary(
    relative: str,
    content_encoded: str,
    base: Optional[Path] = None,
    *,
    offset: Optional[int] = None,
    truncate: bool = False,
    encoding: str = "base64",
) -> dict:
    import base64

    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if root == base:
        raise ValueError("不能覆盖根路径")
    root.parent.mkdir(parents=True, exist_ok=True)
    enc = (encoding or "base64").strip().lower()
    if enc not in ("base64", "hex"):
        raise ValueError("encoding 仅支持 base64 或 hex")
    try:
        if enc == "hex":
            payload = bytes.fromhex((content_encoded or "").strip())
        else:
            payload = base64.b64decode((content_encoded or "").encode("ascii"), validate=True)
    except Exception:
        raise ValueError("content 与 encoding 不匹配")
    if offset is None:
        mode = "wb" if truncate else "ab"
        with open(root, mode) as f:
            f.write(payload)
    else:
        try:
            off = max(0, int(offset))
        except (TypeError, ValueError):
            off = 0
        with open(root, "r+b" if root.exists() else "wb") as f:
            f.seek(off)
            f.write(payload)
    return {"success": True, "path": relative, "bytes_written": len(payload), "encoding": enc}


async def fs_write_binary_async(
    relative: str,
    content_encoded: str,
    base: Optional[Path] = None,
    *,
    offset: Optional[int] = None,
    truncate: bool = False,
    encoding: str = "base64",
) -> dict:
    return await asyncio.to_thread(
        fs_write_binary,
        relative,
        content_encoded,
        base,
        offset=offset,
        truncate=truncate,
        encoding=encoding,
    )


def fs_truncate(relative: str, size: int = 0, base: Optional[Path] = None) -> dict:
    """把 web/fs 文件截断或扩展到指定字节大小。"""
    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if root == base:
        raise ValueError("不能操作根路径")
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        target = max(0, int(size or 0))
    except (TypeError, ValueError):
        target = 0
    with open(root, "a+b") as f:
        f.truncate(target)
    return {"success": True, "path": relative, "size": target}


async def fs_truncate_async(relative: str, size: int = 0, base: Optional[Path] = None) -> dict:
    return await asyncio.to_thread(fs_truncate, relative, size, base)


def fs_mkdir(relative: str, base: Optional[Path] = None) -> dict:
    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if root == base:
        raise ValueError("路径无效")
    root.mkdir(parents=True, exist_ok=True)
    return {"success": True, "path": relative}


async def fs_mkdir_async(relative: str, base: Optional[Path] = None) -> dict:
    return await asyncio.to_thread(fs_mkdir, relative, base)


def fs_pack_tgz(relative: str, base: Optional[Path] = None) -> dict:
    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if not root.is_dir():
        raise ValueError("只能对目录打包")
    out_path = root.with_suffix(root.suffix + ".tgz")
    with tarfile.open(str(out_path), "w:gz") as tf:
        for item in root.rglob("*"):
            if item.is_file():
                arcname = str(item.relative_to(root.parent))
                tf.add(str(item), arcname=arcname)
    rel = str(out_path.relative_to(base)).replace("\\", "/")
    return {"success": True, "path": rel}


async def fs_pack_tgz_async(relative: str, base: Optional[Path] = None) -> dict:
    return await asyncio.to_thread(fs_pack_tgz, relative, base)


def fs_unpack_tgz(path_tgz: str, dest_relative: str = "", base: Optional[Path] = None) -> dict:
    base = base or FS_DIR
    root = resolve_fs_path(path_tgz, base)
    if not root.is_file() or not root.name.endswith((".tgz", ".tar.gz")):
        raise ValueError("需要 .tgz 文件路径")
    dest_root = resolve_fs_path(dest_relative, base) if dest_relative.strip() else root.parent
    if not dest_root.is_dir():
        dest_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(root), "r:gz") as tf:
        tf.extractall(str(dest_root))
    dest_rel = str(dest_root.relative_to(base)).replace("\\", "/")
    return {"success": True, "path": path_tgz, "dest": dest_rel}


async def fs_unpack_tgz_async(path_tgz: str, dest_relative: str = "", base: Optional[Path] = None) -> dict:
    return await asyncio.to_thread(fs_unpack_tgz, path_tgz, dest_relative, base)


def _user_base(user, username: Optional[str] = None) -> Path:
    """当前请求的用户 fs 根（普通用户=自己；管理员可传 username 查看他人）。"""
    return get_user_fs_root(user, username)


@router.get("/list")
async def list_dir(path: str = "", username: Optional[str] = None, user=Depends(get_current_user)):
    """列表：普通用户仅 web/fs/自己的用户名；管理员可传 username= 查看某用户目录。"""
    base = _user_base(user, username)
    try:
        return await fs_list_dir_async(path, base)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/search")
async def search_files(
    path: str = "",
    name_regex: str = "",
    path_regex: str = "",
    extensions: str = "",
    min_bytes: Optional[int] = None,
    max_bytes: Optional[int] = None,
    min_mtime: Optional[float] = None,
    max_mtime: Optional[float] = None,
    modified_after: str = "",
    modified_before: str = "",
    recursive: bool = True,
    files_only: bool = True,
    limit: Optional[int] = None,
    username: Optional[str] = None,
    user=Depends(get_current_user),
):
    """在用户文件系统内搜索：文件名/路径正则、后缀、大小、修改时间。"""
    base = _user_base(user, username)
    ext_list = [s.strip() for s in extensions.split(",") if s.strip()] if extensions else None
    try:
        return await fs_search_files_async(
            path,
            base,
            name_regex=name_regex,
            path_regex=path_regex,
            extensions=ext_list,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            min_mtime=min_mtime,
            max_mtime=max_mtime,
            modified_after=modified_after,
            modified_before=modified_before,
            recursive=recursive,
            files_only=files_only,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/read")
async def read_file(path: str, username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    try:
        return await fs_read_file_async(path, base)
    except ValueError as e:
        raise HTTPException(status_code=404 if "不存在" in str(e) else 400, detail=str(e))


class WriteBody(BaseModel):
    path: str
    content: str = ""
    mode: str = "overwrite"
    offset: Optional[int] = None
    replace_length: int = 0


@router.put("/write")
async def write_file(body: WriteBody, username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    try:
        return await fs_write_file_async(
            body.path,
            body.content,
            base,
            mode=body.mode,
            offset=body.offset,
            replace_length=body.replace_length,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/mkdir")
async def mkdir(path: str, username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    try:
        return await fs_mkdir_async(path, base)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload")
async def upload_file(
    path: str = Form(""),
    file: UploadFile = File(...),
    username: Optional[str] = None,
    user=Depends(get_current_user),
):
    """上传到当前用户 fs 根（web/fs/用户名）；管理员可传 username= 指定用户。"""
    base = _user_base(user, username)
    root = _resolve_path(path, base)
    try:
        if root.is_dir():
            safe_rel = _safe_upload_relative_name(file.filename or "upload")
            target = root / safe_rel
        else:
            target = root
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    try:
        content = await file.read()
        await asyncio.to_thread(target.write_bytes, content)
        rel = str(target.relative_to(base)).replace("\\", "/")
        return {"success": True, "path": rel}
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download")
async def download_file(path: str, username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    root = _resolve_path(path, base)
    if not root.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(root), filename=root.name)


# 与 ai_artifacts 那一份实现保持对称：让 `<a href>`、iframe.src 这类直链请求也能用
# query string 形式传 token（无法附加自定义 Authorization header 的场景）。
async def _resolve_user_via_header_or_query(request: Request) -> dict:
    raw = ""
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth_header:
        parts = auth_header.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw = parts[1].strip()
        else:
            raw = auth_header.strip()
    if not raw:
        raw = (request.query_params.get("token") or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="未登录")
    user = await authenticate_bearer_credentials(raw)
    if not user:
        raise HTTPException(status_code=401, detail="Token已过期或无效")
    assert_user_active(user)
    return user


# inline 预览允许的 mime 前缀。HTML 内 `<script src>` 引用 JS、CSS、图片、JSON 数据
# 等都要从这里出去；application/javascript 必须放开，否则 echarts/mermaid 等就会
# 被强制 attachment 下载而不是被 iframe 当 JS 执行。
_FS_INLINE_MIME_ALLOW_PREFIXES = (
    "text/",
    "image/",
    "application/json",
    "application/pdf",
    "application/xml",
    "application/javascript",
    "application/wasm",
    "font/",
    "application/font",
)


@router.get("/file/{path:path}")
async def fs_file_inline(
    path: str,
    request: Request,
    username: Optional[str] = None,
    download: int = 0,
):
    """路径式 inline 访问 web/fs 文件。

    存在的意义：
    1. HTML 预览（文件系统页 / 任意第三方 iframe）需要让浏览器以**真实路径式 URL**
       作为 base 自动解析 `./libs/x.js` 这类相对引用。`/api/fs/read?path=` 形式
       返回 JSON、`/api/fs/download?path=` 强制 attachment 都不能让 iframe 工作。
    2. `?token=` query 鉴权：iframe.src / `<a href>` 这类直链无法自带
       `Authorization: Bearer` header，与 ai_artifacts 的 `/files/{path:path}`
       端点对称。
    3. `download=1` 强制 attachment，给"文件下载"按钮直接 window.open 用。

    权限：普通用户仅自己的 web/fs/<username>；管理员可传 `username=` 查看他人。
    """
    user = await _resolve_user_via_header_or_query(request)
    base = _user_base(user, username)
    root = _resolve_path(path, base)
    if not root.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    import mimetypes as _mt
    mt, _ = _mt.guess_type(root.name)
    mt = mt or "application/octet-stream"
    inline = not bool(download) and any(mt.startswith(p) for p in _FS_INLINE_MIME_ALLOW_PREFIXES)
    if inline:
        return FileResponse(
            str(root),
            media_type=mt,
            filename=root.name,
            content_disposition_type="inline",
        )
    return FileResponse(str(root), media_type=mt, filename=root.name)


@router.post("/pack-tgz")
async def pack_tgz(path: str, username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    try:
        return await fs_pack_tgz_async(path, base)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unpack-tgz")
async def unpack_tgz(path: str, dest: str = "", username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    try:
        return await fs_unpack_tgz_async(path, dest, base)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


def fs_delete(relative: str, base: Optional[Path] = None) -> dict:
    """同步删除 web/fs 下文件或目录（目录含非空也递归删除）。base 为对应用户的 fs 根。供 API 与 AI skill 共用。"""
    base = base or FS_DIR
    root = resolve_fs_path(relative, base)
    if root == base:
        raise ValueError("不能删除根目录")
    if not root.exists():
        raise ValueError("路径不存在")
    if root.is_dir():
        def _rmtree_onerror(func, path, exc_info):
            # Windows 下 .git/objects 等常为只读，导致 [WinError 5] 拒绝访问；去掉只读后重试
            if not os.access(path, os.W_OK):
                os.chmod(path, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
                func(path)
            else:
                raise exc_info[1]
        shutil.rmtree(root, onerror=_rmtree_onerror)
    else:
        root.unlink()
    return {"success": True, "path": relative or "/"}


async def fs_delete_async(relative: str, base: Optional[Path] = None) -> dict:
    return await asyncio.to_thread(fs_delete, relative, base)


@router.delete("/delete")
async def delete_path(path: str, username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    try:
        return await fs_delete_async(path, base)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


class CopyBody(BaseModel):
    path: str
    dest_dir: str
    move: bool = False


def _fs_copy_or_move(src_relative: str, dest_dir_relative: str, move: bool, base: Optional[Path] = None) -> dict:
    """复制或移动 path 到 dest_dir 下；move 为 True 时复制后删除源。"""
    base = base or FS_DIR
    src = resolve_fs_path(src_relative, base)
    dest_dir = resolve_fs_path(dest_dir_relative, base)
    if not src.exists():
        raise ValueError("源不存在")
    if not dest_dir.is_dir():
        raise ValueError("目标必须是目录")
    dest_path = dest_dir / src.name
    if dest_path.resolve() == src.resolve():
        raise ValueError("目标与源相同")
    if str(dest_path.resolve()).startswith(str(src.resolve()) + "/") or str(dest_path.resolve()).startswith(str(src.resolve()) + "\\"):
        raise ValueError("不能移动到自身目录下")
    try:
        if src.is_file():
            shutil.copy2(str(src), str(dest_path))
        else:
            shutil.copytree(str(src), str(dest_path), dirs_exist_ok=True)
        if move:
            if src.is_file():
                src.unlink()
            else:
                shutil.rmtree(str(src))
        dest_rel = str(dest_path.relative_to(base)).replace("\\", "/")
        return {"success": True, "path": src_relative, "dest": dest_rel, "moved": move}
    except OSError as e:
        raise ValueError(str(e))


async def fs_copy_or_move_async(src_relative: str, dest_dir_relative: str, move: bool, base: Optional[Path] = None) -> dict:
    return await asyncio.to_thread(_fs_copy_or_move, src_relative, dest_dir_relative, move, base)


@router.post("/copy")
async def copy_or_move(body: CopyBody, username: Optional[str] = None, user=Depends(get_current_user)):
    base = _user_base(user, username)
    try:
        return await fs_copy_or_move_async(body.path, body.dest_dir, body.move, base)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
