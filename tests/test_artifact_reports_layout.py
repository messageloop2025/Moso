"""成果物新布局：reports/年/月/日/示意名/uuid.ext"""
from pathlib import Path

import pytest

from api import ai_artifacts as aa


def test_entry_disk_name_uses_uuid():
    assert aa._entry_disk_name("abc123", "index.html") == "abc123.html"
    assert aa._entry_disk_name("abc123", "report.md") == "abc123.md"


def test_workspace_relpath_new_and_legacy():
    assert (
        aa._workspace_relpath_for_artifact("reports/2026/07/24/巡检", "abc.html")
        == "reports/2026/07/24/巡检/abc.html"
    )
    assert (
        aa._workspace_relpath_for_artifact("sessions/42/foo", "index.html")
        == "chats/sessions/42/foo/index.html"
    )


def test_allocate_report_storage_subdir(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    sub = aa._allocate_report_storage_subdir(reports, "三维山水漫步")
    assert sub.startswith("reports/")
    assert "三维山水漫步" in sub or "三维山水漫步" in sub.replace("-", "")
    # 冲突时追加短 id
    rel = "/".join(sub.split("/")[1:])  # strip reports/
    (reports / rel).mkdir(parents=True)
    sub2 = aa._allocate_report_storage_subdir(reports, "三维山水漫步")
    assert sub2 != sub
    assert sub2.startswith("reports/")


def test_resolve_artifact_abs_dir_new_and_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    user_root = tmp_path / "admin"
    user_root.mkdir()
    monkeypatch.setattr(aa, "get_user_fs_root", lambda user: user_root)

    new_dir = user_root / "reports" / "2026" / "07" / "24" / "demo"
    new_dir.mkdir(parents=True)
    assert aa._resolve_artifact_abs_dir("admin", "reports/2026/07/24/demo") == new_dir.resolve()

    legacy = user_root / "chats" / "sessions" / "9" / "old-art"
    legacy.mkdir(parents=True)
    assert aa._resolve_artifact_abs_dir("admin", "sessions/9/old-art") == legacy.resolve()
