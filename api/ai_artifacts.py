"""AI 成果物（artifacts）API：AI 根据用户指令生成并保存的结构化产物（报告/数据包/可视化）。

存储路径：web/fs/<username>/chats/YYYY/MM/DD/<shortid>/...
- 与聊天附件共用同一个 `chats/` 根目录（`CHAT_ATTACHMENT_SUBDIR`），按日期子目录组织；
  附件是文件（`<uuid>.<ext>`），artifact 是子目录（`<slug>-<shortid>/`），互不冲突。
- 每个 artifact 对应一个独立目录（即使只有单文件也建目录，便于后续追加）。
- 目录里按 AI 的意图组织文件：
    - 单文件类 (csv / md / txt / json) → 直接落盘；kind='single_file'，entry_file=该文件名。
    - 复合类 (html + images/ + js/ + css/ + data.json ...) → kind='bundle'，entry_file 可为 index.html 等。
- 下载：
    - single_file → 直接 FileResponse（保留原文件名）。
    - bundle → 服务端流式打包 tar.gz，文件名 <slug>.tgz。
- 数据库表 ai_artifacts 保存元信息；该文件目录同时对用户在 /api/fs 文件面板里可见。
- AI 通过 create_chat_artifact 等工具使用（见 services/ai_skills.py）。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import tarfile
import uuid as _uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

import config
from api.auth import (
    _is_admin_role,
    assert_user_active,
    authenticate_bearer_credentials,
    get_current_user,
)
from api.filesystem import _safe_username, get_user_fs_root
from database import get_db

logger = logging.getLogger("edgeops.ai_artifacts")

router = APIRouter(prefix="/api/ai/artifacts", tags=["AI 助手·成果物"])


# 所有 artifact 都放在每用户 fs 根目录下的这个子目录（默认与聊天附件共用 "chats/"）
ARTIFACT_SUBDIR = str(getattr(config, "ARTIFACT_SUBDIR", "chats") or "chats").strip().strip("/\\") or "chats"
MAX_FILES = int(getattr(config, "ARTIFACT_MAX_FILES", 200))
MAX_FILE_BYTES = int(getattr(config, "ARTIFACT_MAX_FILE_BYTES", 50 * 1024 * 1024))
MAX_TOTAL_BYTES = int(getattr(config, "ARTIFACT_MAX_TOTAL_BYTES", 200 * 1024 * 1024))

# 允许的扩展名白名单（常见报告与静态站点素材）；二进制图片/字体也允许。
_ALLOWED_EXTS = {
    # 文本 / 数据
    ".md", ".markdown", ".mdx", ".txt", ".log",
    ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".ini", ".toml",
    # 静态站点
    ".html", ".htm", ".css", ".js", ".mjs", ".map", ".svg",
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    # 其它文档
    ".pdf",
}

# 相对路径安全：不允许绝对路径 / ../ / 控制字符 / Windows 保留字。
_UNSAFE_PART_RE = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}

# ──────────────── HTML artifact 自包含资源（web/res/manifest.json）────────────────
# 为了让 AI 生成的 HTML artifact 可以被用户单独下载/离线打开，资源（echarts /
# mermaid / markmap / d3 / html-to-image 等）从 `web/res/<pkg>/` 复制到 artifact
# 目录的 `<libs_subdir>/` 下，HTML 用相对路径引用即可。manifest 同时被注入到
# AI 的系统提示，告诉它有哪些包、引用 snippet 长什么样。
_HTML_LIBS_ROOT = Path(getattr(config, "WEB_DIR", "web")).resolve() / "res"
_HTML_LIBS_MANIFEST_PATH = _HTML_LIBS_ROOT / "manifest.json"
# 默认的复制目标子目录名；调用方可以传 "" 表示扁平复制到 artifact 根目录。
_HTML_LIBS_DEFAULT_SUBDIR = "libs"

_html_libs_manifest_cache: dict | None = None
_html_libs_manifest_mtime: float = 0.0


def load_html_libs_manifest(force_reload: bool = False) -> dict:
    """读取 `web/res/manifest.json` 并按 mtime 简单缓存。

    返回结构形如 `{"version": 1, "default_libs_subdir": "libs",
    "packages": {"echarts": {"description":..., "files":[...], "snippet":...}, ...}}`。
    manifest 缺失或非法时返回 `{"version": 1, "packages": {}}`，调用方按"无可用包"
    处理；不会抛异常，避免 manifest 文件被误删时拖垮 artifact 写入主流程。
    """
    global _html_libs_manifest_cache, _html_libs_manifest_mtime
    try:
        st = _HTML_LIBS_MANIFEST_PATH.stat()
    except OSError:
        if force_reload or _html_libs_manifest_cache is None:
            _html_libs_manifest_cache = {"version": 1, "packages": {}}
            _html_libs_manifest_mtime = 0.0
        return _html_libs_manifest_cache or {"version": 1, "packages": {}}

    if (
        not force_reload
        and _html_libs_manifest_cache is not None
        and st.st_mtime == _html_libs_manifest_mtime
    ):
        return _html_libs_manifest_cache

    try:
        with _HTML_LIBS_MANIFEST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("加载 web/res/manifest.json 失败: %s", exc)
        _html_libs_manifest_cache = {"version": 1, "packages": {}}
        _html_libs_manifest_mtime = st.st_mtime
        return _html_libs_manifest_cache

    if not isinstance(data, dict):
        data = {"version": 1, "packages": {}}
    pkgs = data.get("packages") if isinstance(data.get("packages"), dict) else {}
    # 规范化：保留主要字段；去掉无效项；按文件存在性过滤
    norm_pkgs: dict[str, dict] = {}
    for name, meta in pkgs.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(meta, dict):
            continue
        files = meta.get("files")
        if not isinstance(files, list) or not files:
            continue
        files_norm: list[str] = []
        for fn in files:
            if not isinstance(fn, str):
                continue
            fn_clean = fn.strip().replace("\\", "/").lstrip("./")
            if not fn_clean or "/" in fn_clean or ".." in fn_clean:
                # 包内文件名不允许带路径分隔，避免越界
                continue
            files_norm.append(fn_clean)
        if not files_norm:
            continue
        norm_pkgs[name.strip()] = {
            "title": str(meta.get("title") or name).strip(),
            "version": str(meta.get("version") or "").strip(),
            "global": str(meta.get("global") or "").strip(),
            "description": str(meta.get("description") or "").strip(),
            "files": files_norm,
            "snippet": str(meta.get("snippet") or "").strip(),
        }
    out = {
        "version": int(data.get("version") or 1),
        "default_libs_subdir": str(data.get("default_libs_subdir") or _HTML_LIBS_DEFAULT_SUBDIR).strip()
        or _HTML_LIBS_DEFAULT_SUBDIR,
        "packages": norm_pkgs,
    }
    _html_libs_manifest_cache = out
    _html_libs_manifest_mtime = st.st_mtime
    return out


def _resolve_libs_subdir(libs_subdir: Optional[str]) -> str:
    """归一化 libs_subdir：None/未传 → 默认 'libs'；空串 → 平铺到 artifact 根目录。"""
    if libs_subdir is None:
        return _HTML_LIBS_DEFAULT_SUBDIR
    raw = str(libs_subdir).strip()
    if not raw:
        return ""  # 显式平铺
    # 绝对路径（含 Windows 盘符）直接拒绝；strip("/\\") 之前先判断，避免吞掉前导分隔符
    if (
        raw.startswith("/")
        or raw.startswith("\\")
        or os.path.isabs(raw)
    ):
        raise ValueError("libs_subdir 不允许是绝对路径")
    s = raw.replace("\\", "/")
    if ".." in s.split("/"):
        raise ValueError("libs_subdir 不允许包含 ..")
    if _UNSAFE_PART_RE.search(s):
        raise ValueError("libs_subdir 含非法字符")
    return s


def _provision_libs_into(dest_dir: Path, libs: list[str], libs_subdir: str) -> dict:
    """把 manifest 中 `libs` 指定的包文件复制到 `dest_dir/<libs_subdir>/` 下。

    返回 `{"copied": {pkg: [rel_path,...]}, "snippets": [str,...], "missing": [pkg,...],
    "bytes": int, "files": int}`，方便上层把"额外占用"计入 artifact 元信息并回送 AI。
    """
    manifest = load_html_libs_manifest()
    pkgs = manifest.get("packages") or {}
    copied: dict[str, list[str]] = {}
    snippets: list[str] = []
    missing: list[str] = []
    total_bytes = 0
    total_files = 0
    seen_targets: set[str] = set()  # 同 artifact 多个包共用同名文件（如 d3）只复制一次

    target_root = dest_dir if not libs_subdir else (dest_dir / libs_subdir)
    for name in libs or []:
        key = (name or "").strip()
        if not key:
            continue
        meta = pkgs.get(key)
        if not meta:
            missing.append(key)
            continue
        target_root.mkdir(parents=True, exist_ok=True)
        pkg_dir = (_HTML_LIBS_ROOT / key).resolve()
        rel_paths: list[str] = []
        for fn in meta["files"]:
            src = (pkg_dir / fn).resolve()
            try:
                src.relative_to(_HTML_LIBS_ROOT.resolve())
            except ValueError:
                logger.warning("provision_libs: 拒绝越界路径 pkg=%s file=%s", key, fn)
                continue
            if not src.exists() or not src.is_file():
                logger.warning("provision_libs: 资源缺失 pkg=%s file=%s", key, fn)
                continue
            target_rel = fn if not libs_subdir else f"{libs_subdir}/{fn}"
            target = (dest_dir / target_rel).resolve()
            try:
                target.relative_to(dest_dir.resolve())
            except ValueError:
                logger.warning("provision_libs: 目标越界 pkg=%s target=%s", key, target_rel)
                continue
            if target_rel not in seen_targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(src), str(target))
                seen_targets.add(target_rel)
                total_bytes += target.stat().st_size
                total_files += 1
            rel_paths.append(target_rel)
        if rel_paths:
            copied[key] = rel_paths
            if meta.get("snippet"):
                # 若调用方使用了非默认子目录或扁平复制，把 snippet 中的 "./libs/" 替换为实际相对前缀
                prefix = (libs_subdir + "/") if libs_subdir else ""
                snippet = meta["snippet"].replace("./libs/", f"./{prefix}")
                snippets.append(snippet)
        else:
            missing.append(key)
    return {
        "copied": copied,
        "snippets": snippets,
        "missing": missing,
        "bytes": total_bytes,
        "files": total_files,
    }


# ───────────────────────── 内部工具 ─────────────────────────


def _today_subdir() -> str:
    """'YYYY/MM/DD'（UTC），用于按日期组织 artifact 目录。"""
    return datetime.utcnow().strftime("%Y/%m/%d")


def _short_id() -> str:
    """短 id（8 字符），附加到日期目录后，形成最终 storage_subdir。"""
    return _uuid.uuid4().hex[:8]


def _get_artifact_root(user: dict) -> Path:
    """web/fs/<safe_username>/chats/（与聊天附件共用）"""
    root = get_user_fs_root(user) / ARTIFACT_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slugify(s: str, default: str = "artifact") -> str:
    """仅保留可放进目录名/文件名的字符。"""
    s = (s or "").strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "", s)
    return (s[:60] or default)


def _validate_relative_path(rel: str) -> str:
    """规范化 AI 传入的相对路径，拒绝越界 / 非法字符 / 保留字 / 未授权扩展名。"""
    if not rel or not isinstance(rel, str):
        raise ValueError("文件路径为空")
    # 统一分隔符并清理首尾
    p = rel.replace("\\", "/").strip().lstrip("/")
    if not p:
        raise ValueError("文件路径为空")
    if p.endswith("/"):
        raise ValueError("文件路径不能以 / 结尾")
    # 按 POSIX 语义：`.` 段表示"当前目录"，对路径解析无影响 → 静默吸收（典型场景：
    # AI 写出 `./libs/foo.js`，前端把这串原样塞进 file?path=... 不应被 400 拒）。
    # `..` 仍然严格拒绝（越界风险）。
    parts = [x for x in p.split("/") if x and x != "."]
    if not parts:
        raise ValueError("文件路径非法")
    for part in parts:
        if part == "..":
            raise ValueError("路径中禁止使用 ..")
        if _UNSAFE_PART_RE.search(part):
            raise ValueError(f"路径包含非法字符: {part}")
        stem = part.split(".", 1)[0].upper()
        if stem in _WIN_RESERVED:
            raise ValueError(f"路径段落是系统保留字: {part}")
        if len(part) > 120:
            raise ValueError("单段路径过长（>120 字符）")
    # 扩展名白名单
    last = parts[-1].lower()
    _, dot, ext = last.rpartition(".")
    if not dot:
        raise ValueError("文件必须带扩展名")
    if ("." + ext) not in _ALLOWED_EXTS:
        raise ValueError(
            f"不允许的扩展名 .{ext}（仅支持 {sorted(_ALLOWED_EXTS)} 中的类型）"
        )
    return "/".join(parts)


def _coerce_bytes(content: Any, encoding: Optional[str]) -> bytes:
    """把 AI 工具传入的 content 转成 bytes。

    - encoding='base64'：content 必须是 base64 字符串
    - encoding in ('utf-8','utf8',None) 且 content 是 str：utf-8 编码
    - content 已是 bytes/bytearray：直接转 bytes
    - content 不是 str/bytes：json.dumps 后编 utf-8（方便 AI 直接丢 dict/list）
    """
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    enc = (encoding or "").strip().lower()
    if enc in ("base64", "b64"):
        if not isinstance(content, str):
            raise ValueError("base64 编码要求 content 是字符串")
        try:
            return base64.b64decode(content, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"base64 解码失败: {exc}") from exc
    if isinstance(content, str):
        return content.encode("utf-8")
    # 复杂对象（dict/list）直接 JSON 化
    import json as _json
    return _json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")


def _attachment_disposition(name: str) -> str:
    """构造 Content-Disposition 的 filename（兼容非 ASCII 名）。"""
    from urllib.parse import quote
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "artifact"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name)}'


# ───────────────────────── 鉴权（header 或 ?token= 均可） ─────────────────────────


async def _resolve_user_via_header_or_query(request: Request) -> dict:
    """让 `<a href="/api/ai/artifacts/.../download?token=...">` 等直接链接也能访问。"""
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


# ───────────────────────── db / 路径解析 ─────────────────────────


async def _load_artifact_row(db, uuid_str: str) -> Optional[dict]:
    rows = await db.execute_fetchall(
        """SELECT id, uuid, user_id, session_id, message_id, title, description, kind,
                  storage_subdir, entry_file, file_count, total_bytes, created_at
             FROM ai_artifacts WHERE uuid = ?""",
        (uuid_str,),
    )
    return dict(rows[0]) if rows else None


async def _load_username(db, user_id: int) -> str:
    rows = await db.execute_fetchall("SELECT username FROM users WHERE id = ?", (user_id,))
    return (rows[0]["username"] if rows else "default") or "default"


def _artifact_dir_for(row: dict, username: str) -> Path:
    """根据行与 username 推出 artifact 的物理目录，并做边界校验。"""
    root = _get_artifact_root({"username": username}).resolve()
    subdir = (row.get("storage_subdir") or "").strip().strip("/\\").replace("\\", "/")
    if not subdir:
        raise HTTPException(status_code=500, detail="artifact 目录元信息缺失")
    path = (root / subdir).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="artifact 路径越界") from exc
    return path


def _artifact_to_dict(row: dict) -> dict:
    return {
        "uuid": row.get("uuid"),
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "kind": row.get("kind") or "bundle",
        "storage_subdir": row.get("storage_subdir") or "",
        "entry_file": row.get("entry_file") or "",
        "file_count": int(row.get("file_count") or 0),
        "total_bytes": int(row.get("total_bytes") or 0),
        "session_id": row.get("session_id"),
        "message_id": row.get("message_id"),
        "created_at": row.get("created_at"),
        "download_url": f"/api/ai/artifacts/{row.get('uuid')}/download",
        "preview_url": (
            f"/api/ai/artifacts/{row.get('uuid')}/file?path=" + (row.get("entry_file") or "")
            if row.get("entry_file") else ""
        ),
    }


async def _ensure_session_owned(db, user_id: int, session_id: int) -> None:
    rows = await db.execute_fetchall(
        "SELECT id FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")


# ───────────────────────── 核心：创建 artifact（供 AI 工具调用） ─────────────────────────


async def create_artifact(
    db,
    user: dict,
    *,
    title: str,
    description: str = "",
    files: Iterable[dict] | None = None,
    entry_file: Optional[str] = None,
    session_id: Optional[int] = None,
    message_id: Optional[int] = None,
    libs: Optional[list[str]] = None,
    libs_subdir: Optional[str] = None,
) -> dict:
    """把一组 (path, content) 文件物化为一个 artifact 并登记到数据库。

    files: [{"path": "rel/path.ext", "content": "...", "encoding": "utf-8|base64"}...]
    entry_file: 推荐入口文件（index.html / report.md / data.csv 等）；若留空且只有一个文件则自动指向它。
    libs: 可选；按 `web/res/manifest.json` 中的包名（如 ["echarts","mermaid"]），
          后端会把对应文件复制到 artifact 的 `<libs_subdir>/` 子目录，让生成出的
          HTML 自包含全部依赖（无需联网、可单独下载）。
    libs_subdir: 复制目标子目录名，默认 "libs"；传 "" 表示扁平复制到 artifact 根目录。
    返回 `_artifact_to_dict(row)` 结构，并附 `libs_provided` 字段（若指定了 libs）。
    """
    files = list(files or [])
    if not files:
        raise ValueError("files 不能为空")
    if len(files) > MAX_FILES:
        raise ValueError(f"单个 artifact 文件数不能超过 {MAX_FILES}")

    # 校验 session 归属
    if session_id is not None:
        await _ensure_session_owned(db, user["id"], int(session_id))

    # 规范化所有文件路径 + 换算 bytes + 校验大小
    plan: list[tuple[str, bytes]] = []
    total = 0
    for idx, f in enumerate(files):
        if not isinstance(f, dict):
            raise ValueError(f"files[{idx}] 必须是 {{path, content[, encoding]}}")
        try:
            rel = _validate_relative_path(str(f.get("path") or ""))
        except ValueError as exc:
            raise ValueError(f"files[{idx}] 路径非法: {exc}") from exc
        try:
            data = _coerce_bytes(f.get("content"), f.get("encoding"))
        except ValueError as exc:
            raise ValueError(f"files[{idx}] 内容解码失败: {exc}") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"files[{idx}]（{rel}）超过单文件上限 {MAX_FILE_BYTES // (1024 * 1024)} MB")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"artifact 总字节超出上限 {MAX_TOTAL_BYTES // (1024 * 1024)} MB")
        plan.append((rel, data))

    # 防同路径重复（后者覆盖前者时给出警告而不是报错；这里直接拒绝以避免 AI 无意覆盖）
    seen_paths = set()
    for rel, _data in plan:
        if rel in seen_paths:
            raise ValueError(f"files 中存在重复路径: {rel}")
        seen_paths.add(rel)

    # 选 entry_file
    if entry_file:
        entry_file = _validate_relative_path(entry_file)
        if entry_file not in seen_paths:
            raise ValueError(f"entry_file '{entry_file}' 不在 files 中")
    elif len(plan) == 1:
        entry_file = plan[0][0]
    else:
        # 找常见首页
        for candidate in ("index.html", "index.htm", "README.md", "report.md", "report.html"):
            if candidate in seen_paths:
                entry_file = candidate
                break
        if not entry_file:
            entry_file = plan[0][0]

    kind = "single_file" if len(plan) == 1 else "bundle"

    # 生成 storage_subdir：sessions/<id>/<slug>-<shortid>（无 session 则日期兼容）
    from api.chat_attachments import session_storage_subdir as _session_storage_subdir

    short = _short_id()
    slug = _slugify(title or "artifact")
    leaf = f"{slug}-{short}" if slug and slug != "artifact" else short
    storage_subdir = _session_storage_subdir(session_id, leaf=leaf)
    root = _get_artifact_root(user)
    dest = (root / storage_subdir).resolve()
    try:
        dest.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("artifact 路径越界") from exc

    # 落盘：先 mkdir，再写每个文件
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for rel, data in plan:
            target = (dest / rel).resolve()
            try:
                target.relative_to(dest)
            except ValueError as exc:
                raise ValueError(f"文件路径越界: {rel}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except OSError as exc:
        logger.exception("写入 artifact 失败: %s", exc)
        # 尝试回滚已写入的目录
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
        raise ValueError(f"保存 artifact 失败: {exc}") from exc

    # 复制 HTML 自包含依赖（echarts / mermaid / markmap 等）。失败不打断主流程：
    # AI 写出的 HTML 文件已经落盘，缺依赖只会让浏览器引用 404；上层会在返回里
    # 暴露 missing 列表，模型可以在下一轮通过 fs_write_file 自行补救或换包名。
    libs_provision: dict | None = None
    libs_norm = [str(x).strip() for x in (libs or []) if str(x).strip()]
    if libs_norm:
        try:
            sub = _resolve_libs_subdir(libs_subdir)
        except ValueError as exc:
            # 回滚整个 artifact 目录，避免半成品
            try:
                shutil.rmtree(dest, ignore_errors=True)
            except Exception:
                pass
            raise ValueError(f"libs_subdir 非法: {exc}") from exc
        try:
            libs_provision = _provision_libs_into(dest, libs_norm, sub)
        except OSError as exc:
            logger.warning("provision_libs 写入失败: %s", exc)
            libs_provision = {
                "copied": {},
                "snippets": [],
                "missing": libs_norm,
                "bytes": 0,
                "files": 0,
                "error": str(exc),
            }
        else:
            # 复制成功的 vendor 文件计入总占用，方便后续配额统计；
            # 不强行卡 MAX_TOTAL_BYTES，因为 vendor 是受控固定集合，避免误伤合法调用。
            total += int(libs_provision.get("bytes") or 0)

    # 只要 provision 真正复制了文件，artifact 实际上是「HTML + 子资源」结构 ——
    # 必须升级为 bundle，否则前端 srcdoc 预览不会触发相对引用改写、下载也不会打包
    # libs 目录。这一步必须在 INSERT 之前。
    if libs_provision and int(libs_provision.get("files") or 0) > 0:
        kind = "bundle"

    uuid_str = _uuid.uuid4().hex
    final_file_count = len(plan) + int((libs_provision or {}).get("files") or 0)
    await db.execute(
        """INSERT INTO ai_artifacts
               (uuid, user_id, session_id, message_id, title, description, kind,
                storage_subdir, entry_file, file_count, total_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid_str,
            user["id"],
            int(session_id) if session_id is not None else None,
            int(message_id) if message_id is not None else None,
            (title or "").strip()[:200],
            (description or "").strip()[:1000],
            kind,
            storage_subdir,
            entry_file or "",
            final_file_count,
            total,
        ),
    )
    await db.commit()
    row = await _load_artifact_row(db, uuid_str)
    out = _artifact_to_dict(row or {
        "uuid": uuid_str,
        "title": title,
        "description": description,
        "kind": kind,
        "storage_subdir": storage_subdir,
        "entry_file": entry_file or "",
        "file_count": final_file_count,
        "total_bytes": total,
        "session_id": session_id,
        "message_id": message_id,
    })
    if libs_provision is not None:
        out["libs_provided"] = libs_provision
    return out


