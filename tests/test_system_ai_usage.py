"""system_ai_usage_limit 读取与校验。"""
from services.system_ai_usage import (
    default_system_ai_usage_limit,
    parse_system_ai_usage_limit_value,
)


def test_parse_valid():
    ok, n = parse_system_ai_usage_limit_value("2000")
    assert ok and n == 2000


def test_parse_rejects_negative():
    ok, msg = parse_system_ai_usage_limit_value("-1")
    assert not ok and isinstance(msg, str)


def test_default_from_config():
    assert default_system_ai_usage_limit() >= 0
