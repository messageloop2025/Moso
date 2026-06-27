import json

from api.ai_agent import _compact_tool_result_for_messages


def test_read_chat_data_skips_json_content_compaction():
    content = "设备" * 2000
    payload = json.dumps({"success": True, "content": content}, ensure_ascii=False)
    out = _compact_tool_result_for_messages("read_chat_data", payload, 6000)
    assert content in out
    assert "已截断" not in out
