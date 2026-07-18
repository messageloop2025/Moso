"""会话级 MCP 熔断：失败后本会话跳过，直到用户要求重试。"""
from services.user_mcp_client import (
    clear_session_mcp_skip,
    is_session_mcp_skipped,
    mark_session_mcp_skip,
    user_requests_mcp_retry,
    _server_known_failed,
)


def test_user_requests_mcp_retry_phrases():
    assert user_requests_mcp_retry("重试 MCP") is True
    assert user_requests_mcp_retry("MCP 恢复了，再连一下") is True
    assert user_requests_mcp_retry("retry mcp please") is True
    assert user_requests_mcp_retry("在吗") is False
    assert user_requests_mcp_retry("列出主机") is False


def test_session_mcp_skip_roundtrip():
    clear_session_mcp_skip(42)
    assert is_session_mcp_skipped(42, 7) is False
    mark_session_mcp_skip(42, 7, reason="connection failed")
    assert is_session_mcp_skipped(42, 7) is True
    assert is_session_mcp_skipped(42, 8) is False
    assert is_session_mcp_skipped(99, 7) is False
    clear_session_mcp_skip(42, server_id=7)
    assert is_session_mcp_skipped(42, 7) is False


def test_server_known_failed():
    assert _server_known_failed({"last_test_ok": 0}) is True
    assert _server_known_failed({"last_test_ok": 1}) is False
    assert _server_known_failed({"last_test_ok": None}) is False
    assert _server_known_failed({}) is False
