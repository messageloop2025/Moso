"""子 AI 批量并发与只读工具集。"""
import asyncio

import pytest

from services.sub_ai import (
    DEFAULT_READONLY_TOOLS,
    HARD_MAX_BATCH,
    run_sub_ai_batch,
)


def test_default_readonly_includes_spill_tools():
    assert "read_chat_data" in DEFAULT_READONLY_TOOLS
    assert "fs_read_file" in DEFAULT_READONLY_TOOLS
    assert "get_session_chat_detail" in DEFAULT_READONLY_TOOLS


@pytest.mark.asyncio
async def test_run_sub_ai_batch_rejects_empty():
    out = await run_sub_ai_batch(user={"id": 1}, scope="default", tasks=[])
    assert out["success"] is False
    assert "不能为空" in out["error"]


@pytest.mark.asyncio
async def test_run_sub_ai_batch_rejects_too_many():
    tasks = [{"task": f"t{i}", "system_prompt": "x"} for i in range(HARD_MAX_BATCH + 1)]
    out = await run_sub_ai_batch(user={"id": 1}, scope="default", tasks=tasks)
    assert out["success"] is False
    assert str(HARD_MAX_BATCH) in out["error"]


@pytest.mark.asyncio
async def test_run_sub_ai_batch_missing_prompt_per_task():
    out = await run_sub_ai_batch(
        user={"id": 1},
        scope="default",
        tasks=[{"task": "analyze", "name": "a"}],
    )
    assert out["success"] is False
    assert out["failed"] == 1
    assert out["results"][0]["success"] is False
