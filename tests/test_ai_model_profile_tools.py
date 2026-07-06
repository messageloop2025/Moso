"""AI 模型 Profile 服务与工具辅助逻辑。"""
import pytest

from services.ai_skills import _profile_create_fields_from_tool_args, _profile_patch_from_tool_args


def test_profile_create_fields_defaults():
    fields = _profile_create_fields_from_tool_args({
        "name": "Ollama",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen3.5:27b",
    })
    assert fields["provider"] == "ollama"
    assert fields["model"] == "qwen3.5:27b"
    assert fields["auto_approve"] is False
    assert fields["vision_enabled"] is True


def test_profile_patch_only_explicit_keys():
    patch = _profile_patch_from_tool_args({"model": "gpt-4o", "api_key": "***"})
    assert patch == {"model": "gpt-4o"}
    patch2 = _profile_patch_from_tool_args({"base_url": "http://x/v1/", "set_active": True})
    assert patch2 == {"base_url": "http://x/v1"}


@pytest.mark.asyncio
async def test_create_profile_does_not_auto_activate_second(monkeypatch):
    """第二组配置默认不成为当前激活项（除非 set_active）。"""
    from services import ai_model_profiles as amp

    calls = {"activate": 0}

    async def fake_create(db, user_id, name, fields):
        return {"id": 99, "name": name, **fields}

    async def fake_activate(db, user_id, profile_id):
        calls["activate"] += 1

    async def fake_list(db, user_id):
        return ([{"id": 1, "name": "默认配置", "is_active": True}, {"id": 99, "name": "新模型", "is_active": False}], 1)

    async def fake_get_active(db, user_id):
        return 1

    monkeypatch.setattr(amp, "create_profile", fake_create)
    monkeypatch.setattr(amp, "activate_profile", fake_activate)
    monkeypatch.setattr(amp, "list_profiles", fake_list)
    monkeypatch.setattr(amp, "get_active_profile_id", fake_get_active)
    monkeypatch.setattr(amp, "profile_row_to_tool_config", lambda row: {"id": row["id"], "name": row["name"]})

    from services.ai_skills import execute_tool

    raw = await execute_tool(
        "create_ai_model_profile",
        {"name": "新模型", "model": "m1", "set_active": False},
        {"id": 1, "role": "user", "username": "u1"},
    )
    import json
    data = json.loads(raw)
    assert data["success"] is True
    assert calls["activate"] == 0
    assert "未切换" in data["message"]