def _build_artifact_write_plan(files: Iterable[dict]) -> tuple[list[tuple[str, bytes]], set[str], int]:
    """校验 files 参数并返回 (plan, seen_paths, new_bytes_sum)。"""
    plan: list[tuple[str, bytes]] = []
    total = 0
    seen_paths: set[str] = set()
    file_list = list(files or [])
    if not file_list:
        raise ValueError("files 不能为空")
    if len(file_list) > MAX_FILES:
        raise ValueError(f"单个 artifact 文件数不能超过 {MAX_FILES}")
    for idx, f in enumerate(file_list):
        if not isinstance(f, dict):
            raise ValueError(f"files[{idx}] 必须是 {{path, content[, encoding]}}")
        try:
            rel = _validate_relative_path(str(f.get("path") or ""))
        except ValueError as exc:
            raise ValueError(f"files[{idx}] 路径非法: {exc}") from exc
        try:
            data = _coerce_bytes(f.get("content"), f.get("encoding"))
        except ValueError as exc:
            raise ValueError(f"files[{idx}] 内容解码失败: {exc}") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"files[{idx}]（{rel}）超过单文件上限 {MAX_FILE_BYTES // (1024 * 1024)} MB")
        if rel in seen_paths:
            raise ValueError(f"files 中存在重复路径: {rel}")
        seen_paths.add(rel)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"artifact 总字节超出上限 {MAX_TOTAL_BYTES // (1024 * 1024)} MB")
        plan.append((rel, data))
    return plan, seen_paths, total


