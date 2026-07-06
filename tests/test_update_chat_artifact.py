"""update_chat_artifact 辅助逻辑。"""
from pathlib import Path

from api.ai_artifacts import _build_artifact_write_plan, _scan_artifact_dir_stats


def test_build_artifact_write_plan_rejects_empty():
    try:
        _build_artifact_write_plan([])
        assert False, "expected ValueError"
    except ValueError as e:
        assert "不能为空" in str(e)


def test_scan_artifact_dir_stats(tmp_path: Path):
    d = tmp_path / "art"
    d.mkdir()
    (d / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (d / "libs").mkdir()
    (d / "libs" / "echarts.min.js").write_bytes(b"x" * 100)
    count, total = _scan_artifact_dir_stats(d)
    assert count == 2
    assert total > 100
