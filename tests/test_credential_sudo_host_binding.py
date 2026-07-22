"""本机 sudo 凭证：主机绑定优先，禁止空 address 跨机污染。"""
from services.credential_vault import (
    _credential_bound_to_host,
    _prefer_host_bound_credentials,
    apply_credential_resolution,
)


def test_credential_bound_to_host():
    assert _credential_bound_to_host({"linked_host_id": 5}, 5)
    assert _credential_bound_to_host({"host_id": 5}, 5)
    assert not _credential_bound_to_host({"linked_host_id": 9}, 5)
    assert not _credential_bound_to_host({"address": ""}, 5)


def test_prefer_host_bound_first():
    items = [
        {"id": 1, "linked_host_id": None, "host_id": None, "last_accessed_at": "2026-01-02"},
        {"id": 2, "linked_host_id": 10, "host_id": None, "last_accessed_at": "2026-01-01"},
        {"id": 3, "linked_host_id": None, "host_id": 10, "last_accessed_at": "2026-01-03"},
    ]
    ordered = _prefer_host_bound_credentials(items, 10)
    assert [x["id"] for x in ordered] == [3, 2, 1]


def test_resolution_empty_sudo_suggests_host_login():
    result = apply_credential_resolution(
        {},
        [],
        inferred={"service": "sudo", "host_id": 42, "command_hint": "sudo apt update"},
    )
    assert result["resolution"] == "try_linked_host_or_execute"
    assert result.get("use_host_login") is True
    assert result.get("suggested_linked_host_id") == 42
    assert "use_host_login=true" in (result.get("choice_hint") or "")


def test_resolution_prefers_single_bound_among_many():
    items = [
        {
            "id": 1,
            "service": "sudo",
            "service_username": "alice",
            "address": "",
            "port": None,
            "linked_host_id": None,
            "host_id": None,
            "last_accessed_at": "2026-07-01",
            "updated_at": "2026-07-01",
            "created_at": "2026-07-01",
        },
        {
            "id": 2,
            "service": "sudo",
            "service_username": "bob",
            "address": "",
            "port": None,
            "linked_host_id": 7,
            "host_id": 7,
            "last_accessed_at": "2026-06-01",
            "updated_at": "2026-06-01",
            "created_at": "2026-06-01",
        },
    ]
    result = apply_credential_resolution(
        {},
        items,
        inferred={"service": "sudo", "host_id": 7},
    )
    assert result["resolution"] == "use_credential"
    assert result["suggested_credential_id"] == 2
