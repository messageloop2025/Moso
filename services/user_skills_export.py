"""每用户 Agent Skills 导出 / 导入（JSON 包 + tgz 目录树）。"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from services.user_skills_registry import (
    create_user_skill,
    get_user_skill_raw_by_name,
    list_user_skills,
    list_skill_resource_files,
    normalize_skill_name,
    read_skill_content,
    skill_dir_path,
    update_user_skill,
    write_skill_content,
)

_TGZ_ROOT = "edgeops-skills"
_TGZ_SKILLS = f"{_TGZ_ROOT}/skills"
_TGZ_MANIFEST = f"{_TGZ_ROOT}/manifest.json"


def export_user_skills_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


async def export_user_skills_bundle(
    db,
    user_id: int,
    user: dict,
    *,
    include_disabled: bool = True,
) -> dict[str, Any]:
    items = await list_user_skills(db, user_id, user)
    skills_out: dict[str, Any] = {}
    for row in items:
        if not include_disabled and not bool(row.get("enabled", 1)):
            continue
        slug = row.get("name") or ""
        if not slug:
            continue
        content = await read_skill_content(user, slug)
        files: dict[str, str] = {}
        for rel in list_skill_resource_files(user, slug):
            if rel == "SKILL.md":
                continue
            path = skill_dir_path(user, slug) / rel.replace("/", Path.sep)
            if path.is_file():
                try:
                    files[rel] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        skills_out[slug] = {
            "display_name": row.get("display_name") or slug,
            "description": row.get("description") or "",
            "enabled": bool(row.get("enabled", 1)),
            "chat_enabled": bool(row.get("chat_enabled", 1)),
            "chat_scope_web": bool(row.get("chat_scope_web", 1)),
            "chat_scope_host": bool(row.get("chat_scope_host", 1)),
            "chat_scope_integration": bool(row.get("chat_scope_integration", 0)),
            "group_id": row.get("group_id"),
            "group_name": row.get("group_name") or "",
            "content": content,
            "files": files,
        }
    return {
        "_edgeops": {"version": 1, "type": "agent-skills"},
        "skills": skills_out,
    }


def parse_skills_import_blob(raw: str | dict) -> dict[str, dict[str, Any]]:
    if isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("JSON 根须为对象")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        raise ValueError("未找到 skills 对象")
    if not skills:
        raise ValueError("skills 为空")
    out: dict[str, dict[str, Any]] = {}
    for key, entry in skills.items():
        slug = normalize_skill_name(str(key))
        if not isinstance(entry, dict):
            raise ValueError(f"{slug}: 配置须为对象")
        out[slug] = entry
    return out


async def import_user_skills_bundle(
    db,
    user_id: int,
    user: dict,
    raw: str | dict,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    parsed = parse_skills_import_blob(raw)
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for slug, entry in parsed.items():
        try:
            content = entry.get("content")
            if content is not None and not str(content).strip():
                content = None
            existing = await get_user_skill_raw_by_name(db, user_id, slug)
            if existing and not overwrite:
                skipped.append(slug)
                continue
            kwargs = {
                "display_name": str(entry.get("display_name") or slug)[:120],
                "description": str(entry.get("description") or "")[:2000],
                "enabled": bool(entry.get("enabled", True)),
                "chat_enabled": bool(entry.get("chat_enabled", True)),
                "chat_scope_web": bool(entry.get("chat_scope_web", True)),
                "chat_scope_host": bool(entry.get("chat_scope_host", True)),
                "chat_scope_integration": bool(entry.get("chat_scope_integration", False)),
            }
            if existing:
                await update_user_skill(
                    db,
                    user_id,
                    user,
                    int(existing["id"]),
                    content=content if content is not None else None,
                    **kwargs,
                )
                updated.append(slug)
            else:
                await create_user_skill(
                    db,
                    user_id,
                    user,
                    name=slug,
                    content=content,
                    **kwargs,
                )
                created.append(slug)
            files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
            base = skill_dir_path(user, slug)
            for rel, text in files.items():
                rel_s = str(rel).strip().replace("\\", "/").lstrip("/")
                if not rel_s or ".." in rel_s.split("/") or rel_s == "SKILL.md":
                    continue
                dest = (base / rel_s).resolve()
                try:
                    dest.relative_to(base.resolve())
                except ValueError:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(text or ""), encoding="utf-8")
        except Exception as e:
            errors.append({"name": slug, "error": str(e)})
    return {
        "success": len(errors) == 0,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "count": len(created) + len(updated),
    }


def _skill_meta_from_row(row: dict[str, Any]) -> dict[str, Any]:
    slug = row.get("name") or ""
    return {
        "display_name": row.get("display_name") or slug,
        "description": row.get("description") or "",
        "enabled": bool(row.get("enabled", 1)),
        "chat_enabled": bool(row.get("chat_enabled", 1)),
        "chat_scope_web": bool(row.get("chat_scope_web", 1)),
        "chat_scope_host": bool(row.get("chat_scope_host", 1)),
        "chat_scope_integration": bool(row.get("chat_scope_integration", 0)),
        "group_name": row.get("group_name") or "",
        "slash_name": row.get("slash_name") or "",
        "hooks_enabled": bool(row.get("hooks_enabled", 0)),
        "allowed_tools": row.get("allowed_tools") or "",
    }


def _safe_tar_member_name(name: str) -> str:
    """规范化并校验 tar 成员路径；非法则抛 ValueError。"""
    raw = (name or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("../") or "/../" in f"/{raw}/":
        raise ValueError(f"非法路径: {name}")
    parts = [p for p in raw.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"非法路径: {name}")
    return "/".join(parts)


def _filter_skill_rows(
    items: list[dict[str, Any]],
    *,
    skill_ids: Optional[Iterable[int]] = None,
    include_disabled: bool = True,
) -> list[dict[str, Any]]:
    id_set: set[int] | None = None
    if skill_ids is not None:
        id_set = {int(x) for x in skill_ids}
        if not id_set:
            return []
    out: list[dict[str, Any]] = []
    for row in items:
        if id_set is not None and int(row.get("id") or 0) not in id_set:
            continue
        if not include_disabled and not bool(row.get("enabled", 1)):
            continue
        if not (row.get("name") or "").strip():
            continue
        out.append(row)
    return out


async def export_user_skills_tgz(
    db,
    user_id: int,
    user: dict,
    *,
    skill_ids: Optional[Iterable[int]] = None,
    include_disabled: bool = True,
) -> bytes:
    """打包个人 Skills 为 gzip tar（manifest + skills/<slug>/ 整目录）。"""
    items = await list_user_skills(db, user_id, user)
    rows = _filter_skill_rows(items, skill_ids=skill_ids, include_disabled=include_disabled)
    if not rows:
        raise ValueError("没有可导出的 Skill")

    manifest_skills: dict[str, Any] = {}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for row in rows:
            slug = normalize_skill_name(str(row["name"]))
            manifest_skills[slug] = _skill_meta_from_row(row)
            skill_dir = skill_dir_path(user, slug)
            if not skill_dir.is_dir():
                # 无目录时至少写入 SKILL.md（从 registry 读）
                content = await read_skill_content(user, slug)
                data = (content or "").encode("utf-8")
                info = tarfile.TarInfo(name=f"{_TGZ_SKILLS}/{slug}/SKILL.md")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
                continue
            tf.add(str(skill_dir), arcname=f"{_TGZ_SKILLS}/{slug}", recursive=True)

        manifest = {
            "_edgeops": {"version": 2, "type": "agent-skills", "format": "tgz-tree"},
            "skills": manifest_skills,
        }
        raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name=_TGZ_MANIFEST)
        info.size = len(raw)
        tf.addfile(info, io.BytesIO(raw))

    return buf.getvalue()


def _extract_tgz_safely(raw_bytes: bytes, dest: Path) -> Path:
    """解压到 dest，仅允许 edgeops-skills/ 下成员；返回包根目录。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            name = _safe_tar_member_name(member.name)
            if not name.startswith(f"{_TGZ_ROOT}/") and name != _TGZ_ROOT:
                raise ValueError(f"包内路径越界: {member.name}")
            # 拒绝链接逃逸
            if member.issym() or member.islnk():
                raise ValueError(f"不允许符号/硬链接: {member.name}")
            target = (dest / name).resolve()
            try:
                target.relative_to(dest.resolve())
            except ValueError as exc:
                raise ValueError(f"包内路径越界: {member.name}") from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    root = dest / _TGZ_ROOT
    if not root.is_dir():
        raise ValueError("无效的 Skills 包：缺少 edgeops-skills/ 根目录")
    return root


