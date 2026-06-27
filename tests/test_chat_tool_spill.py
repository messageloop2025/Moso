import json

from services.chat_tool_spill import (
    build_force_read_spill_user_message,
    format_tool_message_with_spill,
    list_unresolved_spill_refs,
    parse_spill_sentinel_fields,
)


def test_format_tool_message_without_preview_by_default(monkeypatch):
    monkeypatch.setattr("services.chat_tool_spill.CHAT_TOOL_SPILL_INCLUDE_PREVIEW", False)
    spill = {
        "spill_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "storage_subdir": "2026/06/27",
        "char_length": 3424,
        "tool_name": "user_mcp_3__list_devices",
        "session_id": 1,
        "relative_path": "chats/2026/06/27/spill/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.data",
    }
    out = format_tool_message_with_spill(spill, '{"devices":[{"id":1}]}')
    assert "[[EDGEOPS_CHAT_DATA ref=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in out
    assert "预览已省略" in out
    assert '{"devices"' not in out


def test_list_unresolved_spill_refs_until_read_chat_data():
    sentinel = (
        "[[EDGEOPS_CHAT_DATA ref=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "
        "subdir=2026/06/27 chars=3424 tool=list_devices session=1]]\n"
        "more"
    )
    messages = [
        {"role": "user", "content": "列出设备"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1",
            "function": {"name": "user_mcp_3__list_devices", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": sentinel},
    ]
    unresolved = list_unresolved_spill_refs(messages)
    assert len(unresolved) == 1
    assert unresolved[0]["ref"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c2",
            "function": {
                "name": "read_chat_data",
                "arguments": json.dumps({
                    "spill_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "date_subdir": "2026/06/27",
                    "mode": "head",
                }),
            },
        }],
    })
    assert list_unresolved_spill_refs(messages) == []


def test_parse_spill_sentinel_fields():
    line = (
        "[[EDGEOPS_CHAT_DATA ref=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "
        "subdir=2026/06/27 chars=3424 tool=list_devices session=1]]"
    )
    fields = parse_spill_sentinel_fields(line)
    assert fields is not None
    assert fields["ref"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert fields["subdir"] == "2026/06/27"


def test_build_force_read_spill_user_message_mentions_read():
    msg = build_force_read_spill_user_message([{
        "ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "subdir": "2026/06/27",
        "chars": "3424",
        "tool": "list_devices",
    }])
    assert "read_chat_data" in msg
    assert "禁止" in msg
