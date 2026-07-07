import time
from pathlib import Path

import pytest

from api.filesystem import fs_search_files


@pytest.fixture
def user_base(tmp_path: Path) -> Path:
    root = tmp_path / "alice"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "app.log").write_text("hello", encoding="utf-8")
    (root / "logs" / "app.err").write_text("err", encoding="utf-8")
    (root / "data.txt").write_text("data", encoding="utf-8")
    (root / "subdir").mkdir()
    (root / "subdir" / "nested.md").write_text("# hi", encoding="utf-8")
    return root


def test_fs_search_by_name_regex(user_base: Path):
    out = fs_search_files("", user_base, name_regex=r".*\.log$")
    paths = {item["path"] for item in out["items"]}
    assert out["success"] is True
    assert paths == {"logs/app.log"}
    assert out["items"][0]["id"] == 1
    assert "usage" in out


def test_fs_search_item_ids_sequential(user_base: Path):
    out = fs_search_files("", user_base, limit=10)
    ids = [item["id"] for item in out["items"]]
    assert ids == list(range(1, len(out["items"]) + 1))


def test_fs_search_by_extension_and_size(user_base: Path):
    out = fs_search_files(
        "",
        user_base,
        extensions=[".txt"],
        min_bytes=3,
        max_bytes=10,
    )
    paths = {item["path"] for item in out["items"]}
    assert paths == {"data.txt"}


def test_fs_search_recursive_and_limit(user_base: Path):
    out = fs_search_files("", user_base, path_regex=r"nested", limit=1)
    assert out["count"] == 1
    assert out["items"][0]["path"] == "subdir/nested.md"
    assert out["truncated_results"] is True


def test_fs_search_modified_after(user_base: Path):
    import os

    old = user_base / "old.bin"
    old.write_bytes(b"x")
    past = time.time() - 86400
    os.utime(old, (past, past))

    out = fs_search_files(
        "",
        user_base,
        name_regex=r"old\.bin",
        modified_after=str(int(time.time() - 3600)),
    )
    assert out["count"] == 0

    out2 = fs_search_files(
        "",
        user_base,
        name_regex=r"data\.txt",
        modified_after=str(int(time.time() - 60)),
    )
    assert out2["count"] == 1


def test_fs_search_non_recursive(user_base: Path):
    out = fs_search_files("", user_base, recursive=False)
    paths = {item["path"] for item in out["items"]}
    assert "data.txt" in paths
    assert "subdir/nested.md" not in paths


def test_fs_search_single_condition_only_extensions(user_base: Path):
    out = fs_search_files("", user_base, extensions=[".md"])
    assert out["count"] == 1
    assert out["items"][0]["path"] == "subdir/nested.md"
    assert out["filters_applied"] == {"extensions": [".md"]}
    assert out["filter_logic"] == "and"


def test_fs_search_combined_conditions(user_base: Path):
    out = fs_search_files(
        "logs",
        user_base,
        name_regex=r".*\.log$",
        extensions=[".log"],
        min_bytes=1,
    )
    assert out["count"] == 1
    assert out["items"][0]["path"] == "logs/app.log"
    assert "name_regex" in out["filters_applied"]
    assert "extensions" in out["filters_applied"]
    assert "min_bytes" in out["filters_applied"]


def test_fs_search_no_filters_lists_all_files(user_base: Path):
    out = fs_search_files("", user_base, limit=10)
    paths = {item["path"] for item in out["items"]}
    assert "data.txt" in paths
    assert "logs/app.log" in paths
    assert out["filters_applied"] == {}


def test_fs_search_invalid_regex(user_base: Path):
    with pytest.raises(ValueError, match="正则无效"):
        fs_search_files("", user_base, name_regex="[")