def _scan_artifact_dir_stats(dir_path: Path) -> tuple[int, int]:
    count = 0
    total = 0
    if not dir_path.is_dir():
        return 0, 0
    for p in dir_path.rglob("*"):
        if p.is_file():
            count += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return count, total


async def update_artifact(
    db,
    user: dict,
    *,
    uuid: str,
    files: Iterable[dict] | None = None,
    title: str | None = None,
    description: str | None = None,
    entry_file: str | None = None,
) -> dict:
    """在原 artifact 目录内覆盖/追加文件，**不新建 UUID**。用于用户要求「改报告里的小问题」等增量修订。"""
    uuid_str = (uuid or "").strip()
    if not uuid_str:
        raise ValueError("uuid 不能为空")
    rows = await load_artifacts_for_user(db, int(user["id"]), [uuid_str])
    if not rows:
        raise ValueError("artifact 不存在或无权访问")
    row = dict(rows[0])
    username = await _load_username(db, int(user["id"]))
    dest = _artifact_dir_for(row, username)
    if not dest.is_dir():
        raise ValueError("artifact 物理目录不存在，无法更新")

    plan, seen_paths, _ = _build_artifact_write_plan(files)
    try:
        for rel, data in plan:
            target = (dest / rel).resolve()
            try:
                target.relative_to(dest.resolve())
            except ValueError as exc:
                raise ValueError(f"文件路径越界: {rel}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except OSError as exc:
        logger.exception("更新 artifact 失败 uuid=%s: %s", uuid_str, exc)
        raise ValueError(f"更新 artifact 失败: {exc}") from exc

    new_entry = (entry_file or "").strip() or (row.get("entry_file") or "")
    if entry_file:
        new_entry = _validate_relative_path(entry_file)
        entry_path = (dest / new_entry).resolve()
        if not entry_path.is_file():
            raise ValueError(f"entry_file '{new_entry}' 在 artifact 中不存在")

    new_title = (title or row.get("title") or "").strip()[:200]
    new_desc = (description if description is not None else row.get("description") or "")
    new_desc = str(new_desc).strip()[:1000]
    file_count, total_bytes = _scan_artifact_dir_stats(dest)
    kind = row.get("kind") or "bundle"
    if file_count <= 1:
        kind = "single_file"
    elif file_count > 1:
        kind = "bundle"

    await db.execute(
        """UPDATE ai_artifacts SET title=?, description=?, kind=?, entry_file=?,
                  file_count=?, total_bytes=? WHERE uuid=? AND user_id=?""",
        (
            new_title,
            new_desc,
            kind,
            new_entry,
            file_count,
            total_bytes,
            uuid_str,
            int(user["id"]),
        ),
    )
    await db.commit()
    updated = await _load_artifact_row(db, uuid_str)
    out = _artifact_to_dict(updated or row)
    out["updated"] = True
    out["updated_paths"] = [rel for rel, _ in plan]
    return out


# ───────────────────────── 路由 ─────────────────────────


@router.get("")
async def list_artifacts(session_id: Optional[int] = None, user=Depends(get_current_user)):
    db = await get_db()
    if session_id is not None:
        rows = await db.execute_fetchall(
            """SELECT id, uuid, user_id, session_id, message_id, title, description, kind,
                      storage_subdir, entry_file, file_count, total_bytes, created_at
                 FROM ai_artifacts
                WHERE user_id = ? AND session_id = ?
                ORDER BY id DESC LIMIT 200""",
            (user["id"], int(session_id)),
        )
    else:
        rows = await db.execute_fetchall(
            """SELECT id, uuid, user_id, session_id, message_id, title, description, kind,
                      storage_subdir, entry_file, file_count, total_bytes, created_at
                 FROM ai_artifacts
                WHERE user_id = ? ORDER BY id DESC LIMIT 200""",
            (user["id"],),
        )
    return {"success": True, "artifacts": [_artifact_to_dict(dict(r)) for r in rows]}


@router.get("/{uuid_str}/meta")
async def artifact_meta(uuid_str: str, user=Depends(get_current_user)):
    db = await get_db()
    row = await _load_artifact_row(db, uuid_str)
    if not row:
        raise HTTPException(status_code=404, detail="成果物不存在")
    if row["user_id"] != user["id"] and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="无权访问该成果物")
    return {"success": True, "artifact": _artifact_to_dict(row)}


