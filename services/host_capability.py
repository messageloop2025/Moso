"""主机能力画像（host capability profile）

通过一段自检 shell 脚本，一次 SSH 探测目标主机的 OS / 硬件 / 已安装 CLI 工具，
产出结构化字典后，格式化为 Markdown 写入 `ai_host_prompts.content` 的哨兵块内。
既不额外建表，又天然复用现有的"主机级 AI 提示词"通道——AI 在规划操作前
`get_host_prompt` 看到的就已经是带画像的说明。

设计要点：
1. 存储：直接复用 `ai_host_prompts` 表（按 `(host_id, user_id)` 独立），不新增表；
2. 合并：用 `<!-- EDGEOPS:HOST_PROFILE v1 -->` 哨兵只替换块内，用户/AI 在哨兵之外
   手写的规则、注意事项等保留不动；
3. 探测：单次 SSH 跑一段 shell，单工具 `timeout 2s` 防卡死，
   输出用固定前缀（`TOOL|名字|版本` 等）便于本地解析；
4. AI 读画像：不需要任何新增上下文注入——`get_host_prompt` 拿回的 Markdown 里
   就已经含哨兵块。前端渲染也一致。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from services.ssh_client import run_ssh_command


PROFILE_SENTINEL_BEGIN = "<!-- EDGEOPS:HOST_PROFILE v1 -->"
PROFILE_SENTINEL_END = "<!-- /EDGEOPS:HOST_PROFILE v1 -->"

# 工具按语义分类；AI 可根据这些分类判断主机"能干什么"。
# 新增工具时：在这里加一项，脚本会自动探测；展示 Markdown 也会自动分组。
TOOL_GROUPS: dict[str, list[str]] = {
    "cloud_infra": [
        "docker", "podman", "kubectl", "helm", "terraform", "ansible", "packer",
        "vault", "consul", "nomad",
        "aliyun", "ossutil", "gcloud", "aws", "az", "doctl", "tencentcloud",
    ],
    "ai_cli": [
        "cursor-agent", "opencode", "aider", "claude", "codex", "goose", "cline",
        "continue", "llm", "ollama", "chatgpt",
    ],
    "runtime": [
        "python3", "python", "node", "npm", "pnpm", "yarn", "go", "rustc", "cargo",
        "ruby", "php", "java", "mvn", "gradle", "dotnet", "deno", "bun",
    ],
    "devops": [
        "git", "gh", "glab", "jq", "yq", "curl", "wget", "rsync", "make", "cmake",
        "tmux", "screen", "unzip", "7z",
    ],
    "security": [
        # 侦察
        "nmap", "masscan", "rustscan", "amass", "subfinder", "httpx", "naabu",
        # Web
        "nikto", "sqlmap", "gobuster", "ffuf", "feroxbuster", "wfuzz", "wpscan",
        # 凭证/密码
        "hydra", "medusa", "john", "hashcat", "crackmapexec",
        # 流量
        "tshark", "tcpdump", "ngrep", "wireshark",
        # Metasploit / Exploit
        "msfconsole", "msfvenom", "searchsploit",
        # 无线
        "aircrack-ng", "hcxdumptool",
        # 通用
        "responder", "nuclei",
    ],
    "database_cli": [
        "mysql", "psql", "redis-cli", "mongo", "mongosh", "sqlite3",
        "clickhouse-client", "influx",
    ],
}


def _flatten_tools() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tools in TOOL_GROUPS.values():
        for t in tools:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


ALL_TOOLS: list[str] = _flatten_tools()


def _build_probe_script(tools: list[str]) -> str:
    """生成一段自检 shell 脚本。输出行前缀固定，便于解析。"""
    tools_str = " ".join(shell_safe(t) for t in tools)
    return r"""set +e
echo "=== OS ==="
uname -s 2>/dev/null
uname -r 2>/dev/null
uname -m 2>/dev/null
if [ -r /etc/os-release ]; then
  . /etc/os-release 2>/dev/null
  echo "ID=${ID:-}"
  echo "PRETTY_NAME=${PRETTY_NAME:-}"
  echo "VERSION_ID=${VERSION_ID:-}"
fi
echo "SHELL=${SHELL:-}"
for pm in apt dnf yum apk pacman zypper brew; do
  command -v "$pm" >/dev/null 2>&1 && { echo "PKG=$pm"; break; }
done
if grep -qi kali /etc/os-release 2>/dev/null; then echo "IS_KALI=yes"; fi