def _copy_skill_tree(src_dir: Path, dest_dir: Path) -> None:
    """把解压出的 skill 目录（含二进制）同步到用户 skills/<slug>/。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for path in src_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src_dir).as_posix()
        if ".." in rel.split("/"):
            continue
        target = (dest_dir / rel).resolve()
        try:
            target.relative_to(dest_dir.resolve())
        except ValueError:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(path), str(target))


async def _resolve_group_id_by_name(db, user_id: int, group_name: str) -> int | None:
    name = (group_name or "").strip()
    if not name:
        return None
    rows = await db.execute_fetchall(
        "SELECT id FROM user_skill_groups WHERE user_id=? AND name=? LIMIT 1",
        (user_id, name),
    )
    if not rows:
        return None
    return int(rows[0]["id"])


async def import_user_skills_tgz(
    db,
    user_id: int,
    user: dict,
    raw_bytes: bytes,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """从 .tgz 导入 Skills（自动解压）；语义同 import_user_skills_bundle。"""
    if not raw_bytes:
        raise ValueError("空的 tgz 内容")
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="edgeops-skills-import-") as tmp:
        root = _extract_tgz_safely(raw_bytes, Path(tmp))
        manifest_path = root / "manifest.json"
        manifest_skills: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("skills"), dict):
                    manifest_skills = data["skills"]
            except (OSError, ValueError) as exc:
                raise ValueError(f"manifest.json 无效: {exc}") from exc

        skills_root = root / "skills"
        if not skills_root.is_dir():
            raise ValueError("无效的 Skills 包：缺少 skills/ 目录")

        slug_dirs = sorted(
            [p for p in skills_root.iterdir() if p.is_dir()],
            key=lambda p: p.name.lower(),
        )
        if not slug_dirs and not manifest_skills:
            raise ValueError("skills 为空")

        # 以目录为准；manifest 仅补元数据。目录缺失但 manifest 有的项报错跳过。
        seen: set[str] = set()
        for skill_dir in slug_dirs:
            try:
                slug = normalize_skill_name(skill_dir.name)
            except ValueError as exc:
                errors.append({"name": skill_dir.name, "error": str(exc)})
                continue
            seen.add(slug)
            entry = manifest_skills.get(slug) if isinstance(manifest_skills.get(slug), dict) else {}
            md_path = skill_dir / "SKILL.md"
            if not md_path.is_file():
                errors.append({"name": slug, "error": "缺少 SKILL.md"})
                continue
            try:
                content = md_path.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append({"name": slug, "error": f"读取 SKILL.md 失败: {exc}"})
                continue
            try:
                existing = await get_user_skill_raw_by_name(db, user_id, slug)
                if existing and not overwrite:
                    skipped.append(slug)
                    continue
                kwargs = {
                    "display_name": str(entry.get("display_name") or slug)[:120],
                    "description": str(entry.get("description") or "")[:2000],
                    "enabled": bool(entry.get("enabled", True)),
                    "chat_enabled": bool(entry.get("chat_enabled", True)),
                    "chat_scope_web": bool(entry.get("chat_scope_web", True)),
                    "chat_scope_host": bool(entry.get("chat_scope_host", True)),
                    "chat_scope_integration": bool(entry.get("chat_scope_integration", False)),
                    "slash_name": str(entry.get("slash_name") or ""),
                    "hooks_enabled": bool(entry.get("hooks_enabled", False)),
                    "allowed_tools": str(entry.get("allowed_tools") or ""),
                }
                gname = str(entry.get("group_name") or "").strip()
                group_id = await _resolve_group_id_by_name(db, user_id, gname) if gname else None
                if existing:
                    upd = dict(kwargs)
                    if gname and group_id is not None:
                        upd["group_id"] = group_id
                    await update_user_skill(
                        db,
                        user_id,
                        user,
                        int(existing["id"]),
                        content=content,
                        **upd,
                    )
                    updated.append(slug)
                else:
                    await create_user_skill(
                        db,
                        user_id,
                        user,
                        name=slug,
                        content=content,
                        group_id=group_id,
                        **kwargs,
                    )
                    created.append(slug)
                _copy_skill_tree(skill_dir, skill_dir_path(user, slug))
            except Exception as exc:  # noqa: BLE001
                errors.append({"name": slug, "error": str(exc)})

        for key in manifest_skills:
            try:
                slug = normalize_skill_name(str(key))
            except ValueError:
                continue
            if slug not in seen:
                errors.append({"name": slug, "error": "manifest 中有记录但包内缺少目录"})

    return {
        "success": len(errors) == 0,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "count": len(created) + len(updated),
    }
