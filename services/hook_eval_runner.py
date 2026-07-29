"""Hook 条件判断执行器：运行 Python 脚本做前置检查，根据返回值动态决策。

脚本接口：
- stdin: JSON 上下文 → {event, tool_name, tool_args, chat_mode, user_id}
- stdout: JSON 决策 → {decision: "allow"|"deny"|"ask", reason: str}
- 退出码 0 + stdout 解析成功 → 采用决策
- 超时/异常/解析失败 → fail_open（默认 allow，可配为 deny）
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("edgeops.hook_eval_runner")

_EVAL_DEFAULT_TIMEOUT_S = 5
_EVAL_MAX_STDOUT_BYTES = 64 * 1024


def resolve_eval_script_path(skill_dir: str | Path, script_name: str) -> Path | None:
    """在 skill 目录下查找 eval 脚本。

    搜索顺序：
    1) skill_dir/hooks_checks/{script_name}
    2) skill_dir/{script_name}

    仅 .py 文件；拒绝路径穿越（".."）。
    """
    name = (script_name or "").strip()
    if not name:
        return None
    # 拒绝路径穿越
    if ".." in name or "\\" in name:
        logger.warning("eval 脚本名包含非法字符: %s", name)
        return None
    if not name.endswith(".py"):
        logger.warning("eval 脚本必须是 .py 文件: %s", name)
        return None
    base = Path(str(skill_dir))
    # 必须解析后仍在 skill_dir 内
    candidate1 = (base / "hooks_checks" / name).resolve()
    candidate2 = (base / name).resolve()
    for candidate in (candidate1, candidate2):
        try:
            # resolve 后必须在 skill_dir 子树内
            if candidate.is_file() and str(candidate).startswith(str(base.resolve())):
                return candidate
        except Exception:
            continue
    return None


async def run_hook_eval(
    *,
    script_path: str | Path,
    context: dict[str, Any],
    timeout_ms: int = _EVAL_DEFAULT_TIMEOUT_S * 1000,
    fail_open: bool = True,
) -> dict[str, Any]:
    """执行一个 Python eval 脚本并解析决策结果。

    返回 {"decision": "allow"|"deny"|"ask", "reason": str, "source": "eval"}
    fail_open=True 时，脚本异常/超时 → {"decision": "allow", "source": "eval_fallback"}
    fail_open=False 时 → {"decision": "deny", "source": "eval_fallback"}
    """
    timeout_s = max(1.0, timeout_ms / 1000.0)
    ctx_json = json.dumps(context, ensure_ascii=False, default=str)
    fallback_decision = "allow" if fail_open else "deny"
    fallback_reason = "eval 脚本超时或异常" if fail_open else "eval 脚本超时或异常（fail-closed）"

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "python",
            str(script_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(ctx_json.encode("utf-8")),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            logger.warning("Hook eval 脚本超时: %s (%.1fs)", script_path, timeout_s)
            return {
                "decision": fallback_decision,
                "reason": fallback_reason,
                "source": "eval_fallback",
                "eval_error": "timeout",
            }

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")[:_EVAL_MAX_STDOUT_BYTES]
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")[:2000]

        if proc.returncode != 0:
            logger.warning(
                "Hook eval 脚本非零退出: %s exit=%s stderr=%s",
                script_path, proc.returncode, stderr[:200],
            )
            return {
                "decision": fallback_decision,
                "reason": fallback_reason,
                "source": "eval_fallback",
                "eval_error": f"exit={proc.returncode}",
            }

        # 解析 JSON
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Hook eval 脚本返回非 JSON: %s", stdout[:200])
            return {
                "decision": fallback_decision,
                "reason": fallback_reason,
                "source": "eval_fallback",
                "eval_error": "json_parse",
            }

        if not isinstance(parsed, dict):
            return {
                "decision": fallback_decision,
                "reason": fallback_reason,
                "source": "eval_fallback",
                "eval_error": "not_dict",
            }

        decision = str(parsed.get("decision") or "allow").strip().lower()
        if decision not in ("allow", "deny", "ask"):
            decision = "allow"
        reason = str(parsed.get("reason") or "eval script decision")[:500]
        return {
            "decision": decision,
            "reason": reason,
            "source": "eval",
        }

    except FileNotFoundError:
        logger.warning("Hook eval 脚本 python 不可用: %s", script_path)
        return {
            "decision": fallback_decision,
            "reason": fallback_reason,
            "source": "eval_fallback",
            "eval_error": "python_not_found",
        }
    except Exception as exc:
        logger.warning("Hook eval 脚本异常: %s — %s", script_path, exc)
        return {
            "decision": fallback_decision,
            "reason": fallback_reason,
            "source": "eval_fallback",
            "eval_error": str(type(exc).__name__),
        }
    finally:
        if proc is not None:
            try:
                if proc.returncode is None:
                    proc.kill()
                    await asyncio.sleep(0)
            except Exception:
                pass