echo "=== HARDWARE ==="
c=$(nproc 2>/dev/null); [ -n "$c" ] && echo "CPU_CORES=$c"
awk '/MemTotal/ { printf "MEM_KB=%d\n",$2; exit }' /proc/meminfo 2>/dev/null
if command -v nvidia-smi >/dev/null 2>&1; then
  g=$( (timeout 2 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true) | head -1)
  [ -n "$g" ] && echo "GPU=$g"
fi

echo "=== TOOLS ==="
probe_tool() {
  c="$1"
  command -v "$c" >/dev/null 2>&1 || return 0
  if command -v timeout >/dev/null 2>&1; then TO="timeout 2"; else TO=""; fi
  v=""
  for flag in "--version" "-V" "-v" "version"; do
    if [ -z "$v" ]; then
      v=$( ($TO "$c" $flag </dev/null 2>&1 || true) | head -1 | tr -d '\r' )
    fi
  done
  [ -z "$v" ] && v="(installed)"
  # 防异常字符污染解析
  v=$(printf "%s" "$v" | tr -d '|' | cut -c1-160)
  printf "TOOL|%s|%s\n" "$c" "$v"
}
for c in __TOOL_LIST__; do probe_tool "$c"; done
echo "=== END ==="
""".replace("__TOOL_LIST__", tools_str)


def shell_safe(token: str) -> str:
    """工具名一律小写字母 + 数字 + `-_.`；非法字符替换成下划线，避免任何 shell 注入。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", token)