@router.get("/{uuid_str}/download")
async def download_artifact(uuid_str: str, request: Request):
    """单文件 → 直接文件流；bundle → 服务端流式打 tar.gz 返回。"""
    user = await _resolve_user_via_header_or_query(request)
    db = await get_db()
    row = await _load_artifact_row(db, uuid_str)
    if not row:
        raise HTTPException(status_code=404, detail="成果物不存在")
    if row["user_id"] != user["id"] and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="无权下载该成果物")
    username = await _load_username(db, row["user_id"])
    root_dir = _artifact_dir_for(row, username)
    if not root_dir.exists() or not root_dir.is_dir():
        raise HTTPException(status_code=404, detail="成果物目录已丢失")

    slug = _slugify(row.get("title") or "artifact")

    # 单文件：直接返流
    if row.get("kind") == "single_file":
        entry = (row.get("entry_file") or "").strip()
        if not entry:
            raise HTTPException(status_code=500, detail="single_file 缺少 entry_file")
        try:
            rel = _validate_relative_path(entry)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"入口文件非法: {exc}") from exc
        target = (root_dir / rel).resolve()
        try:
            target.relative_to(root_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="文件路径越界") from exc
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        import mimetypes as _mt
        mt, _ = _mt.guess_type(target.name)
        return FileResponse(
            str(target),
            media_type=mt or "application/octet-stream",
            filename=target.name,
        )

    # bundle：流式 tar.gz
    archive_name = f"{slug}.tgz"

    def _iter_tgz() -> Iterable[bytes]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=5) as tf:
            # 以 artifact 的 title-slug 作为 tar 内的顶层目录
            top = slug or "artifact"
            for fpath in sorted(root_dir.rglob("*")):
                if fpath.is_dir():
                    continue
                try:
                    rel = fpath.relative_to(root_dir).as_posix()
                except ValueError:
                    continue
                arcname = f"{top}/{rel}"
                try:
                    tf.add(str(fpath), arcname=arcname, recursive=False)
                except OSError as exc:
                    logger.warning("打包 artifact 跳过 %s: %s", fpath, exc)
                # 分块输出，避免把整个压缩包都攒内存里
                data = buf.getvalue()
                if data:
                    yield data
                    buf.seek(0)
                    buf.truncate(0)
        tail = buf.getvalue()
        if tail:
            yield tail

    return StreamingResponse(
        _iter_tgz(),
        media_type="application/gzip",
        headers={"Content-Disposition": _attachment_disposition(archive_name)},
    )


