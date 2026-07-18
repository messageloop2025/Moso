"""P2-8：在 User Skill 的 scripts/ 目录内执行脚本（超时、禁网尽力约束）。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _skill_scripts_dir(user: dict, skill_name: str) -> Path | None:
    from services.user_skills_registry import skill_md_path

    md = skill_md_path(user, skill_name)
    if not md or not md.is_file():
        return None
    return md.parent / "scripts"


async def run_skill_script(
    user: dict,
    *,
    skill_name: str,
    script: str,
    args: list[str] | None = None,
    timeout_sec: int = 30,
) -> str:
    """仅允许执行 skills/<name>/scripts/ 下的文件；禁止路径穿越。"""
    name = (skill_name or "").strip().lower()
    script_name = (script or "").strip().replace("\\", "/").lstrip("/")
    if "/" in script_name or ".." in script_name or not _SAFE_NAME.match(script_name.split("/")[-1]):
        return json.dumps(
            {"success": False, "error": "script 仅允许 scripts/ 下的单层文件名"},
            ensure_ascii=False,
        )
    if not name or not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", name):
        return json.dumps({"success": False, "error": "无效 skill_name"}, ensure_ascii=False)

    scripts_dir = _skill_scripts_dir(user, name)
    if scripts_dir is None or not scripts_dir.is_dir():
        return json.dumps(
            {"success": False, "error": f"Skill {name} 无 scripts/ 目录"},
            ensure_ascii=False,
        )
    target = (scripts_dir / script_name).resolve()
    try:
        target.relative_to(scripts_dir.resolve())
    except ValueError:
        return json.dumps({"success": False, "error": "路径越界"}, ensure_ascii=False)
    if not target.is_file():
        return json.dumps({"success": False, "error": f"脚本不存在: {script_name}"}, ensure_ascii=False)

    timeout = max(1, min(120, int(timeout_sec or 30)))
    argv = list(args or [])
    if not all(isinstance(a, str) and len(a) < 500 for a in argv):
        return json.dumps({"success": False, "error": "args 必须为短字符串数组"}, ensure_ascii=False)

    suffix = target.suffix.lower()
    if suffix == ".py":
        cmd = [sys.executable, str(target), *argv]
    elif suffix in (".sh", ".bash"):
        cmd = ["bash", str(target), *argv]
    elif suffix in (".ps1",):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(target), *argv]
    else:
        # 可执行脚本（无扩展名或 .cmd）
        cmd = [str(target), *argv]

    env = os.environ.copy()
    # 尽力禁网：提示子进程勿出网（无法在所有平台硬拦）
    env["EDGEOPS_SKILL_SCRIPT_NO_NET"] = "1"
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("ALL_PROXY", None)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(scripts_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return json.dumps(
                {"success": False, "error": f"超时（>{timeout}s）", "timeout": True},
                ensure_ascii=False,
            )
        out = (stdout_b or b"").decode("utf-8", errors="replace")[:20000]
        err = (stderr_b or b"").decode("utf-8", errors="replace")[:8000]
        return json.dumps(
            {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": out,
                "stderr": err,
                "script": script_name,
                "skill": name,
                "note": "沙箱约束：仅 scripts/、超时、清除代理环境变量；未做内核级禁网",
            },
            ensure_ascii=False,
        )
    except FileNotFoundError as e:
        return json.dumps({"success": False, "error": f"解释器不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
