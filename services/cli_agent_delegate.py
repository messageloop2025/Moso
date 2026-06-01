"""子 AI 委派（delegate to CLI agent）

让 毛竹 的主 AI 把一段任务**委派**给远端主机上的另一个 AI 代理 CLI
（cursor-agent / opencode / aider / claude / codex / goose 等），通过 SSH 一次性
执行并把完整输出收回来。主 AI 拿到子 AI 的输出后可以继续推理、总结、或触发
后续动作（比如提交 git diff、回滚、追加测试）。

设计要点：
1. **探测优先**：调用前先读主机能力画像（`ai_host_prompts` 哨兵块）确认目标 agent
   已安装，否则直接拒绝并给出建议；
2. **模板化**：每个 agent 有"非交互 / 一次性打印"的推荐调用方式，在这里硬编码；
   用户/AI 可用 `command_template` 覆盖；
3. **工作目录**：对 `cd $workdir && $cmd` 包一层；若是 git 仓库，执行前后各记录
   `git rev-parse HEAD`，自动产出前后 diff 的统计摘要给主 AI；
4. **环境变量**：允许传 `env` 追加（比如 `CURSOR_API_KEY`），但**不回显**，
   审计日志里只记 key 列表不记 value；
5. **输出截断**：stdout 超长时保留头尾，中间留 `...[truncated N chars]...`，
   避免主 AI 上下文爆炸；
6. **超时**：总时长 10~900s（由调用方传入）；
7. **安全**：子 AI 会修改主机文件，视同 destructive 操作——Skill 描述提醒主 AI
   通过 `ask_user_choice` 先让用户确认。
"""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from typing import Any

from services.ssh_client import run_ssh_command, run_ssh_command_streaming


# ===== 支持的 agent 列表与调用模板 =====================================

@dataclass(frozen=True)
class AgentSpec:
    name: str                  # 画像里的工具名 / CLI 二进制名
    display: str               # 展示名
    build_cmd: str             # 以 {task_q} {extra} 为占位的命令模板；task_q 已经 shlex.quote 过
    modifies_files: bool       # 是否会修改主机/仓库文件（决定是否强制 confirm）
    supports_output_format: bool = False  # 是否支持 --output-format
    docs_hint: str = ""        # 给主 AI 看的一句话说明


AGENT_SPECS: dict[str, AgentSpec] = {
    "cursor-agent": AgentSpec(
        name="cursor-agent",
        display="Cursor Agent CLI",
        build_cmd="cursor-agent -p {task_q} {extra}",
        modifies_files=True,
        supports_output_format=True,
        docs_hint="Cursor 官方 CLI，headless 模式：`-p`（print）；需 `CURSOR_API_KEY` 或登录态",
    ),
    "opencode": AgentSpec(
        name="opencode",
        display="OpenCode (sst/opencode)",
        build_cmd="opencode run {task_q} {extra}",
        modifies_files=True,
        docs_hint="SST OpenCode 一次性模式：`opencode run <prompt>`；需目标主机已 `opencode auth login`",
    ),
    "aider": AgentSpec(
        name="aider",
        display="Aider",
        build_cmd="aider --message {task_q} --yes --no-pretty {extra}",
        modifies_files=True,
        docs_hint="一次性修改模式，必须在 git 仓库根目录下；推荐同时传 --file <path> 限定上下文",
    ),
    "claude": AgentSpec(
        name="claude",
        display="Claude Code CLI",
        build_cmd="claude -p {task_q} {extra}",
        modifies_files=True,
        supports_output_format=True,
        docs_hint="Anthropic claude code CLI：`-p` print 模式；需 ANTHROPIC_API_KEY",
    ),
    "codex": AgentSpec(
        name="codex",
        display="OpenAI Codex CLI",
        build_cmd="codex exec {task_q} --full-auto {extra}",
        modifies_files=True,
        docs_hint="OpenAI Codex CLI，`codex exec` 非交互 + `--full-auto` 自动审批；需 OPENAI_API_KEY",
    ),
    "goose": AgentSpec(
        name="goose",
        display="Goose (Block)",
        build_cmd="goose run --text {task_q} {extra}",
        modifies_files=True,
        docs_hint="Block 的 Goose CLI，`goose run --text` 非交互",
    ),
    "cline": AgentSpec(
        name="cline",
        display="Cline",
        build_cmd="cline run {task_q} {extra}",
        modifies_files=True,
        docs_hint="Cline CLI（若主机装了 cline）",
    ),
    "llm": AgentSpec(
        name="llm",
        display="Simon Willison's llm",
        build_cmd="llm {task_q} {extra}",
        modifies_files=False,
        docs_hint="纯 LLM 调用（不会改文件），适合让子 AI 做规划、总结、信息抽取",
    ),
}


