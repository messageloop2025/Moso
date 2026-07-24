"""用户 Memory 空间：初始化、写入、索引、搜索。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import config
from services import user_memory as um


@pytest.fixture
def mem_user(tmp_path, monkeypatch):
    import api.filesystem as fs_mod

    root = tmp_path / "fs"
    root.mkdir()
    monkeypatch.setattr(config, "FS_DIR", root)
    monkeypatch.setattr(fs_mod, "FS_DIR", root)
    monkeypatch.setattr(fs_mod, "FS_ROOT", root)
    user = {"id": 1, "username": "memtest"}
    return user, root


def _run(coro):
    return asyncio.run(coro)


def test_ensure_and_write_host_memory(mem_user):
    user, root = mem_user
    out = _run(um.ensure_memory_workspace(user))
    assert out["success"] is True
    base = root / "memtest"
    assert (base / "memory" / "GUIDE.md").is_file()
    assert (base / "memory" / "INDEX.md").is_file()
    assert (base / "memory" / "hosts").is_dir()

    w = _run(
        um.write_memory_file(
            user,
            content="## Environment\n\n- Ubuntu 22.04\n- nginx 1.24\n",
            host_id=3,
            host_name="web-prod",
            title="web-prod",
            summary="Ubuntu22 + nginx1.24",
            tags=["nginx", "web"],
        )
    )
    assert w["success"] is True
    assert "memory/hosts/h3_web-prod.md" in w["path"].replace("\\", "/")
    path = base / "memory" / "hosts" / "h3_web-prod.md"
    text = path.read_text(encoding="utf-8")
    assert "edgeops-memory" in text
    assert "nginx 1.24" in text

    listed = _run(um.list_memory_entries(user, kind="host"))
    assert listed["count"] >= 1
    assert any(e.get("host_id") == 3 for e in listed["entries"])

    idx = (base / "memory" / "INDEX.md").read_text(encoding="utf-8")
    assert "h3_web-prod" in idx or "web-prod" in idx

    hit = _run(um.search_memory(user, "nginx", host_id=3))
    assert hit["success"] is True
    assert hit["hit_count"] >= 1


def test_memory_map_mentions_sync_caveat():
    text = um.build_memory_map_prompt_section()
    assert "memory/" in text or "Memory" in text
    assert "过时" in text or "实机" in text
    assert "get_session_prompt" in text
    assert "主机知识" in text
    assert "禁止擅自" in text or "征得同意" in text
    assert "缩小探测" in text or "检查" in text
    assert "习惯" in text


def test_parse_meta_and_summary():
    raw = um.build_memory_meta_block(
        kind="host",
        title="x",
        summary="sum",
        host_id=9,
        tags=["a", "b"],
        path="memory/hosts/h9_x.md",
    ) + "# x\n\nhello world\n"
    meta = um.parse_memory_meta(raw)
    assert meta["kind"] == "host"
    assert meta["host_id"] == 9
    assert meta["tags"] == ["a", "b"]
    assert um.infer_summary_from_body("# t\n\n- first line\n") == "first line"


def test_markdown_corpus_under_memory(mem_user):
    user, root = mem_user
    _run(um.ensure_memory_workspace(user))
    _run(
        um.write_memory_file(
            user,
            content="## Status\n\nredis master\n",
            kind="topic",
            title="cache",
            path="memory/topics/cache.md",
            summary="redis notes",
        )
    )
    pairs = _run(um.list_fs_markdown_under(user, "memory"))
    assert any(p.endswith("cache.md") for p, _ in pairs)
