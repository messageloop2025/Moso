"""本会话文件资源索引与注入文案。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.session_file_resources import (
    INDEX_FILENAME,
    _merge_resources,
    build_session_file_resources_section,
    record_session_file_resource,
)


def test_record_and_reload_index(tmp_path, monkeypatch):
    # 把用户工作区指到临时目录
    user_root = tmp_path / "u1"
    user_root.mkdir()
    monkeypatch.setattr(
        "api.filesystem.get_user_fs_root",
        lambda user, username_override=None: user_root,
    )
    monkeypatch.setattr("config.CHAT_ATTACHMENT_SUBDIR", "chats")

    record_session_file_resource(
        username="u1",
        session_id=42,
        kind="artifact",
        path="reports/2026/07/25/demo/abc.html",
        uuid="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        title="三维太阳系",
        entry_file="index.html",
        note="create",
    )
    # 同 uuid 再记一次应覆盖而非重复
    record_session_file_resource(
        username="u1",
        session_id=42,
        kind="artifact",
        path="reports/2026/07/25/demo/abc.html",
        uuid="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        title="三维太阳系·发光",
        entry_file="index.html",
        note="update",
    )
    idx = user_root / "chats" / "sessions" / "42" / INDEX_FILENAME
    assert idx.is_file()
    data = json.loads(idx.read_text(encoding="utf-8"))
    assert len(data["files"]) == 1
    assert data["files"][0]["title"] == "三维太阳系·发光"
    assert data["files"][0]["uuid"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_merge_prefers_earlier_groups():
    a = [{"kind": "artifact", "uuid": "u1", "path": "p1", "title": "A"}]
    b = [{"kind": "artifact", "uuid": "u1", "path": "p1", "title": "B"}]
    c = [{"kind": "workspace", "path": "chats/sessions/1/x.txt", "title": "x"}]
    merged = _merge_resources(a, b, c, limit=10)
    assert len(merged) == 2
    assert merged[0]["title"] == "A"
    assert merged[1]["path"].endswith("x.txt")


def test_build_section_includes_guidelines(monkeypatch):
    import asyncio

    class _FakeDb:
        async def execute_fetchall(self, sql, params=None):
            if "ai_artifacts" in sql:
                return [
                    {
                        "uuid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        "title": "太阳系",
                        "kind": "bundle",
                        "storage_subdir": "reports/2026/07/25/solar/bbbb",
                        "entry_file": "index.html",
                        "file_count": 7,
                        "total_bytes": 1000,
                        "created_at": "2026-07-25",
                    }
                ]
            return []

    monkeypatch.setattr(
        "api.ai_artifacts._workspace_relpath_for_artifact",
        lambda sub, entry="": f"{sub}/{entry}" if entry else sub,
    )
    monkeypatch.setattr(
        "api.filesystem.get_user_fs_root",
        lambda user, username_override=None: Path("/tmp/nonexistent-edgeops-fs"),
    )

    text = asyncio.run(
        build_session_file_resources_section(
            _FakeDb(),
            user_id=1,
            session_id=9,
            username="tester",
        )
    )
    assert "本会话文件资源" in text
    assert "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in text
    assert "update_chat_artifact" in text
    assert "整份" in text