# agent 选择优先级（auto 模式用）：改文件能力强的优先，llm 放最后（它不改文件）
AUTO_PRIORITY = [
    "cursor-agent", "opencode", "aider", "claude", "codex", "goose", "cline", "llm",
]


def pick_agent_auto(installed_tools: dict[str, str]) -> str | None:
    """按优先级从画像里挑一个已安装的 agent 名。"""
    for name in AUTO_PRIORITY:
        if name in installed_tools:
            return name
    return None


def build_agent_command(
    agent: str,
    task: str,
    *,
    model: str | None = None,
    extra_args: str = "",
    output_format: str = "",
    workdir: str = "",
    env: dict[str, str] | None = None,
    command_template: str | None = None,
) -> str:
    """构造最终发给 SSH 的 shell 命令（可以 `sh -c` 直接跑）。

    - 自动加 `cd <workdir> && ` 前缀（若指定且非空）；
    - `env` 合并成 `KEY1=... KEY2=... <cmd>` 前缀；
    - `extra_args` 原样拼接在命令末尾。
    """
    spec = AGENT_SPECS.get(agent)
    if not spec and not command_template:
        raise ValueError(f"未知 agent: {agent}；当前支持：{', '.join(AGENT_SPECS)}")

    task_q = shlex.quote(task or "")
    extras: list[str] = []
    if model:
        extras.append(f"--model {shlex.quote(model)}")
    if output_format and spec and spec.supports_output_format:
        extras.append(f"--output-format {shlex.quote(output_format)}")
    if extra_args:
        extras.append(extra_args.strip())
    extra_str = " ".join(s for s in extras if s).strip()

    if command_template:
        body = command_template.format(task_q=task_q, extra=extra_str)
    else:
        body = spec.build_cmd.format(task_q=task_q, extra=extra_str)  # type: ignore[union-attr]

    env_prefix = ""
    if env:
        parts = []
        for k, v in env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k or ""):
                continue
            parts.append(f"{k}={shlex.quote(str(v))}")
        if parts:
            env_prefix = " ".join(parts) + " "

    if workdir:
        return f"cd {shlex.quote(workdir)} && {env_prefix}{body}"
    return f"{env_prefix}{body}"


# ===== 输出处理 =========================================================

