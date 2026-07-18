"""P2-1：会话级 chats/sessions/<id>/ 路径约定。"""
from api.chat_attachments import (
    _sanitize_subdir,
    get_chats_workspace_dir,
    session_storage_subdir,
)


def test_session_storage_subdir():
    assert session_storage_subdir(42) == "sessions/42"
    assert session_storage_subdir(42, leaf="spill") == "sessions/42/spill"
    assert session_storage_subdir(42, leaf="report-ab12") == "sessions/42/report-ab12"
    assert "/" in session_storage_subdir(None)  # 回退日期


def test_sanitize_accepts_session_and_date():
    assert _sanitize_subdir("sessions/12") == "sessions/12"
    assert _sanitize_subdir("sessions/12/spill") == "sessions/12/spill"
    assert _sanitize_subdir("2026/07/18") == "2026/07/18"
    assert _sanitize_subdir("../etc") == ""
    assert _sanitize_subdir("sessions/../x") == ""


def test_get_chats_workspace_dir_mkdir(tmp_path, monkeypatch):
    import api.chat_attachments as ca
    import api.filesystem as fs

    monkeypatch.setattr(fs, "FS_DIR", tmp_path)
    monkeypatch.setattr(ca, "get_user_fs_root", lambda user: tmp_path / "u")
    user = {"id": 1, "username": "u"}
    info = get_chats_workspace_dir(user, 99)
    assert info["storage_subdir"] == "sessions/99"
    assert info["layout"] == "session"
    assert (tmp_path / "u" / "chats" / "sessions" / "99").is_dir()
