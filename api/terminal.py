"""SSH 终端 WebSocket 与 AI 介入 API（xterm.js 前端 + 后端桥接）。

卡顿/白闪/断连说明：
- SSH 长时间无数据时，中间网络或服务端可能断开连接，后端已对 SSH 开启 keepalive（约 30 秒）以减轻空闲断连。
- 终端输出量很大时，浏览器主线程会被 xterm 渲染占满，表现为界面卡住、白闪；前端已对 term.write 做 requestAnimationFrame 节流，每帧合并输出以减轻卡顿；ResizeObserver/窗口 resize 对 fit 做了短防抖，减少分栏拖拽时的连续 resize 与闪白。
- WebSocket 或 SSH 任一侧断开后，终端会显示未连接，需用户重新点击「连接」。"""
import asyncio
import json
import logging
import re
import socket
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from database import get_db
from api.auth import get_current_user, user_dict_for_websocket_from_token
from api.hosts import _resolve_host_auth, parse_host_aliases_cell
from services.ssh_shell import open_shell_session
from services.ssh_connect import friendly_ssh_error
from services.terminal_input import expand_control_keys, is_control_only
import paramiko

logger = logging.getLogger("edgeops.terminal")

router = APIRouter(prefix="/api/terminal", tags=["SSH 终端"])

DEFAULT_TERMINAL_SCOPE = "default"

# 终端会话：(user_id, scope_id, slot) -> { channel, client, buffer, host_id, host_type, host_name, host_ip, host_port, host_aliases, created_by, ws, task }；slot 0 为默认控制台
_user_sessions: dict[tuple[int, str, int], dict] = {}
# AI 请求创建控制台：(user_id, scope_id) -> [ { "host_id", "created_by": "ai" }, ... ]，前端轮询取走后清空
_pending_console_creations: dict[tuple[int, str], list[dict]] = {}
BUFFER_MAX = 262144
TERMINAL_SLOT_AI = 0  # 默认控制台槽位（不可关闭）
# connect_terminal 后前端建立 WebSocket+SSH 需时间；AI 连续 tool 调用时服务端在 send/read 前等待就绪
TERMINAL_CONNECT_WAIT_MAX_SEC = 12.0
TERMINAL_CONNECT_POLL_SEC = 0.25


def normalize_terminal_scope_id(scope_id: Optional[str]) -> str:
    scope = (scope_id or "").strip()
    if not scope:
        return DEFAULT_TERMINAL_SCOPE
    if len(scope) > 120:
        scope = scope[:120]
    return scope


