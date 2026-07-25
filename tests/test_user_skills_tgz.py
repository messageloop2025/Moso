"""个人 Skills .tgz 导出/导入：打包结构、安全解压、覆盖语义。"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.user_skills_export import (
    _TGZ_MANIFEST,
    _TGZ_ROOT,
    _TGZ_SKILLS,
    _extract_tgz_safely,
    _safe_tar_member_name,
    export_user_skills_tgz,
    import_user_skills_tgz,
)


def test_safe_tar_member_rejects_traversal():
    with pytest.raises(ValueError):
        _safe_tar_member_name("../etc/passwd")
    with pytest.raises(ValueError):
        _safe_tar_member_name("edgeops-skills/skills/../../x")
    with pytest.raises(ValueError):
        _safe_tar_member_name("/abs/path")
    assert _safe_tar_member_name("edgeops-skills/skills/foo/SKILL.md") == (
        "edgeops-skills/skills/foo/SKILL.md"
    )


def test_extract_tgz_rejects_path_escape(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"evil"
        info = tarfile.TarInfo(name="../outside.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError):
        _extract_tgz_safely(buf.getvalue(), tmp_path / "out")


def test_extract_tgz_rejects_symlink(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=f"{_TGZ_ROOT}/skills/x/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(ValueError):
        _extract_tgz_safely(buf.getvalue(), tmp_path / "out")


def _write_skill_tree(skills_root: Path, slug: str, *, binary: bytes = b"\x00\x01\xff") -> None:
    d = skills_root / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: Test skill for tgz export.\n---\n\n# {slug}\n",
        encoding="utf-8",
    )
    (d / "reference.md").write_text("ref\n", encoding="utf-8")
    (d / "scripts").mkdir()
    (d / "scripts" / "tool.bin").write_bytes(binary)


def test_export_tgz_pack_structure(tmp_path: Path):
    async def _go():
        skills_root = tmp_path / "skills"
        _write_skill_tree(skills_root, "alpha-skill")
        _write_skill_tree(skills_root, "beta-skill", binary=b"BIN")
        user = {"id": 1, "username": "tgzuser"}
        rows = [
            {
                "id": 10,
                "name": "alpha-skill",
                "display_name": "Alpha",
                "description": "A",
                "enabled": 1,
                "chat_enabled": 1,
                "chat_scope_web": 1,
                "chat_scope_host": 1,
                "chat_scope_integration": 0,
                "group_name": "g1",
                "slash_name": "alpha",
                "hooks_enabled": 0,
                "pre_tool_use_matcher": "",
                "pre_tool_use_decision": "ask",
                "allowed_tools": "fs_list",
            },
            {
                "id": 11,
                "name": "beta-skill",
                "display_name": "Beta",
                "description": "B",
                "enabled": 1,
                "chat_enabled": 1,
                "chat_scope_web": 1,
                "chat_scope_host": 0,
                "chat_scope_integration": 0,
                "group_name": "",
                "slash_name": "beta",
                "hooks_enabled": 1,
                "pre_tool_use_matcher": "shell_*",
                "pre_tool_use_decision": "deny",
                "allowed_tools": "",
            },
        ]
        with patch(
            "services.user_skills_export.list_user_skills",
            new=AsyncMock(return_value=rows),
        ), patch(
            "services.user_skills_export.skill_dir_path",
            side_effect=lambda u, name: skills_root / name,
        ):
            blob = await export_user_skills_tgz(
                db=None, user_id=1, user=user, skill_ids=[10], include_disabled=True
            )
        assert blob[:2] == b"\x1f\x8b"  # gzip magic
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            names = set(tf.getnames())
            assert _TGZ_MANIFEST in names
            assert f"{_TGZ_SKILLS}/alpha-skill/SKILL.md" in names
            assert f"{_TGZ_SKILLS}/alpha-skill/scripts/tool.bin" in names
            assert f"{_TGZ_SKILLS}/beta-skill/SKILL.md" not in names
            mf = json.loads(tf.extractfile(_TGZ_MANIFEST).read().decode("utf-8"))
            assert mf["_edgeops"]["format"] == "tgz-tree"
            assert mf["_edgeops"]["version"] == 2
            assert "alpha-skill" in mf["skills"]
            assert mf["skills"]["alpha-skill"]["allowed_tools"] == "fs_list"
            bin_member = tf.extractfile(f"{_TGZ_SKILLS}/alpha-skill/scripts/tool.bin")
            assert bin_member.read() == b"\x00\x01\xff"

    asyncio.run(_go())


def test_import_tgz_skip_existing_and_create(tmp_path: Path):
    async def _go():
        # Build a valid package
        pack_root = tmp_path / "pack"
        skills = pack_root / _TGZ_ROOT / "skills"
        _write_skill_tree(skills, "new-skill")
        manifest = {
            "_edgeops": {"version": 2, "type": "agent-skills", "format": "tgz-tree"},
            "skills": {
                "new-skill": {
                    "display_name": "New",
                    "description": "Desc",
                    "enabled": True,
                    "chat_enabled": True,
                    "chat_scope_web": True,
                    "chat_scope_host": True,
                    "chat_scope_integration": False,
                    "slash_name": "new",
                    "hooks_enabled": False,
                    "pre_tool_use_matcher": "",
                    "pre_tool_use_decision": "ask",
                    "allowed_tools": "",
                }
            },
        }
        (pack_root / _TGZ_ROOT).mkdir(parents=True, exist_ok=True)
        (pack_root / _TGZ_ROOT / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(str(pack_root / _TGZ_ROOT), arcname=_TGZ_ROOT)
        raw = buf.getvalue()

        dest_root = tmp_path / "user-skills"
        user = {"id": 1, "username": "imp"}
        created = []

        async def _get_raw(db, uid, name):
            return {"id": 99, "name": name} if name == "exists-skill" else None

        async def _create(db, uid, user, **kwargs):
            created.append(kwargs.get("name"))
            dest = dest_root / kwargs["name"]
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(kwargs.get("content") or "", encoding="utf-8")
            return {"id": 1, "name": kwargs["name"]}

        with patch(
            "services.user_skills_export.get_user_skill_raw_by_name",
            new=_get_raw,
        ), patch(
            "services.user_skills_export.create_user_skill",
            new=_create,
        ), patch(
            "services.user_skills_export.update_user_skill",
            new=AsyncMock(),
        ), patch(
            "services.user_skills_export.skill_dir_path",
            side_effect=lambda u, name: dest_root / name,
        ), patch(
            "services.user_skills_export._resolve_group_id_by_name",
            new=AsyncMock(return_value=None),
        ):
            r1 = await import_user_skills_tgz(None, 1, user, raw, overwrite=False)
            assert "new-skill" in r1["created"]
            assert (dest_root / "new-skill" / "scripts" / "tool.bin").read_bytes() == b"\x00\x01\xff"

            # Second import without overwrite → skip
            async def _get_existing(db, uid, name):
                return {"id": 1, "name": name}

            with patch(
                "services.user_skills_export.get_user_skill_raw_by_name",
                new=_get_existing,
            ):
                r2 = await import_user_skills_tgz(None, 1, user, raw, overwrite=False)
            assert "new-skill" in r2["skipped"]
            assert r2["created"] == []

    asyncio.run(_go())


def test_export_empty_raises():
    async def _go():
        with patch(
            "services.user_skills_export.list_user_skills",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(ValueError, match="没有可导出"):
                await export_user_skills_tgz(None, 1, {"username": "u"})

    asyncio.run(_go())
