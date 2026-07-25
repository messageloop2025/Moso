"""助手正文 artifact: 链接校验：剔除编造 UUID，补上本轮工具真实 markdown_link。"""
import asyncio
import json

from api.ai_artifacts import (
    extract_artifact_markdown_links_from_tool_trace,
    sanitize_assistant_artifact_links,
)


class _FakeDB:
    def __init__(self, existing: set[str]):
        self.existing = {u.lower() for u in existing}

    async def execute_fetchall(self, sql, params=()):
        user_id = params[0]
        uuids = [str(u).lower() for u in params[1:]]
        assert user_id == 1
        return [{"uuid": u} for u in uuids if u in self.existing]


def test_extract_links_from_tool_trace():
    trace = [
        {
            "type": "tool",
            "event": "finished",
            "tool": "create_chat_artifact",
            "action": "completed",
            "result_preview": json.dumps(
                {
                    "success": True,
                    "artifact": {
                        "uuid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "markdown_link": "[太阳系](artifact:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)",
                    },
                },
                ensure_ascii=False,
            ),
        }
    ]
    links = extract_artifact_markdown_links_from_tool_trace(trace)
    assert links == ["[太阳系](artifact:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)"]


def test_sanitize_removes_hallucinated_and_appends_real():
    async def _go():
        db = _FakeDB({"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"})
        content = "好了：**[假的](artifact:cccccccccccccccccccccccccccccccc)** 请下载"
        trace = [
            {
                "type": "tool",
                "event": "finished",
                "tool": "create_chat_artifact",
                "action": "completed",
                "result_preview": json.dumps(
                    {
                        "success": True,
                        "artifact": {
                            "markdown_link": "[真的](artifact:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)",
                        },
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        return await sanitize_assistant_artifact_links(content, db, 1, tool_trace=trace)

    out, changed = asyncio.run(_go())
    assert changed
    assert "cccccccccccccccccccccccccccccccc" not in out
    assert "成果物未成功创建" in out
    assert "[真的](artifact:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)" in out


def test_sanitize_keeps_existing():
    async def _go():
        db = _FakeDB({"dddddddddddddddddddddddddddddddd"})
        content = "见 [报告](artifact:dddddddddddddddddddddddddddddddd)"
        return await sanitize_assistant_artifact_links(content, db, 1, tool_trace=None)

    out, changed = asyncio.run(_go())
    assert not changed
    assert out == "见 [报告](artifact:dddddddddddddddddddddddddddddddd)"