# 预览允许的 MIME 前缀白名单：HTML / 文本 / 图片 / PDF / JSON / SVG 等。
# 其它类型仍可通过 download=1 以 attachment 方式取回。
_INLINE_MIME_ALLOW_PREFIXES = ("text/", "image/", "application/json", "application/pdf", "application/xml")

# 与 web/js/app.js `edgeopsRewriteArtifactHtmlRefs` 对齐：
# 相对 src/href / importmap / ESM import → `/files/<rel>?token=…`
_REL_ATTR_RE = re.compile(
    r"\b(src|href)\s*=\s*(?P<q>['\"])(?P<v>[^'\"]*)(?P=q)",
    re.IGNORECASE,
)
_SKIP_SCHEME_RE = re.compile(
    r"^(https?:|data:|blob:|mailto:|tel:|javascript:|#|//)",
    re.IGNORECASE,
)
_IMPORTMAP_SCRIPT_RE = re.compile(
    r"(<script\b(?=[^>]*\btype\s*=\s*['\"]importmap['\"])[^>]*>)(.*?)(</script\s*>)",
    re.IGNORECASE | re.DOTALL,
)
# import './x.js' | import … from './x.js' | import('./x.js') | export … from './x.js'
_ESM_REL_SPEC_RE = re.compile(
    r"(?P<pre>(?:\bimport\s*\(\s*|\b(?:import|export)\s+(?:type\s+)?(?:[^'\"\n;]|'[^']*'|\"[^\"]*\")*?\s+from\s+|\bimport\s+))"
    r"(?P<q>['\"])(?P<v>\./[^'\"]+)(?P=q)",
    re.IGNORECASE | re.DOTALL,
)
_REL_ASSET_EXT_RE = re.compile(
    r"\.(?:js|mjs|cjs|css|json|wasm|map|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf|html?)$",
    re.IGNORECASE,
)


