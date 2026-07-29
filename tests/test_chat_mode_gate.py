"""聊天模式门禁与严格允许缓存、斜杠解析、Hook。"""
import json

from services.chat_mode_gate import (
    build_strict_confirm_body,
    chat_mode_system_section,
    dump_strict_allow_cache,
    is_qa_blocked,
    is_strict_allow_cached,
    needs_strict_confirm,
    normalize_chat_mode,
    parse_strict_allow_cache,
    qa_blocked_tool_result,
    qa_redacted_args_for_ui,
    strict_allow_cache_key,
    strict_allow_cache_key_legacy,
)
from services.chat_mode_runtime import (
    annotate_tool_result_with_strict_decision,
    apply_slash_arg_placeholders,
    build_strict_confirm_ui_action,
    format_slash_skill_injection,
    parse_slash_invocation,
    parse_strict_choice,
    slash_skill_token,
)
from services.chat_mode_runtime import tool_matches


def test_normalize_chat_mode():
    assert normalize_chat_mode("QA") == "qa"
    assert normalize_chat_mode("strict") == "strict"
    assert normalize_chat_mode("") == "normal"
    assert normalize_chat_mode("ask") == "qa"


def test_qa_blocks_mutating_allows_read():
    assert is_qa_blocked("ssh_execute")
    assert is_qa_blocked("send_to_terminal")
    assert is_qa_blocked("ssh_channel_send")
    assert not is_qa_blocked("list_hosts")
    assert not is_qa_blocked("ssh_channel_read_lines")
    assert not is_qa_blocked("get_terminal_buffer")


def test_qa_blocked_result_has_copy_card():
    r = qa_blocked_tool_result("ssh_execute", {"host_id": 1, "command": "uptime"})
    assert r["success"] is False
    assert r["mode"] == "qa"
    assert r["copy_card"] is True
    assert "uptime" in r["suggested_command"]
    assert r["ui_action"]["type"] == "pending_command"
    # 非命令类写操作：不展示 args，不生成复制卡
    r2 = qa_blocked_tool_result("scp_push", {"host_id": 1, "local_path": "/a", "remote_path": "/b"})
    assert r2["copy_card"] is False
    assert r2.get("hide_tool_args") is True
    assert "local_path" not in (r2.get("suggested_command") or "")
    assert "args" not in (r2.get("suggested_command") or "").lower()
    assert "问答模式" in (r2.get("error") or "")


def test_strict_confirm_tools():
    assert needs_strict_confirm("send_to_terminal")
    assert needs_strict_confirm("ssh_execute")
    assert needs_strict_confirm("ssh_channel_send")
    assert needs_strict_confirm("scp_push")
    assert not needs_strict_confirm("list_hosts")
    # 严格模式不拦截创建/打开终端
    assert not needs_strict_confirm("connect_terminal")
    assert not needs_strict_confirm("create_console")
    assert not needs_strict_confirm("create_local_console")
    assert not needs_strict_confirm("ssh_channel_create")


def test_strict_allow_cache_by_tool_name():
    """「总是」= 本会话内同工具函数不再提示（与具体命令无关）。"""
    key = strict_allow_cache_key("send_to_terminal", {"host_id": 3, "text": "ls -la"})
    assert key == "send_to_terminal"
    raw = dump_strict_allow_cache([key])
    assert is_strict_allow_cached(raw, "send_to_terminal", {"host_id": 3, "text": "pwd"})
    assert is_strict_allow_cached(raw, "send_to_terminal", {"host_id": 9, "text": "uptime"})
    assert not is_strict_allow_cached(raw, "ssh_execute", {"host_id": 3, "command": "pwd"})
    keys = parse_strict_allow_cache(raw)
    assert key in keys


def test_strict_allow_cache_legacy_key_promotes_to_tool():
    legacy = strict_allow_cache_key_legacy(
        "send_to_terminal", {"host_id": 3, "text": "ls -la"}
    )
    raw = dump_strict_allow_cache([legacy])
    # 旧精确键仍视为该工具已「总是」
    assert is_strict_allow_cached(raw, "send_to_terminal", {"host_id": 3, "text": "other"})


def test_strict_confirm_body_shows_command_and_reason():
    body = build_strict_confirm_body(
        "ssh_execute",
        {"host_id": 7, "command": "nginx -t"},
        assistant_note="检查配置语法",
    )
    assert "ssh_execute" in body
    assert "nginx -t" in body
    assert "检查配置语法" in body
    ua = build_strict_confirm_ui_action(
        tool_name="ssh_execute",
        args={"host_id": 7, "command": "nginx -t"},
        assistant_note="检查配置语法",
    )
    assert ua["action"] == "strict_command_confirm"
    assert ua["kind"] == "strict_command_confirm"
    assert ua["command"] == "nginx -t"
    assert "检查配置语法" in (ua.get("reason") or "")
    labels = [o["label"] for o in ua["options"]]
    assert labels == ["允许", "总是", "拒绝"]
    assert "nginx -t" in ua["question"]


