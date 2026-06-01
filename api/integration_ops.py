"""OpenClaw / 第三方集成：纯后台运维 AI 对话（与浏览器 /api/ai/chat 分离）。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from database import get_db
from api.auth import get_current_user
from api.ai_agent import run_ops_integration_chat_complete
from services.host_prompt_search import search_hosts_by_prompt

router = APIRouter(prefix="/api/integration", tags=["OpenClaw/API 集成"])


class OpsChatCompleteRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=200_000,
        description="用户或上游 AI 传入的运维意图/指令",
    )
    session_id: int | None = Field(default=None, description="沿用上一次返回的 session_id 以保持上下文")
    host_id: int | None = Field(
        default=None,
        description="新建会话时可选：绑定到该主机的运维上下文（与会话内 host 范围说明一致）",
    )
    skip_secondary_assistant: bool = Field(
        default=True,
        description="为 true 时不启用「辅助 AI」多轮续跑，适合 OpenClaw 等外部编排（默认 true）",
    )
    attachment_uuids: list[str] = Field(
        default_factory=list,
        description="可选：本轮用户已上传的附件 UUID 列表（通过 POST /api/ai/attachments 得到）。系统会把附件清单追加到用户消息末尾，AI 可用 read_chat_attachment(uuid) 读取。",
    )
    ui_locale: str | None = Field(
        default=None,
        description="可选：客户端界面语言（BCP-47 / I18n），用于无法从用户输入判断回复语言时回退到界面/浏览器语言",
    )


@router.post("/ops-chat/complete")
async def ops_chat_complete(req: OpsChatCompleteRequest, user=Depends(get_current_user)):
    """
    非流式、JSON 返回。使用 session_scope=integration 的会话，不进入网页 AI 助手列表；
    主机维度的会话列表也会排除 integration 会话。
    """
    db = await get_db()
    return await run_ops_integration_chat_complete(
        db,
        user,
        req.message,
        req.session_id,
        req.host_id,
        skip_secondary_assistant=req.skip_secondary_assistant,
        attachment_uuids=list(req.attachment_uuids or []),
        ui_locale=req.ui_locale,
    )


@router.get("/hosts/search-by-prompt")
async def integration_search_hosts_by_prompt(
    query: str = Query(..., min_length=1, description="在主机级提示词中搜索的关键字"),
    group_id: Optional[int] = Query(None, description="可选，限定分组"),
    tag_ids: Optional[list[int]] = Query(None, description="可选，限定标签 ID（命中任一）"),
    regex: str = Query("", description="可选，在提示词全文上正则精筛"),
    case_sensitive: bool = Query(False, description="regex / 片段匹配是否区分大小写"),
    limit: int = Query(30, ge=1, le=100, description="最多返回条数"),
    snippet_chars: int = Query(200, ge=50, le=600, description="每条命中片段长度"),
    user=Depends(get_current_user),
):
    """OpenClaw / 集成侧：按主机级 AI 提示词搜索主机（与 AI 工具 search_hosts_by_prompt 等价）。"""
    db = await get_db()
    try:
        return await search_hosts_by_prompt(
            db,
            user,
            query=query,
            group_id=group_id,
            tag_ids=list(tag_ids or []),
            regex=regex,
            case_sensitive=case_sensitive,
            limit=limit,
            snippet_chars=snippet_chars,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/spill/read")
async def integration_read_spill(
    spill_id: str = Query(..., min_length=1, description="spill UUID（通道 read/dump 返回的 spill_id）"),
    date_subdir: str = Query(..., min_length=1, description="UTC 日期子目录，如 2026/05/22"),
    mode: str = Query("head_tail", description="head | tail | head_tail | range"),
    session_id: Optional[int] = Query(None, description="可选，与 spill 元数据校验"),
    head_chars: int = Query(8000, ge=0, le=500_000),
    tail_chars: int = Query(8000, ge=0, le=500_000),
    range_start: int = Query(0, ge=0),
    max_chars: int = Query(16000, ge=256, le=500_000),
    user=Depends(get_current_user),
):
    """OpenClaw / 集成侧：分段读取工具 spill 落盘文件（与 AI 工具 read_chat_data 等价）。"""
    from services.chat_tool_spill import read_chat_data_slice_async

    out = await read_chat_data_slice_async(
        user,
        session_id,
        spill_id.strip(),
        date_subdir.strip(),
        mode,
        head_chars=head_chars,
        tail_chars=tail_chars,
        range_start=range_start,
        max_chars=max_chars,
    )
    if not out.get("success"):
        raise HTTPException(status_code=404, detail=out.get("error") or "读取失败")
    return out