def _is_relative_asset_url(url: str) -> bool:
    """判断是否应改写的相对资源路径（排除 bare specifier，如 importmap 的 key `three`）。"""
    trimmed = str(url or "").strip()
    if not trimmed or _SKIP_SCHEME_RE.match(trimmed) or trimmed.startswith("/"):
        return False
    if trimmed.startswith("../") or "/../" in trimmed.replace("\\", "/"):
        return False
    norm = trimmed.replace("\\", "/")
    if norm.startswith("./"):
        return True
    if "/" in norm:
        return True
    return bool(_REL_ASSET_EXT_RE.search(norm.split("?", 1)[0].split("#", 1)[0]))


def _rewrite_artifact_html_refs_for_token(html: str, uuid_str: str, token: str) -> str:
    """新窗口打开 `…/files/index.html?token=…` 时，浏览器解析相对子资源不会附带 query，
    缺 token → 401。入口 HTML 带 ?token= 时，内联返回前把相对引用改写成带同一 token
    的绝对路径（与站内 iframe 预览改写一致）。

    覆盖：
    - `<script src>` / `<link href>` / `<img src>` 等属性
    - `<script type="importmap">` 内相对 URL（three ESM 常用）
    - `import './x.js'` / `from './x.js'` / `import('./x.js')` 相对模块说明符
    """
    enc_uuid = quote(uuid_str, safe="")
    token_q = "?token=" + quote(token, safe="")

    def rewrite_one(url: str) -> str:
        trimmed = url.strip()
        if not trimmed:
            return url
        if _SKIP_SCHEME_RE.match(trimmed) or trimmed.startswith("/"):
            return url
        if not _is_relative_asset_url(trimmed):
            return url
        hash_part = ""
        hidx = trimmed.find("#")
        if hidx >= 0:
            hash_part = trimmed[hidx:]
            trimmed = trimmed[:hidx]
        query_part = ""
        qidx = trimmed.find("?")
        if qidx >= 0:
            query_part = trimmed[qidx:]
            trimmed = trimmed[:qidx]
        normalized = trimmed.replace("\\", "/").lstrip("/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("../") or "/../" in normalized:
            return url
        if not normalized:
            return url
        encoded = "/".join(quote(seg, safe="") for seg in normalized.split("/"))
        out = f"/api/ai/artifacts/{enc_uuid}/files/{encoded}{token_q}"
        if query_part:
            out += "&_q=" + quote(query_part[1:], safe="")
        out += hash_part
        return out

    def rewrite_importmap_body(body: str) -> str:
        try:
            data = json.loads(body)

            def walk(obj: Any) -> Any:
                if isinstance(obj, dict):
                    return {k: walk(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [walk(x) for x in obj]
                if isinstance(obj, str):
                    return rewrite_one(obj)
                return obj

            return json.dumps(walk(data), ensure_ascii=False, indent=2)
        except (TypeError, ValueError, json.JSONDecodeError):
            # JSON 解析失败时退回：只改写形如 "./libs/..." 的引号字符串
            return re.sub(
                r"(['\"])(\./[^'\"]+)\1",
                lambda m: f"{m.group(1)}{rewrite_one(m.group(2))}{m.group(1)}",
                body,
            )

    def repl_attr(m: re.Match[str]) -> str:
        attr, q, v = m.group(1), m.group("q"), m.group("v")
        return f"{attr}={q}{rewrite_one(v)}{q}"

    out = _REL_ATTR_RE.sub(repl_attr, html)
    out = _IMPORTMAP_SCRIPT_RE.sub(
        lambda m: m.group(1) + rewrite_importmap_body(m.group(2)) + m.group(3),
        out,
    )
    out = _ESM_REL_SPEC_RE.sub(
        lambda m: f"{m.group('pre')}{m.group('q')}{rewrite_one(m.group('v'))}{m.group('q')}",
        out,
    )
    return out


# 站内预览 iframe 使用 sandbox（无 allow-same-origin）时文档 origin 为 opaque/null，
# ESM / importmap 拉取同站绝对 URL 会走 CORS；鉴权已在 query token，故允许 *。
_ARTIFACT_FILE_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


def _with_artifact_file_cors(resp: Union[FileResponse, Response]) -> Union[FileResponse, Response]:
    for key, val in _ARTIFACT_FILE_CORS_HEADERS.items():
        resp.headers[key] = val
    return resp


async def _serve_artifact_file(
    uuid_str: str,
    path: str,
    request: Request,
    *,
    download: int = 0,
) -> Union[FileResponse, Response]:
    """两种 URL 形式（`/file?path=...` 与 `/files/<rel>`) 共用的实际取文件逻辑。"""
    user = await _resolve_user_via_header_or_query(request)
    db = await get_db()
    row = await _load_artifact_row(db, uuid_str)
    if not row:
        raise HTTPException(status_code=404, detail="成果物不存在")
    if row["user_id"] != user["id"] and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="无权访问该成果物")
    username = await _load_username(db, row["user_id"])
    root_dir = _artifact_dir_for(row, username)
    try:
        rel = _validate_relative_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"路径非法: {exc}") from exc
    target = (root_dir / rel).resolve()
    try:
        target.relative_to(root_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="路径越界") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    import mimetypes as _mt
    mt, _ = _mt.guess_type(target.name)
    mt = mt or "application/octet-stream"
    # .js / .mjs 在部分环境被猜成 application/javascript，需 inline + CORS 才能给 sandbox 预览里的 ESM 用
    if mt in ("application/javascript", "text/javascript", "application/ecmascript"):
        mt = "text/javascript"
    want_inline = not bool(download) and (
        any(mt.startswith(p) for p in _INLINE_MIME_ALLOW_PREFIXES)
        or mt == "text/javascript"
        or target.suffix.lower() in (".js", ".mjs", ".cjs", ".wasm", ".map")
    )
    if want_inline:
        q_token = (request.query_params.get("token") or "").strip()
        if q_token and mt.startswith("text/html"):
            try:
                raw_html = target.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("读取 artifact HTML 失败 %s: %s", target, exc)
            else:
                body = _rewrite_artifact_html_refs_for_token(raw_html, uuid_str, q_token)
                disp = f'inline; filename="{re.sub(r"[^A-Za-z0-9._-]+", "_", target.name) or "index.html"}"'
                return _with_artifact_file_cors(
                    Response(
                        content=body.encode("utf-8"),
                        media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": disp},
                    )
                )
        # Starlette 0.33+ 支持 content_disposition_type="inline"；毛竹 运行在 0.52
        return _with_artifact_file_cors(
            FileResponse(
                str(target),
                media_type=mt,
                filename=target.name,
                content_disposition_type="inline",
            )
        )
    return _with_artifact_file_cors(
        FileResponse(str(target), media_type=mt, filename=target.name)
    )


@router.get("/{uuid_str}/file")
async def artifact_file(uuid_str: str, path: str, request: Request, download: int = 0):
    """下载/预览 artifact 内的单个文件（query 形式）。

    - 默认（download=0）：以 `inline` Content-Disposition 返回，便于前端
      iframe / <img> / fetch 等方式预览；对潜在危险类型（未知/二进制）
      强制回退为 attachment。
    - download=1：始终以 attachment 强制下载。

    路径形式见 `/{uuid_str}/files/{path:path}`，浏览器把它作为 base URL 解析
    HTML 内的相对引用时更自然。两条路由共享实现 `_serve_artifact_file`。
    """
    return await _serve_artifact_file(uuid_str, path, request, download=download)


@router.get("/{uuid_str}/files/{path:path}")
async def artifact_file_by_path(
    uuid_str: str,
    path: str,
    request: Request,
    download: int = 0,
):
    """下载/预览 artifact 内的单个文件（路径形式）。

    为什么需要这条路由：当用户/AI 在浏览器**新窗口**或外部直接打开
    `…/files/index.html` 时，HTML 内的 `./libs/echarts.min.js` 会被浏览器
    解析为 `…/files/libs/echarts.min.js`（裸子路径）；这正好命中本条路由。
    若仅有 `?path=index.html` 形式，浏览器解析相对引用会得到 `…/libs/...`
    一个不存在的兄弟路径，导致子资源 404。

    新窗口 URL 常为 `…/files/index.html?token=…`：相对脚本不会继承 query，
    子资源请求缺 token 会 401。对带 `?token=` 的 **HTML** 内联响应，服务端
    会把相对 `src`/`href`、`importmap`、ESM `import './…'` 改写成带同一 token
    的绝对路径（与站内 iframe 预览 `edgeopsRewriteArtifactHtmlRefs` 一致）。

    `_validate_relative_path` 已经吸收 `./`、拒绝 `..`，越界路径会返回 400。
    鉴权与 `/file` 完全一致（token 走 query，cookie 走 header）。
    """
    return await _serve_artifact_file(uuid_str, path, request, download=download)


class BindArtifactBody(BaseModel):
    session_id: int
    message_id: Optional[int] = None


@router.post("/{uuid_str}/bind")
async def bind_artifact(uuid_str: str, body: BindArtifactBody, user=Depends(get_current_user)):
    """把 artifact 绑定到会话（及可选消息 id）。"""
    db = await get_db()
    row = await _load_artifact_row(db, uuid_str)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="成果物不存在")
    await _ensure_session_owned(db, user["id"], body.session_id)
    await db.execute(
        "UPDATE ai_artifacts SET session_id = ?, message_id = COALESCE(?, message_id) WHERE uuid = ? AND user_id = ?",
        (body.session_id, body.message_id, uuid_str, user["id"]),
    )
    await db.commit()
    return {"success": True}