def test_parse_strict_choice():
    assert parse_strict_choice("[allow] 允许") == "allow"
    assert parse_strict_choice("[always_allow] 总是") == "always_allow"
    assert parse_strict_choice("[always_allow] 一直允许（本会话）") == "always_allow"
    assert parse_strict_choice("[deny] 拒绝") == "deny"


def test_annotate_strict_user_decision():
    denied = json.loads(
        annotate_tool_result_with_strict_decision(
            {"success": False, "error": "x"},
            "deny",
            tool_name="ssh_execute",
        )
    )
    assert denied["user_decision"] == "deny"
    assert denied.get("cancelled_by_user") is True
    assert "未批准" in (denied.get("user_decision_note") or "")
    assert "严格" not in (denied.get("user_decision_note") or "")
    assert denied["success"] is False

    allowed = json.loads(
        annotate_tool_result_with_strict_decision(
            {"success": True, "stdout": "ok"},
            "allow",
            tool_name="ssh_execute",
        )
    )
    assert allowed["user_decision"] == "allow"
    assert allowed["success"] is True
    assert "stdout" in allowed
    assert "批准" in (allowed.get("user_decision_note") or "")
    assert "严格" not in (allowed.get("user_decision_note") or "")

    always = json.loads(
        annotate_tool_result_with_strict_decision(
            {"success": True},
            "always_allow",
            tool_name="send_to_terminal",
        )
    )
    assert always["user_decision"] == "always_allow"
    assert "授权" in (always.get("user_decision_note") or "")
    assert "严格" not in (always.get("user_decision_note") or "")


def test_slash_skill_token():
    assert slash_skill_token("/my-skill please") == "my-skill"
    assert slash_skill_token("hello /my-skill") is None
    assert slash_skill_token("/Bad") is None  # 须小写
    assert slash_skill_token("/bad!") is None


def test_qa_system_section_and_redact():
    sec = chat_mode_system_section("qa")
    assert "问答模式" in sec
    assert "不能" in sec or "禁止" in sec
    assert "get_terminal_buffer" in sec
    assert "send_to_terminal" in sec
    red = qa_redacted_args_for_ui("ssh_execute", {"command": "rm -rf /", "host_id": 9})
    assert red.get("_qa_redacted") is True
    assert "rm -rf" not in json.dumps(red, ensure_ascii=False)
    sec_s = chat_mode_system_section("strict")
    sec_n = chat_mode_system_section("normal")
    # 严格模式不对模型注入额外说明，与普通模式一致
    assert sec_s == sec_n
    assert "严格" not in sec_s
    assert "确认框" not in sec_s


def test_hook_matcher_and_deny():
    assert tool_matches("ssh_*,send_*", "ssh_execute")
    assert not tool_matches("scp_*", "ssh_execute")


def test_parse_slash_invocation_and_placeholders():
    assert slash_skill_token("/my-skill host1") == "my-skill"
    inv = parse_slash_invocation("/my-skill host1 foo\nbar")
    assert inv is not None
    assert inv["name"] == "my-skill"
    assert inv["args_raw"].startswith("host1 foo")
    assert "bar" in inv["args_raw"]
    assert inv["args_list"][0] == "host1"
    text = "Run on {{arg1}} full={{arg}} n=$ARGUMENTS"
    out = apply_slash_arg_placeholders(text, "host1 x", ["host1", "x"])
    assert "Run on host1" in out
    assert "full=host1 x" in out
    assert "n=host1 x" in out
    inj = format_slash_skill_injection(
        {
            "name": "demo",
            "slash": "/demo",
            "body": "Target: {{arg}}",
            "args_raw": "web1",
            "args_list": ["web1"],
            "meta": {"description": "d"},
            "source": "skill",
        }
    )
    assert "Target: web1" in inj
    assert "用户参数" in inj


def test_extract_slash_arg_meta_from_body():
    from services.user_skills_registry import extract_slash_arg_meta

    md = """---
name: aliyun-ops
slash-args:
  - balance
  - ecs list
---

## 斜杠参数
- `oss ls` — 列存储桶
- region — 指定区域

示例：`/aliyun-ops balance`
"""
    meta = extract_slash_arg_meta(md, "aliyun-ops")
    assert "balance" in meta["arg_suggestions"]
    assert any("ecs" in x for x in meta["arg_suggestions"])
    assert meta["usage_examples"]