@router.websocket("/ws")
async def terminal_websocket(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token") or ws.query_params.get("Authorization", "").replace("Bearer ", "")
    user = await user_dict_for_websocket_from_token(token)
    if not user:
        await ws.send_json({"type": "error", "message": "未登录或 Token 无效"})
        await ws.close()
        return

    user_id = user["id"]
    slot = 0
    scope_id = DEFAULT_TERMINAL_SCOPE
    session: Optional[dict] = None

    try:
        # 第一帧必须是 init
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send_json({"type": "error", "message": "首帧须为 JSON: { type, host_id }"})
            return
        if msg.get("type") != "init" or "host_id" not in msg:
            await ws.send_json({"type": "error", "message": "需要 type: init 与 host_id"})
            return

        if isinstance(msg.get("slot"), (int, float)):
            slot = int(msg["slot"])
        elif isinstance(msg.get("slot"), str) and msg["slot"].isdigit():
            slot = int(msg["slot"])
        else:
            slot = 0
        slot = max(0, min(slot, 31))
        scope_id = normalize_terminal_scope_id(msg.get("scope_id"))
        created_by = (msg.get("created_by") or "user").strip().lower()
        if created_by not in ("default", "user", "ai"):
            created_by = "user"
        if slot == 0 and created_by != "ai":
            created_by = "default"
        host_id = int(msg["host_id"])
        db = await get_db()
        rows = await db.execute_fetchall("SELECT * FROM hosts WHERE id = ?", (host_id,))
        if not rows:
            await ws.send_json({"type": "error", "message": "主机不存在"})
            return
        host_row = dict(rows[0])
        auth = await _resolve_host_auth(db, host_row)
        if not auth or not auth.get("username"):
            await ws.send_json({"type": "error", "message": "主机未配置有效登录凭证"})
            return

        # 同一用户同 slot 已有会话时先关闭，避免旧连接占用导致新连接异常
        session_key = (user_id, scope_id, slot)
        old = _user_sessions.pop(session_key, None)
        if old:
            old.get("task") and old["task"].cancel()
            try:
                old.get("channel") and old["channel"].close()
            except Exception:
                pass
            try:
                old.get("client") and old["client"].close()
            except Exception:
                pass

        # SSH 建连放入线程池，避免阻塞事件循环（否则长时间/卡住会拖死整个 worker）
        try:
            client, channel = await asyncio.to_thread(
                open_shell_session,
                host=host_row["host"],
                port=host_row.get("port") or 22,
                username=auth["username"],
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                timeout=45,
            )
        except Exception as e:
            # 认证失败/网络问题属于常见用户错误，不需要堆栈刷屏；未知异常仍保留堆栈以便排查
            if isinstance(
                e,
                (
                    paramiko.ssh_exception.AuthenticationException,
                    paramiko.ssh_exception.BadAuthenticationType,
                    paramiko.ssh_exception.SSHException,
                    socket.timeout,
                    TimeoutError,
                    EOFError,
                    ConnectionRefusedError,
                    OSError,
                ),
            ):
                logger.warning("SSH shell open failed: %s", e)
            else:
                logger.exception("SSH shell open failed")
            await ws.send_json({"type": "error", "message": friendly_ssh_error(e)})
            return

        buffer: list = []
        buffer_size = [0]  # 用 list 以便闭包内修改

        async def channel_to_ws():
            try:
                while True:
                    data = await asyncio.to_thread(channel.recv, 4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    buffer.append(text)
                    buffer_size[0] += len(text)
                    while buffer_size[0] > BUFFER_MAX and buffer:
                        first = buffer.pop(0)
                        buffer_size[0] -= len(first)
                    await ws.send_text(text)
            except (WebSocketDisconnect, ConnectionError, RuntimeError):
                pass
            except Exception as e:
                logger.exception("channel_to_ws: %s", e)

        task = asyncio.create_task(channel_to_ws())
        session = {
            "scope_id": scope_id,
            "host_id": host_id,
            "host_type": (host_row.get("host_type") or "").strip(),
            "host_name": (host_row.get("name") or "").strip(),
            "host_ip": (host_row.get("host") or "").strip(),
            "host_port": int(host_row.get("port") or 22),
            "host_aliases": parse_host_aliases_cell(host_row.get("aliases")),
            "created_by": created_by,
            "channel": channel,
            "client": client,
            "buffer": buffer,
            "buffer_size": buffer_size,
            "ws": ws,
            "task": task,
        }
        session_key = (user_id, scope_id, slot)
        _user_sessions[session_key] = session
        await ws.send_json({"type": "ready", "slot": slot, "scope_id": scope_id})

        # 之后所有来自客户端的为终端输入或 resize（xterm 原始字符或 JSON { type: "input", data } / { type: "resize", cols, rows }）
        while True:
            raw = await ws.receive_text()
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    if obj.get("type") == "input" and "data" in obj:
                        raw = obj["data"]
                    elif obj.get("type") == "resize" and session.get("channel") and not channel.exit_status_ready():
                        try:
                            c = int(obj.get("cols") or 0)
                            r = int(obj.get("rows") or 0)
                        except (TypeError, ValueError):
                            c, r = 0, 0
                        if c >= 2 and r >= 1:
                            c, r = min(c, 1000), min(r, 500)
                            try:
                                channel.resize_pty(width=c, height=r)
                            except Exception:
                                pass
                        continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            if session.get("channel") and not channel.exit_status_ready():
                channel.send(raw.encode("utf-8", errors="replace"))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("terminal ws: %s", e)
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        session_key = (user_id, scope_id, slot)
        if session_key in _user_sessions:
            s = _user_sessions.pop(session_key, None)
            if s:
                s["task"].cancel()
                try:
                    await s["task"]
                except asyncio.CancelledError:
                    pass
                try:
                    s["channel"].close()
                except Exception:
                    pass
                try:
                    s["client"].close()
                except Exception:
                    pass


def is_terminal_session_ready(user_id: int, slot: int | None = None, scope_id: str | None = None) -> bool:
    """前端 WebSocket 已完成 init 且 SSH 会话已登记、通道可用。"""
    if slot is None:
        slot = TERMINAL_SLOT_AI
    scope_id = normalize_terminal_scope_id(scope_id)
    session = _user_sessions.get((user_id, scope_id, slot))
    if not session:
        return False
    ch = session.get("channel")
    return bool(ch and not ch.exit_status_ready())


async def wait_for_terminal_session_ready(
    user_id: int,
    slot: int | None = None,
    scope_id: str | None = None,
    *,
    max_wait_sec: float = TERMINAL_CONNECT_WAIT_MAX_SEC,
    poll_interval_sec: float = TERMINAL_CONNECT_POLL_SEC,
) -> bool:
    """在 connect_terminal / create_console 之后轮询，直到会话就绪或超时（避免紧接的读写失败）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.5, min(max_wait_sec, 30.0))
    while loop.time() < deadline:
        if is_terminal_session_ready(user_id, slot, scope_id):
            return True
        await asyncio.sleep(poll_interval_sec)
    return False


def format_terminal_tab_label(item: dict) -> str:
    """与前端 Tab 一致：host_id-host_name-slot (AI|用户)。"""
    hid = item.get("host_id")
    hid_s = str(hid) if hid is not None else "?"
    name = (item.get("host_name") or item.get("host_ip") or "?").strip() or "?"
    slot = item.get("slot", 0)
    who = "AI" if (item.get("created_by") or "") == "ai" else "用户"
    return f"{hid_s}-{name}-{slot} ({who})"


def _enrich_terminal_item(item: dict) -> dict:
    out = dict(item)
    out["tab_label"] = format_terminal_tab_label(item)
    return out


def find_ai_terminal_for_host(user_id: int, host_id: int, scope_id: str | None = None) -> dict | None:
    """查找指定 host 上 AI 已创建的控制台；优先返回已连接项。"""
    try:
        host_id = int(host_id)
    except (TypeError, ValueError):
        return None
    scope_id = normalize_terminal_scope_id(scope_id)
    matches = [
        _enrich_terminal_item(it)
        for it in get_terminals_for_user(user_id, scope_id)
        if (it.get("created_by") or "") == "ai" and it.get("host_id") == host_id
    ]
    if not matches:
        return None
    connected = [it for it in matches if it.get("connected")]
    pool = connected or matches
    return min(pool, key=lambda x: int(x.get("slot") or 0))


def list_ai_terminals_for_host(user_id: int, host_id: int, scope_id: str | None = None) -> list[dict]:
    """列出指定 host 上全部 AI 控制台（含 idle 标记）。"""
    try:
        host_id = int(host_id)
    except (TypeError, ValueError):
        return []
    scope_id = normalize_terminal_scope_id(scope_id)
    out: list[dict] = []
    for it in get_terminals_for_user(user_id, scope_id):
        if (it.get("created_by") or "") != "ai" or it.get("host_id") != host_id:
            continue
        enriched = _enrich_terminal_item(dict(it))
        slot = int(enriched.get("slot") or 0)
        if enriched.get("connected"):
            enriched["buffer_idle"] = terminal_buffer_looks_idle(user_id, slot, scope_id)
        else:
            enriched["buffer_idle"] = None
        out.append(enriched)
    out.sort(key=lambda x: int(x.get("slot") or 0))
    return out


def terminal_buffer_looks_idle(user_id: int, slot: int, scope_id: str | None = None) -> bool:
    """buffer 末尾是否像已回到 shell 提示符（可安全注入新命令）。"""
    buf, connected = get_terminal_buffer_for_user(user_id, slot, scope_id)
    if not connected:
        return False
    tail = (buf or "")[-800:].rstrip()
    if not tail:
        return True
    last_line = tail.splitlines()[-1].strip()
    if not last_line:
        return True
    if re.search(r"[\$#]\s*$", last_line):
        return True
    if re.search(r">\s*$", last_line):
        return True
    if re.search(r"\]\s*[\$#]\s*$", last_line):
        return True
    return False


def find_preferred_ai_terminal_for_host(
    user_id: int,
    host_id: int,
    scope_id: str | None = None,
    *,
    prefer_idle: bool = True,
) -> dict | None:
    """优先返回 buffer 空闲的 AI 控制台；无空闲则返回已连接/任意一项。"""
    matches = list_ai_terminals_for_host(user_id, host_id, scope_id)
    if not matches:
        return None
    if prefer_idle:
        idle = [it for it in matches if it.get("connected") and it.get("buffer_idle")]
        if idle:
            return idle[0]
    connected = [it for it in matches if it.get("connected")]
    pool = connected or matches
    return pool[0]


def terminals_snapshot_for_ai(
    user_id: int,
    scope_id: str | None = None,
    preferred_slot: int | None = None,
) -> dict:
    """供 system prompt 与 list_terminals 工具共用的终端快照。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    ai_items: list[dict] = []
    user_items: list[dict] = []
    for it in get_terminals_for_user(user_id, scope_id):
        enriched = _enrich_terminal_item(it)
        if (it.get("created_by") or "") == "ai":
            slot_i = int(enriched.get("slot") or 0)
            if enriched.get("connected"):
                enriched["buffer_idle"] = terminal_buffer_looks_idle(user_id, slot_i, scope_id)
            else:
                enriched["buffer_idle"] = None
            ai_items.append(enriched)
        else:
            user_items.append(enriched)
    return {
        "scope_id": scope_id,
        "preferred_slot": preferred_slot,
        "ai_terminals": ai_items,
        "user_terminals": user_items,
        "ai_operable_count": len(ai_items),
    }


def format_terminals_mapping_for_prompt(
    user_id: int,
    scope_id: str | None = None,
    preferred_slot: int | None = None,
) -> str:
    """生成注入 system prompt 的可读终端映射（含状态与选用规则）。"""
    snap = terminals_snapshot_for_ai(user_id, scope_id, preferred_slot)
    lines = [
        f"terminal_scope_id={snap['scope_id']}（list_terminals / send_to_terminal / get_terminal_buffer 均在此 scope）",
    ]
    if preferred_slot is not None:
        lines.append(
            f"界面当前活动 slot={preferred_slot}（若为 AI 控制台则优先使用；用户控制台仅作只读参考）"
        )
    ai = snap["ai_terminals"]
    if not ai:
        lines.append(
            "【AI 可操作控制台】当前无。需要时 connect_terminal(host_id) 或 create_console(host_id)；"
            "若界面已有 AI 标签页但此处仍为空，先 list_terminals 确认，或等待 WebSocket 就绪（读写前最多约 12 秒）。"
        )
    else:
        lines.append("【AI 可操作控制台】send_to_terminal / get_terminal_buffer / close_console 仅能使用下列 slot：")
        for t in ai:
            st = "已连接" if t.get("connected") else "未连接/握手中"
            idle_note = ""
            if t.get("connected"):
                slot_i = int(t.get("slot") or 0)
                idle = terminal_buffer_looks_idle(user_id, slot_i, scope_id)
                idle_note = " buffer_idle=是(可发新命令)" if idle else " buffer_idle=否(可能仍有程序占用)"
            lines.append(
                f"  slot={t['slot']} tab={t['tab_label']} host_id={t.get('host_id')} "
                f"ip={t.get('host_ip')}:{t.get('host_port')} status={st}{idle_note}"
            )
    user = snap["user_terminals"]
    if user:
        lines.append("【用户控制台】AI 不可操作；同一 host 已有用户控制台时勿重复 create_console：")
        for t in user:
            st = "已连接" if t.get("connected") else "未连接"
            lines.append(f"  slot={t['slot']} tab={t['tab_label']} host_id={t.get('host_id')} status={st}")
    lines.append(
        "规则：① 操作前先 list_terminals（同一 host 可有多个 AI slot，看 buffer_idle）；"
        "② 有空闲 slot（buffer_idle=是）时优先复用，勿无故再开；"
        "③ 现有终端被长期任务占用、或要在并行 session 里执行新任务时，调用 create_console(host_id) 新开终端（**同一 host 允许多个 AI 控制台**）；"
        "④ 用户明确要求「再开一个终端/新开控制台」时，必须 create_console(host_id)，不得拒绝；"
        "⑤ connect_terminal 仅在尚无该 host 的 AI 控制台、或只需切到已有空闲 slot 时使用；"
        "⑥ send_to_terminal/get_terminal_buffer 失败时先看返回里的 terminals 快照，勿立刻 ssh_execute 替代。"
    )
    return "\n".join(lines)


def resolve_ai_slot(
    user_id: int,
    scope_id: str | None = None,
    requested_slot: int | None = None,
    host_id_hint: int | None = None,
    default_terminal_slot: int | None = None,
) -> tuple[int | None, str | None]:
    """解析 AI 可操作的 Web 控制台 slot（与 execute_tool 内逻辑一致）。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    items = [
        _enrich_terminal_item(it)
        for it in get_terminals_for_user(user_id, scope_id)
        if (it.get("created_by") or "") == "ai"
    ]
    ai_slots = {int(it["slot"]): it for it in items if it.get("slot") is not None}
    if host_id_hint is not None:
        try:
            match = find_preferred_ai_terminal_for_host(
                user_id, int(host_id_hint), scope_id, prefer_idle=True
            )
            if match and match.get("slot") is not None:
                slot_id = int(match["slot"])
                if requested_slot is None or requested_slot == slot_id:
                    return slot_id, None
        except (TypeError, ValueError):
            pass
    if not items:
        snap = terminals_snapshot_for_ai(user_id, scope_id, default_terminal_slot)
        hint = ""
        if snap["user_terminals"]:
            hint = "（界面有用户控制台但 AI 不可操作，请 connect_terminal 创建 AI 控制台）"
        return None, f"当前 scope 内没有 AI 创建的 SSH 控制台，请先 list_terminals 或 connect_terminal(host_id){hint}"
    if requested_slot is not None:
        try:
            requested_slot = int(requested_slot)
        except (TypeError, ValueError):
            return None, "slot 须为整数"
        if requested_slot not in ai_slots:
            labels = ", ".join(
                f"slot={s}({format_terminal_tab_label(ai_slots[s])})" for s in sorted(ai_slots.keys())
            )
            return None, f"slot {requested_slot} 不是 AI 控制台或不存在。可用：{labels}"
        return requested_slot, None
    if default_terminal_slot is not None and default_terminal_slot in ai_slots:
        return default_terminal_slot, None
    connected = sorted(s for s, it in ai_slots.items() if it.get("connected"))
    if connected:
        return connected[0], None
    return min(ai_slots.keys()), None


def get_terminal_buffer_for_user(user_id: int, slot: int | None = None, scope_id: str | None = None) -> tuple[str, bool]:
    """供 Agent 内部调用：返回指定控制台的 (buffer 文本, 是否已连接)。slot 为空则用默认 (0)。"""
    if slot is None:
        slot = TERMINAL_SLOT_AI
    scope_id = normalize_terminal_scope_id(scope_id)
    session = _user_sessions.get((user_id, scope_id, slot))
    if not session:
        return "", False
    ch = session.get("channel")
    connected = ch is not None and not ch.exit_status_ready()
    return "".join(session["buffer"]), connected


def get_current_host_id_for_user(user_id: int, scope_id: str | None = None, slot: int | None = None) -> int | None:
    """供 Agent 内部调用：返回指定控制台（默认 0）所连接的主机 ID。"""
    meta = get_terminal_session_meta_for_user(user_id, slot=slot, scope_id=scope_id)
    if not meta:
        return None
    return meta.get("host_id")


def get_terminal_session_meta_for_user(
    user_id: int, slot: int | None = None, scope_id: str | None = None
) -> dict | None:
    """供 Agent / UI 调用：返回指定控制台 slot 与主机映射等元数据。"""
    if slot is None:
        slot = TERMINAL_SLOT_AI
    else:
        slot = max(0, min(int(slot), 31))
    scope_id = normalize_terminal_scope_id(scope_id)
    session = _user_sessions.get((user_id, scope_id, slot))
    if not session:
        return None
    ch = session.get("channel")
    return {
        "slot": slot,
        "scope_id": scope_id,
        "host_id": session.get("host_id"),
        "host_name": session.get("host_name") or "",
        "host_ip": session.get("host_ip") or "",
        "host_port": session.get("host_port") or 22,
        "host_aliases": session.get("host_aliases") or [],
        "host_type": session.get("host_type") or "",
        "created_by": session.get("created_by") or "user",
        "connected": ch is not None and not ch.exit_status_ready(),
    }


def get_terminals_for_user(user_id: int, scope_id: str | None = None) -> list[dict]:
    """供 Agent 调用：返回当前用户所有控制台列表（含终端与主机映射扩展信息）。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    out = []
    for (uid, session_scope_id, slot), s in list(_user_sessions.items()):
        if uid != user_id or session_scope_id != scope_id:
            continue
        ch = s.get("channel")
        out.append({
            "slot": slot,
            "scope_id": session_scope_id,
            "host_id": s.get("host_id"),
            "host_name": s.get("host_name") or "",
            "host_ip": s.get("host_ip") or "",
            "host_port": s.get("host_port") or 22,
            "host_aliases": s.get("host_aliases") or [],
            "created_by": s.get("created_by") or "user",
            "connected": ch is not None and not ch.exit_status_ready(),
        })
    out.sort(key=lambda x: x["slot"])
    return out


def next_terminal_slot(user_id: int, scope_id: str | None = None) -> int:
    """为 AI/前端创建 SSH 控制台预分配一个当前 scope 内未占用的 slot（含 pending 队列）。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    used: set[int] = set()
    for (uid, session_scope_id, slot), _ in list(_user_sessions.items()):
        if uid != user_id or session_scope_id != scope_id:
            continue
        used.add(slot)
    key = (user_id, scope_id)
    for item in _pending_console_creations.get(key, []):
        try:
            used.add(max(0, min(int(item.get("slot") or 0), 31)))
        except (TypeError, ValueError):
            pass
    for slot in range(32):
        if slot not in used:
            return slot
    return 31


def add_pending_console_creation(
    user_id: int,
    host_id: int,
    created_by: str = "ai",
    scope_id: str | None = None,
    slot: int | None = None,
) -> int:
    """AI 请求创建控制台时调用；前端轮询 GET 取走后会创建对应 tab 并连接。返回预分配的 slot。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    if slot is None:
        slot = next_terminal_slot(user_id, scope_id)
    else:
        slot = max(0, min(int(slot), 31))
    key = (user_id, scope_id)
    if key not in _pending_console_creations:
        _pending_console_creations[key] = []
    _pending_console_creations[key].append(
        {"host_id": host_id, "created_by": created_by, "scope_id": scope_id, "slot": slot}
    )
    return slot


async def close_console(user_id: int, slot: int, scope_id: str | None = None) -> tuple[bool, str]:
    """关闭指定 slot 的控制台。仅当 created_by 为 ai 时可关闭（无默认控制台后 slot 0 也可关闭）。返回 (成功, 消息)。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    session = _user_sessions.get((user_id, scope_id, slot))
    if not session:
        return False, "该控制台不存在或已关闭"
    if (session.get("created_by") or "user") != "ai":
        return False, "仅可关闭由 AI 创建的控制台"
    task = session.get("task")
    if task:
        task.cancel()
    try:
        session.get("channel") and session["channel"].close()
    except Exception:
        pass
    try:
        session.get("client") and session["client"].close()
    except Exception:
        pass
    try:
        ws = session.get("ws")
        if ws:
            await ws.close()
    except Exception:
        pass
    _user_sessions.pop((user_id, scope_id, slot), None)
    return True, "已关闭"


def _terminal_line_ending(text: str, host_type: str) -> str:
    """Windows 需 \\r\\n 提交命令，Linux 用 \\n。"""
    if not text:
        return text
    ht = (host_type or "").lower()
    if "windows" in ht:
        text = text.rstrip("\r\n")
        return text + "\r\n"
    if not text.endswith("\n"):
        return text + "\n"
    return text


def send_to_user_terminal(user_id: int, text: str, slot: int | None = None, scope_id: str | None = None) -> bool:
    """供 Agent 内部调用：向指定控制台注入输入；slot 为空则使用默认 (0)。支持 <Ctrl+C> 等控制键占位符。"""
    if slot is None:
        slot = TERMINAL_SLOT_AI
    scope_id = normalize_terminal_scope_id(scope_id)
    session = _user_sessions.get((user_id, scope_id, slot))
    if not session:
        return False
    ch = session["channel"]
    if ch.exit_status_ready():
        return False
    text = expand_control_keys(text or "")
    if not is_control_only(text):
        text = _terminal_line_ending(text, session.get("host_type"))
    ch.send(text.encode("utf-8", errors="replace"))
    return True


@router.get("/buffer")
async def get_terminal_buffer(slot: int | None = None, scope_id: str | None = None, user=Depends(get_current_user)):
    """读取指定控制台最近输出；slot 不传则默认 0。"""
    slot = max(0, min(slot if slot is not None else TERMINAL_SLOT_AI, 31))
    scope_id = normalize_terminal_scope_id(scope_id)
    buf, connected = get_terminal_buffer_for_user(user["id"], slot, scope_id=scope_id)
    return {"success": True, "buffer": buf, "connected": connected, "slot": slot, "scope_id": scope_id}


@router.get("/list")
async def list_terminals(scope_id: str | None = None, user=Depends(get_current_user)):
    """返回当前用户所有控制台列表（slot、host_id、host_name、host_aliases、host_ip、host_port、created_by、connected）。"""
    scope_id = normalize_terminal_scope_id(scope_id)
    items = get_terminals_for_user(user["id"], scope_id=scope_id)
    return {"success": True, "terminals": items, "scope_id": scope_id}


@router.get("/pending-console-creations")
async def get_pending_console_creations(scope_id: str | None = None, user=Depends(get_current_user)):
    """前端轮询：获取待创建的控制台（AI 请求创建的），取走后清空。"""
    uid = user["id"]
    scope_id = normalize_terminal_scope_id(scope_id)
    items = _pending_console_creations.pop((uid, scope_id), [])
    return {"success": True, "items": items, "scope_id": scope_id}


class TerminalSendBody(BaseModel):
    text: str
    slot: int | None = None  # 不传则发往默认控制台 (0)
    scope_id: str | None = None


@router.post("/send")
async def terminal_send(body: TerminalSendBody, user=Depends(get_current_user)):
    """向指定控制台注入输入；body.slot 不传则发往默认控制台。支持 <Ctrl+C> 等控制键占位符。"""
    slot = body.slot if body.slot is not None else TERMINAL_SLOT_AI
    scope_id = normalize_terminal_scope_id(body.scope_id)
    session = _user_sessions.get((user["id"], scope_id, slot))
    if not session:
        raise HTTPException(status_code=400, detail="该控制台未连接或已关闭")
    channel = session["channel"]
    if channel.exit_status_ready():
        raise HTTPException(status_code=400, detail="终端已关闭")
    text = expand_control_keys(body.text or "")
    if not is_control_only(text):
        text = _terminal_line_ending(text, session.get("host_type"))
    channel.send(text.encode("utf-8", errors="replace"))
    return {"success": True, "scope_id": scope_id}