@router.delete("/{uuid_str}")
async def delete_artifact(uuid_str: str, user=Depends(get_current_user)):
    db = await get_db()
    row = await _load_artifact_row(db, uuid_str)
    if not row:
        raise HTTPException(status_code=404, detail="成果物不存在")
    if row["user_id"] != user["id"] and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="无权删除该成果物")
    username = await _load_username(db, row["user_id"])
    try:
        dir_path = _artifact_dir_for(row, username)
        if dir_path.exists() and dir_path.is_dir():
            import shutil
            shutil.rmtree(dir_path, ignore_errors=True)
    except HTTPException:
        pass
    except OSError as exc:
        logger.warning("删除 artifact 目录失败 uuid=%s err=%s", uuid_str, exc)
    await db.execute("DELETE FROM ai_artifacts WHERE uuid = ?", (uuid_str,))
    await db.commit()
    return {"success": True}


# ───────────────────────── 对外暴露给 ai_skills ─────────────────────────


async def load_artifacts_for_user(db, user_id: int, uuids: list[str]) -> list[dict]:
    """按 uuid 批量读取属于该用户的 artifact 元信息。"""
    if not uuids:
        return []
    seen = set()
    out: list[dict] = []
    for u in uuids:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        rows = await db.execute_fetchall(
            """SELECT id, uuid, user_id, session_id, message_id, title, description, kind,
                      storage_subdir, entry_file, file_count, total_bytes, created_at
                 FROM ai_artifacts WHERE uuid = ? AND user_id = ?""",
            (u, user_id),
        )
        if rows:
            out.append(dict(rows[0]))
    return out


def humanize_size(size: int) -> str:
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"