def parse_probe_output(text: str) -> dict[str, Any]:
    """解析 probe 脚本的 stdout 为结构化字典。"""
    lines = (text or "").splitlines()
    section: str | None = None
    os_info: dict[str, str] = {}
    hardware: dict[str, Any] = {}
    tools: dict[str, str] = {}
    uname_buf: list[str] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("=== ") and line.endswith(" ==="):
            section = line.strip(" =").lower()
            continue
        if section == "os":
            if re.match(r"^[A-Z_]+=", line):
                k, _, v = line.partition("=")
                os_info[k.strip().lower()] = v.strip().strip('"')
            else:
                uname_buf.append(line)
        elif section == "hardware":
            if line.startswith("CPU_CORES="):
                try:
                    hardware["cpu_cores"] = int(line.split("=", 1)[1])
                except Exception:
                    pass
            elif line.startswith("MEM_KB="):
                try:
                    hardware["mem_total_mb"] = int(line.split("=", 1)[1]) // 1024
                except Exception:
                    pass
            elif line.startswith("GPU="):
                hardware["gpu"] = line.split("=", 1)[1].strip()
        elif section == "tools":
            if line.startswith("TOOL|"):
                parts = line.split("|", 2)
                if len(parts) == 3:
                    tools[parts[1]] = parts[2].strip() or "(installed)"

    if len(uname_buf) >= 3:
        os_info.setdefault("kernel_name", uname_buf[0])
        os_info.setdefault("kernel_release", uname_buf[1])
        os_info.setdefault("arch", uname_buf[2])
    elif len(uname_buf) >= 1:
        os_info.setdefault("kernel_name", uname_buf[0])

    grouped: dict[str, dict[str, str]] = {grp: {} for grp in TOOL_GROUPS}
    for grp, tnames in TOOL_GROUPS.items():
        for tn in tnames:
            if tn in tools:
                grouped[grp][tn] = tools[tn]

    return {
        "os": {
            "id": os_info.get("id", ""),
            "pretty_name": os_info.get("pretty_name", ""),
            "version_id": os_info.get("version_id", ""),
            "kernel_name": os_info.get("kernel_name", ""),
            "kernel_release": os_info.get("kernel_release", ""),
            "arch": os_info.get("arch", ""),
            "shell": os_info.get("shell", ""),
            "pkg_manager": os_info.get("pkg", ""),
            "is_kali": os_info.get("is_kali", "") == "yes",
        },
        "hardware": hardware,
        "tools": tools,
        "tools_by_group": grouped,
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def format_profile_markdown(data: dict[str, Any]) -> str:
    """把结构化画像渲染成带哨兵的 Markdown。"""
    os_ = data.get("os") or {}
    hw = data.get("hardware") or {}
    grouped = data.get("tools_by_group") or {}
    probed_at = data.get("probed_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines: list[str] = [
        PROFILE_SENTINEL_BEGIN,
        f"## 主机能力画像（自动采集于 {probed_at}）",
        "",
        "> 本段由 `probe_host_capabilities` 自动写入；之上/之下用户或 AI 追加的内容不会被覆盖。",
        "> AI 在规划操作前**先读本段**了解主机能力边界；**\"未安装\"的工具请勿尝试调用**。",
        "> 需要刷新请调用 `probe_host_capabilities(host_id=..., refresh=true)`。",
        "",
        "### 基础",
        f"- 系统: {os_.get('pretty_name') or os_.get('id') or '未知'}",
        f"- 内核: {(os_.get('kernel_name','') + ' ' + os_.get('kernel_release','')).strip() or '未知'}",
        f"- 架构: {os_.get('arch') or '未知'}",
        f"- Shell: {os_.get('shell') or '未知'}",
        f"- 包管理器: {os_.get('pkg_manager') or '未知'}",
    ]
    if os_.get("is_kali"):
        lines.append("- ⚠️ **Kali Linux**：该主机可用于渗透 / 安全测试（需遵守授权范围）")
    lines.append("")

    mem_mb = hw.get("mem_total_mb")
    mem_text = f"{mem_mb / 1024:.1f} GB" if isinstance(mem_mb, int) else "未知"
    if mem_text.endswith(".0 GB"):
        mem_text = mem_text.replace(".0 GB", " GB")
    lines.extend([
        "### 硬件",
        f"- CPU 核心: {hw.get('cpu_cores','未知')}",
        f"- 内存: {mem_text}",
    ])
    if hw.get("gpu"):
        lines.append(f"- GPU: {hw['gpu']}")
    lines.append("")

    group_titles = {
        "cloud_infra": "### 云与基础设施 CLI",
        "ai_cli": "### AI 代理 CLI（可供 delegate_to_cli_agent 使用）",
        "runtime": "### 语言运行时",
        "devops": "### DevOps 工具",
        "security": "### 安全 / 渗透工具",
        "database_cli": "### 数据库客户端",
    }
    for grp, title in group_titles.items():
        items = grouped.get(grp) or {}
        if not items:
            continue
        lines.append(title)
        for tname, ver in items.items():
            lines.append(f"- `{tname}`: {ver}")
        lines.append("")

    lines.append(PROFILE_SENTINEL_END)
    return "\n".join(lines).rstrip() + "\n"


_SENTINEL_RE = re.compile(
    re.escape(PROFILE_SENTINEL_BEGIN) + r".*?" + re.escape(PROFILE_SENTINEL_END) + r"\n?",
    re.DOTALL,
)


def merge_profile_into_prompt(existing_prompt: str, new_profile_md: str) -> str:
    """把新画像合并进 existing_prompt：已有哨兵块则替换，否则追加末尾。"""
    existing = (existing_prompt or "").rstrip()
    new_block = new_profile_md.strip() + "\n"
    if _SENTINEL_RE.search(existing):
        merged = _SENTINEL_RE.sub(new_block, existing)
        return merged.rstrip() + "\n"
    if not existing:
        return new_block
    return existing + "\n\n" + new_block


def extract_profile_block(prompt_text: str) -> str | None:
    """从 prompt 文本中取出画像块（含哨兵）。若无则 None。"""
    if not prompt_text:
        return None
    m = _SENTINEL_RE.search(prompt_text)
    return m.group(0) if m else None


_PROBED_AT_RE = re.compile(r"自动采集于\s+([0-9T:\-+.Z]+)")


def probed_at_of_block(block: str) -> datetime | None:
    """从画像块 Markdown 中解析采集时间（UTC）。"""
    if not block:
        return None
    m = _PROBED_AT_RE.search(block)
    if not m:
        return None
    try:
        s = m.group(1).strip()
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


async def run_probe_on_host(
    *,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str | None = None,
    key_path: str | None = None,
    private_key_pem: str | None = None,
    tools: list[str] | None = None,
    timeout: int = 40,
) -> dict[str, Any]:
    """在目标主机上跑自检脚本并解析结果。"""
    script = _build_probe_script(tools or ALL_TOOLS)
    stdout, stderr, code = await run_ssh_command(
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        password=password,
        key_path=key_path,
        private_key_pem=private_key_pem,
        command=script,
        timeout=timeout,
    )
    data = parse_probe_output(stdout or "")
    data["ssh_exit_code"] = code
    data["raw_stderr"] = (stderr or "").strip()[-500:]
    return data