def truncate_middle(text: str, max_chars: int) -> tuple[str, bool, int]:
    """超长文本保留头尾，中间用 `...[truncated N chars]...` 代替。
    返回 (截断后文本, 是否被截断, 原长度)。"""
    if not text:
        return "", False, 0
    n = len(text)
    if n <= max_chars:
        return text, False, n
    half = max(256, (max_chars - 60) // 2)
    head = text[:half]
    tail = text[-half:]
    return f"{head}\n...[truncated {n - 2 * half} chars]...\n{tail}", True, n


# ===== git diff 自动捕获 ================================================

async def _capture_git_state(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str | None,
    key_path: str | None,
    private_key_pem: str | None,
    workdir: str,
) -> dict[str, str]:
    """在 workdir 下执行 git 状态捕获。workdir 不是 git 仓库时静默返回空 dict。"""
    if not workdir:
        return {}
    cmd = (
        f"cd {shlex.quote(workdir)} && "
        "git rev-parse --is-inside-work-tree 2>/dev/null && "
        "git rev-parse HEAD 2>/dev/null && "
        "git status --porcelain=v1 2>/dev/null | head -200"
    )
    try:
        stdout, _stderr, code = await run_ssh_command(
            host=host, port=port, username=username,
            auth_type=auth_type, password=password,
            key_path=key_path, private_key_pem=private_key_pem,
            command=cmd, timeout=15,
        )
    except Exception:
        return {}
    if code != 0 or "true" not in (stdout or "").splitlines()[:1]:
        return {}
    lines = (stdout or "").splitlines()
    head = lines[1].strip() if len(lines) > 1 else ""
    status_lines = lines[2:]
    return {"head": head, "status_preview": "\n".join(status_lines[:30])}


async def _compute_git_diff_summary(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str | None,
    key_path: str | None,
    private_key_pem: str | None,
    workdir: str,
    head_before: str,
) -> dict[str, Any]:
    """算 head_before 到当前工作区的 diff 概要：文件数 / 增删行 / 前 30 文件列表 / diff 预览。"""
    if not workdir or not head_before:
        return {}
    cmd = (
        f"cd {shlex.quote(workdir)} && "
        "{ echo '=== SHORTSTAT ==='; "
        f"git diff --shortstat {shlex.quote(head_before)} 2>/dev/null; "
        "echo '=== NAMES ==='; "
        f"git diff --name-status {shlex.quote(head_before)} 2>/dev/null | head -100; "
        "echo '=== DIFF ==='; "
        f"git diff --unified=2 {shlex.quote(head_before)} 2>/dev/null | head -400; "
        "} || true"
    )
    try:
        stdout, _stderr, _code = await run_ssh_command(
            host=host, port=port, username=username,
            auth_type=auth_type, password=password,
            key_path=key_path, private_key_pem=private_key_pem,
            command=cmd, timeout=30,
        )
    except Exception:
        return {}
    text = stdout or ""

    # 解析三个段
    section: str = ""
    shortstat = ""
    names: list[str] = []
    diff_lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("=== ") and raw.endswith(" ==="):
            section = raw.strip("= ").lower()
            continue
        if section == "shortstat":
            if raw.strip():
                shortstat = raw.strip()
        elif section == "names":
            if raw.strip():
                names.append(raw.strip())
        elif section == "diff":
            diff_lines.append(raw)

    files_changed = insertions = deletions = 0
    m = re.search(r"(\d+)\s+file[s]?\s+changed", shortstat)
    if m:
        files_changed = int(m.group(1))
    m = re.search(r"(\d+)\s+insertion", shortstat)
    if m:
        insertions = int(m.group(1))
    m = re.search(r"(\d+)\s+deletion", shortstat)
    if m:
        deletions = int(m.group(1))

    diff_preview, truncated, _full_len = truncate_middle("\n".join(diff_lines), 4000)
    if not diff_lines:
        diff_preview = ""

    return {
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "files": [ln.split("\t", 1)[-1].strip() for ln in names[:50] if ln.strip()],
        "diff_preview": diff_preview,
        "diff_preview_truncated": truncated,
        "shortstat": shortstat,
    }


# ===== 主入口 ===========================================================

@dataclass
class DelegateResult:
    success: bool
    agent: str
    cmd: str
    stdout: str
    stderr: str
    exit_code: int
    duration_sec: float
    stdout_truncated: bool
    stdout_full_length: int
    git_diff: dict[str, Any]
    error: str | None = None


async def delegate_to_agent(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str | None,
    key_path: str | None,
    private_key_pem: str | None,
    agent: str,
    task: str,
    workdir: str = "",
    model: str | None = None,
    extra_args: str = "",
    output_format: str = "",
    env: dict[str, str] | None = None,
    command_template: str | None = None,
    timeout: int = 300,
    max_output_chars: int = 20000,
    on_line: Any = None,
) -> DelegateResult:
    """在目标主机上跑子 AI，收集输出与 git diff 概要。

    `on_line(stream: str, line: str) -> Awaitable[None]`：可选流式回调；
    每行 stdout/stderr 出来就调一次，用于前端实时展示进度。不传则退化为缓冲式。
    """
    cmd_line = build_agent_command(
        agent=agent, task=task, model=model, extra_args=extra_args,
        output_format=output_format, workdir=workdir, env=env,
        command_template=command_template,
    )

    # 先抓一次 HEAD
    pre = await _capture_git_state(
        host=host, port=port, username=username,
        auth_type=auth_type, password=password, key_path=key_path, private_key_pem=private_key_pem,
        workdir=workdir,
    )
    head_before = pre.get("head") or ""

    t0 = time.time()
    err_msg: str | None = None
    stdout = stderr = ""
    code = -1
    try:
        if on_line is not None:
            stdout, stderr, code, _timed_out = await run_ssh_command_streaming(
                host=host, port=port, username=username,
                auth_type=auth_type, password=password,
                key_path=key_path, private_key_pem=private_key_pem,
                command=cmd_line, timeout=max(10, min(900, timeout)),
                on_line=on_line,
            )
        else:
            stdout, stderr, code = await run_ssh_command(
                host=host, port=port, username=username,
                auth_type=auth_type, password=password,
                key_path=key_path, private_key_pem=private_key_pem,
                command=cmd_line, timeout=max(10, min(900, timeout)),
            )
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
    dur = round(time.time() - t0, 2)

    stdout_t, truncated, stdout_full = truncate_middle(stdout, max_output_chars)
    stderr_t, _, _ = truncate_middle(stderr, 4000)

    git_diff: dict[str, Any] = {}
    if workdir and head_before and code == 0 and err_msg is None:
        git_diff = await _compute_git_diff_summary(
            host=host, port=port, username=username,
            auth_type=auth_type, password=password, key_path=key_path, private_key_pem=private_key_pem,
            workdir=workdir, head_before=head_before,
        )

    return DelegateResult(
        success=(err_msg is None and code == 0),
        agent=agent,
        cmd=cmd_line,
        stdout=stdout_t,
        stderr=stderr_t,
        exit_code=code,
        duration_sec=dur,
        stdout_truncated=truncated,
        stdout_full_length=stdout_full,
        git_diff=git_diff,
        error=err_msg,
    )


# ===== 多步编排 delegate_chain ==========================================

import asyncio as _asyncio


@dataclass
class HostConnInfo:
    """一台主机的 SSH 接入信息 + 已安装 CLI 列表（用于 chain 里每步按需切机）。"""
    host_id: int
    host: str
    port: int
    username: str
    auth_type: str
    password: str | None = None
    key_path: str | None = None
    private_key_pem: str | None = None
    installed_tools: dict[str, str] | None = None  # 预解析自画像的工具名→版本
    label: str = ""  # 主机名/nickname，供日志展示


@dataclass
class ChainStepResult:
    """链式执行中单个步骤的结果。kind 可能是 delegate / ssh / sleep / skipped。"""
    index: int
    name: str
    kind: str
    success: bool
    skipped: bool = False
    skip_reason: str = ""
    host_id: int = 0  # 实际执行该步的主机 ID（sleep 为 0）
    host_label: str = ""
    # 执行相关
    cmd: str = ""
    agent: str = ""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stdout_full_length: int = 0
    duration_sec: float = 0.0
    git_diff: dict[str, Any] | None = None
    error: str | None = None


_TEMPLATE_VAR_RE = re.compile(r"\{(prev_(?:stdout|stderr|exit_code|cmd|agent|files_changed|insertions|deletions))\}")


def _build_template_vars(prev: ChainStepResult | None) -> dict[str, str]:
    """把上一步结果扁平成可供字符串模板插值的 dict（值都转 str）。"""
    if not prev:
        return {
            "prev_stdout": "",
            "prev_stderr": "",
            "prev_exit_code": "",
            "prev_cmd": "",
            "prev_agent": "",
            "prev_files_changed": "0",
            "prev_insertions": "0",
            "prev_deletions": "0",
        }
    diff = prev.git_diff or {}
    return {
        "prev_stdout": prev.stdout or "",
        "prev_stderr": prev.stderr or "",
        "prev_exit_code": str(prev.exit_code),
        "prev_cmd": prev.cmd or "",
        "prev_agent": prev.agent or "",
        "prev_files_changed": str(diff.get("files_changed", 0)),
        "prev_insertions": str(diff.get("insertions", 0)),
        "prev_deletions": str(diff.get("deletions", 0)),
    }


def _substitute(text: str, vars_: dict[str, str]) -> str:
    """安全字符串插值：只替换白名单变量，保留其它花括号不动。"""
    if not text or not vars_:
        return text or ""

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        return vars_.get(key, m.group(0))

    return _TEMPLATE_VAR_RE.sub(_repl, text)


async def run_chain(
    *,
    hosts: dict[int, HostConnInfo],
    default_host_id: int,
    steps: list[dict[str, Any]],
    stop_on_failure: bool = True,
    max_output_chars: int = 20000,
    default_timeout: int = 300,
    on_event: Any = None,
) -> list[ChainStepResult]:
    """按顺序执行 steps。step 结构见 `delegate_chain` 技能说明。

    支持 kind：
    - `delegate`：调用子 AI（等价 delegate_to_agent）
    - `ssh`：一次性 SSH 命令
    - `sleep`：等待 N 秒（用于节流）

    支持 when：
    - `always`（默认第 1 步；后续步显式指定）
    - `on_success`（默认 i>0 步；前一步成功才跑）
    - `on_failure`（前一步失败才跑——适合"pytest 失败→让子 AI 修复"）

    跨主机：step.host_id 可选；缺省走 default_host_id。
    `hosts` 必须包含所有用到的 host_id（包括 default），否则该步报 error。

    `on_event(payload: dict) -> Awaitable[None]`：可选事件回调；每一步开始 / 每行
    stdout|stderr / 每步结束会调一次，用于 SSE 给前端推进度。payload 里必含 `kind`
    字段（chain_step_start / chain_step_line / chain_step_end / chain_step_skip）。
    """
    results: list[ChainStepResult] = []
    prev: ChainStepResult | None = None

    async def _emit(payload: dict) -> None:
        if on_event is None:
            return
        try:
            await on_event(payload)
        except Exception:
            pass

    for i, raw_step in enumerate(steps):
        step = dict(raw_step or {})
        kind = (step.get("kind") or "delegate").lower()
        name = str(step.get("name") or f"step{i + 1}")
        # 确定本步执行的主机
        try:
            step_host_id = int(step.get("host_id")) if step.get("host_id") is not None else default_host_id
        except Exception:
            step_host_id = default_host_id
        hi = hosts.get(step_host_id) if kind != "sleep" else None
        if kind != "sleep" and hi is None:
            sr = ChainStepResult(
                index=i, name=name, kind=kind, success=False,
                host_id=step_host_id,
                error=f"未提供 host_id={step_host_id} 的连接信息",
            )
            results.append(sr)
            prev = sr
            if stop_on_failure:
                break
            continue

        # when 条件
        when = (step.get("when") or ("always" if i == 0 else "on_success")).lower()
        prev_ok = prev is not None and prev.success
        should_run = (
            when == "always"
            or (when == "on_success" and (prev is None or prev_ok))
            or (when == "on_failure" and prev is not None and not prev_ok)
        )
        if not should_run:
            reason = (
                f"when={when}，但上一步 success={prev_ok}"
                if prev is not None
                else f"when={when} 但无前置结果"
            )
            sr_skip = ChainStepResult(
                index=i, name=name, kind=kind, success=True,
                skipped=True, skip_reason=reason,
            )
            results.append(sr_skip)
            await _emit({
                "kind": "chain_step_skip",
                "index": i, "name": name, "step_kind": kind,
                "reason": reason,
            })
            continue

        tvars = _build_template_vars(prev)
        await _emit({
            "kind": "chain_step_start",
            "index": i, "name": name, "step_kind": kind,
            "host_id": (hi.host_id if hi else 0),
            "host_label": (hi.label if hi else ""),
        })

        if kind == "delegate":
            task = _substitute(str(step.get("task") or ""), tvars)
            workdir = str(step.get("workdir") or "")
            agent = str(step.get("agent") or "")
            model = step.get("model") or None
            extra_args = _substitute(str(step.get("extra_args") or ""), tvars)
            env = step.get("env") or {}
            command_template = step.get("command_template") or None
            output_format = str(step.get("output_format") or "")
            timeout = int(step.get("timeout") or default_timeout)
            max_out = int(step.get("max_output_chars") or max_output_chars)
            if not agent or not task:
                results.append(ChainStepResult(
                    index=i, name=name, kind=kind, success=False,
                    error="delegate 步骤缺少 agent 或 task",
                ))
                if stop_on_failure:
                    prev = results[-1]
                    break
                prev = results[-1]
                continue
            async def _line_cb(stream: str, line: str, _i=i, _name=name, _hid=hi.host_id, _hlbl=hi.label) -> None:
                await _emit({
                    "kind": "chain_step_line",
                    "index": _i, "name": _name, "step_kind": "delegate",
                    "host_id": _hid, "host_label": _hlbl,
                    "stream": stream, "line": line[:2000],
                })
            dr = await delegate_to_agent(
                host=hi.host, port=hi.port, username=hi.username,
                auth_type=hi.auth_type, password=hi.password,
                key_path=hi.key_path, private_key_pem=hi.private_key_pem,
                agent=agent, task=task, workdir=workdir, model=model,
                extra_args=extra_args, output_format=output_format,
                env={str(k): str(v) for k, v in (env or {}).items()},
                command_template=command_template,
                timeout=max(10, min(900, timeout)),
                max_output_chars=max(2000, min(200000, max_out)),
                on_line=(_line_cb if on_event is not None else None),
            )
            sr = ChainStepResult(
                index=i, name=name, kind=kind, success=dr.success,
                host_id=hi.host_id, host_label=hi.label,
                cmd=dr.cmd, agent=dr.agent, exit_code=dr.exit_code,
                stdout=dr.stdout, stderr=dr.stderr,
                stdout_truncated=dr.stdout_truncated, stdout_full_length=dr.stdout_full_length,
                duration_sec=dr.duration_sec, git_diff=dr.git_diff, error=dr.error,
            )
            results.append(sr)
            prev = sr
            await _emit({
                "kind": "chain_step_end",
                "index": i, "name": name, "step_kind": "delegate",
                "host_id": hi.host_id, "host_label": hi.label,
                "success": sr.success, "exit_code": sr.exit_code,
                "duration_sec": sr.duration_sec,
                "files_changed": (sr.git_diff or {}).get("files_changed", 0),
                "error": sr.error,
            })
            if stop_on_failure and not sr.success:
                break

        elif kind == "ssh":
            command = _substitute(str(step.get("command") or ""), tvars)
            workdir = str(step.get("workdir") or "")
            timeout = int(step.get("timeout") or 60)
            max_out = int(step.get("max_output_chars") or max_output_chars)
            if not command:
                results.append(ChainStepResult(
                    index=i, name=name, kind=kind, success=False,
                    error="ssh 步骤缺少 command",
                ))
                if stop_on_failure:
                    prev = results[-1]
                    break
                prev = results[-1]
                continue
            final_cmd = f"cd {shlex.quote(workdir)} && {command}" if workdir else command
            t0 = time.time()
            err_msg = None
            out = err = ""
            rc = -1
            try:
                if on_event is not None:
                    async def _line_cb_ssh(stream: str, line: str, _i=i, _name=name, _hid=hi.host_id, _hlbl=hi.label) -> None:
                        await _emit({
                            "kind": "chain_step_line",
                            "index": _i, "name": _name, "step_kind": "ssh",
                            "host_id": _hid, "host_label": _hlbl,
                            "stream": stream, "line": line[:2000],
                        })
                    out, err, rc, _to = await run_ssh_command_streaming(
                        host=hi.host, port=hi.port, username=hi.username,
                        auth_type=hi.auth_type, password=hi.password,
                        key_path=hi.key_path, private_key_pem=hi.private_key_pem,
                        command=final_cmd, timeout=max(5, min(900, timeout)),
                        on_line=_line_cb_ssh,
                    )
                else:
                    out, err, rc = await run_ssh_command(
                        host=hi.host, port=hi.port, username=hi.username,
                        auth_type=hi.auth_type, password=hi.password,
                        key_path=hi.key_path, private_key_pem=hi.private_key_pem,
                        command=final_cmd, timeout=max(5, min(900, timeout)),
                    )
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
            dur = round(time.time() - t0, 2)
            out_t, trunc, full = truncate_middle(out, max(2000, min(200000, max_out)))
            err_t, _, _ = truncate_middle(err, 4000)
            sr = ChainStepResult(
                index=i, name=name, kind=kind,
                success=(err_msg is None and rc == 0),
                host_id=hi.host_id, host_label=hi.label,
                cmd=final_cmd, exit_code=rc,
                stdout=out_t, stderr=err_t,
                stdout_truncated=trunc, stdout_full_length=full,
                duration_sec=dur, error=err_msg,
            )
            results.append(sr)
            prev = sr
            await _emit({
                "kind": "chain_step_end",
                "index": i, "name": name, "step_kind": "ssh",
                "host_id": hi.host_id, "host_label": hi.label,
                "success": sr.success, "exit_code": sr.exit_code,
                "duration_sec": sr.duration_sec, "error": sr.error,
            })
            if stop_on_failure and not sr.success:
                break

        elif kind == "sleep":
            try:
                seconds = max(0, min(600, int(step.get("seconds") or 0)))
            except Exception:
                seconds = 0
            t0 = time.time()
            if seconds:
                await _asyncio.sleep(seconds)
            sr = ChainStepResult(
                index=i, name=name, kind=kind, success=True,
                duration_sec=round(time.time() - t0, 2),
                stdout=f"slept {seconds}s",
            )
            results.append(sr)
            prev = sr
            await _emit({
                "kind": "chain_step_end",
                "index": i, "name": name, "step_kind": "sleep",
                "host_id": 0, "host_label": "",
                "success": True, "duration_sec": sr.duration_sec,
                "slept_seconds": seconds,
            })

        else:
            sr = ChainStepResult(
                index=i, name=name, kind=kind, success=False,
                error=f"未知 step.kind={kind}（仅支持 delegate / ssh / sleep）",
            )
            results.append(sr)
            prev = sr
            await _emit({
                "kind": "chain_step_end",
                "index": i, "name": name, "step_kind": kind,
                "host_id": (hi.host_id if hi else 0),
                "host_label": (hi.label if hi else ""),
                "success": False, "error": sr.error,
            })
            if stop_on_failure:
                break

    return results
