"""chat_utils：TOOL_TRACE 解码与会话详情组装。"""

from __future__ import annotations

import base64
import json

from services.chat_utils import (
    assistant_content_for_chat_detail,
    extract_tool_trace_steps,
    strip_assistant_embedded_sentinels,
)


def _embed_trace(steps: list[dict]) -> str:
    raw = json.dumps({"v": 1, "steps": steps}, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f"<!-- EDGEOPS:TOOL_TRACE:v1 {b64} -->"


def test_extract_tool_trace_steps_decodes_sentinel():
    body = "北京明天多云，25～32℃。\n\n" + _embed_trace(
        [
            {
                "type": "tool",
                "event": "finished",
                "tool": "http_request",
                "action": "completed",
                "args": '{"url":"https://example.com/weather"}',
                "result_preview": '{"temp":30}',
            }
        ]
    )
    steps = extract_tool_trace_steps(body)
    assert len(steps) == 1
    assert steps[0]["tool"] == "http_request"
    assert "weather" in (steps[0].get("args") or "")


def test_assistant_content_for_chat_detail_includes_tool_trace():
    body = "已查询天气。\n\n" + _embed_trace(
        [{"type": "tool", "tool": "web_search", "result_preview": "ok"}]
    )
    detail = assistant_content_for_chat_detail(body, include_tool_results=True)
    assert "已查询天气" in detail["content"]
    assert "EDGEOPS:TOOL_TRACE" not in detail["content"]
    assert detail["tool_trace_step_count"] == 1
    assert detail["tool_trace"][0]["tool"] == "web_search"

    summary = assistant_content_for_chat_detail(body, include_tool_results=False)
    assert summary["tool_trace"] == []
    assert "已查询" in summary["content"]


def test_strip_assistant_embedded_sentinels_removes_trace():
    body = "正文\n\n" + _embed_trace([{"tool": "x"}])
    cleaned = strip_assistant_embedded_sentinels(body)
    assert cleaned.strip() == "正文"
    assert "TOOL_TRACE" not in cleaned
