"""主机类型与版本检测：通过 SSH 识别 Linux/macOS/Windows/FreeBSD/OpenBSD/NetBSD 及版本、Shell、包管理器，供 AI 优化命令与脚本策略"""
import re
import logging
from typing import Tuple, Dict, Any, Optional

from services.ssh_client import run_ssh_command

logger = logging.getLogger("edgeops.host_detection")

HostEnvResult = Dict[str, Any]

_UNKNOWN = "未知"


def _unknown(s: Optional[str]) -> bool:
    return not (s or "").strip() or (s or "").strip() == _UNKNOWN


async def detect_host_os(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password=None,
    key_path=None,
    private_key_pem=None,
    timeout: int = 15,
) -> Tuple[str, str]:
    """通过 SSH 检测主机操作系统类型与版本。完整环境信息请使用 detect_host_env()。"""
    env = await detect_host_env(
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        password=password,
        key_path=key_path,
        private_key_pem=private_key_pem,
        timeout=timeout,
    )
    return (env["host_type"], env["host_version"])


async def detect_host_env(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password=None,
    key_path=None,
    private_key_pem=None,
    timeout: int = 15,
) -> HostEnvResult:
    """
    通过 SSH 检测主机环境。返回：
    - host_type / host_version: 可能为「未知」
    - shell: 登录默认 Shell（如 bash）；仅在无法检测且无法推断时为「未知」
    - package_manager: 系统约定默认包管理器（如 apt）；同上
    """
    result: HostEnvResult = {
        "host_type": _UNKNOWN,
        "host_version": _UNKNOWN,
        "shell": _UNKNOWN,
        "package_manager": _UNKNOWN,
    }
    auth = {
        "host": host,
        "port": port,
        "username": username,
        "auth_type": auth_type or "password",
        "password": password,
        "key_path": key_path,
        "private_key_pem": private_key_pem,
        "timeout": timeout,
    }

    os_release_lines: Optional[list] = None

    try:
        out, err, code = await run_ssh_command(
            **auth,
            command=(
                "uname -s 2>/dev/null; echo '---'; "
                "cat /etc/os-release 2>/dev/null || true; echo '---'; "
                "sw_vers 2>/dev/null || true; echo '---'; "
                "freebsd-version -u 2>/dev/null || uname -r 2>/dev/null || true; "
                "true"
            ),
        )
        text = (out or "").strip()
        if code == 0 and text:
            parts = _split_parts(text, "---", 4)
            uname_s = (parts[0][0] if (parts and parts[0]) else "").strip().upper()
            os_release = parts[1] if len(parts) > 1 else []
            sw_vers_lines = parts[2] if len(parts) > 2 else []
            bsd_lines = parts[3] if len(parts) > 3 else []
            bsd_version_line = (bsd_lines[0] if bsd_lines else "").strip()
            os_release_lines = os_release

            if "DARWIN" in uname_s:
                result["host_type"] = "macOS"
                result["host_version"] = _parse_sw_vers(sw_vers_lines) or _UNKNOWN
            elif "FREEBSD" in uname_s:
                result["host_type"] = "FreeBSD"
                result["host_version"] = bsd_version_line[:120] if bsd_version_line else _UNKNOWN
            elif "OPENBSD" in uname_s:
                result["host_type"] = "OpenBSD"
                result["host_version"] = bsd_version_line[:120] if bsd_version_line else _UNKNOWN
            elif "NETBSD" in uname_s:
                result["host_type"] = "NetBSD"
                result["host_version"] = bsd_version_line[:120] if bsd_version_line else _UNKNOWN
            elif "LINUX" in uname_s:
                result["host_type"] = "Linux"
                result["host_version"] = _parse_os_release(os_release) or _UNKNOWN

            if result["host_type"] != _UNKNOWN:
                await _detect_unix_shell_and_pkg(auth, result)
                _infer_shell_and_package_manager(result, os_release_lines)
                return result
    except Exception as e:
        logger.debug("Unix 系检测失败 %s: %s", host, e)

    try:
        out, err, code = await run_ssh_command(
            **auth,
            command="ver 2>nul || cmd /c ver 2>nul",
        )
        text = ((out or "") + (err or "")).strip()
        if code == 0 and text:
            version = _parse_windows_ver(text)
            if version or "Windows" in text or "Microsoft" in text:
                result["host_type"] = "Windows"
                result["host_version"] = version or _normalize_win_version(text[:120]) or _UNKNOWN
                await _detect_windows_shell_and_pkg(auth, result)
                _infer_shell_and_package_manager(result, None)
                return result
    except Exception as e:
        logger.debug("Windows ver 检测失败 %s: %s", host, e)

    try:
        out, err, code = await run_ssh_command(
            **auth,
            command="wmic os get Caption /value 2>nul",
        )
        text = (out or "").strip()
        if code == 0 and "Caption" in text:
            m = re.search(r"Caption=(.+)", text, re.IGNORECASE)
            if m:
                result["host_type"] = "Windows"
                result["host_version"] = m.group(1).strip()[:120] or _UNKNOWN
                await _detect_windows_shell_and_pkg(auth, result)
                _infer_shell_and_package_manager(result, None)
                return result
    except Exception as e:
        logger.debug("Windows wmic 检测失败 %s: %s", host, e)

    return result


def _split_parts(text: str, sep: str, max_parts: int = 4) -> list:
    lines = text.split("\n")
    parts = []
    current = []
    for line in lines:
        if line.strip() == sep:
            parts.append(current)
            current = []
            if len(parts) >= max_parts - 1:
                current = []
                break
        else:
            current.append(line.strip())
    parts.append(current)
    return parts


def _parse_os_release_dict(lines: list) -> dict:
    d: dict = {}
    for line in lines:
        line = (line or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            d[k] = v
    return d


def _infer_shell_and_package_manager(result: HostEnvResult, os_release_lines: Optional[list]) -> None:
    """在探测失败或部分失败时，按 OS / 发行版惯例补齐 shell / 包管理器。"""
    ht = result.get("host_type") or ""
    shell = result.get("shell") or ""
    pkg = result.get("package_manager") or ""
    if _unknown(shell):
        shell = ""
    if _unknown(pkg):
        pkg = ""

    if ht == "Linux" and os_release_lines:
        d = _parse_os_release_dict(os_release_lines)
        oid = (d.get("ID") or "").strip().lower()
        ver_id = (d.get("VERSION_ID") or "").strip()
        id_like = (d.get("ID_LIKE") or "").lower()

        if not pkg:
            if oid in (
                "ubuntu",
                "debian",
                "linuxmint",
                "pop",
                "raspbian",
                "kali",
                "elementary",
                "zorin",
                "deepin",
                "kylin",
            ) or "debian" in id_like:
                pkg = "apt"
            elif oid == "fedora" or "fedora" in id_like:
                pkg = "dnf"
            elif oid in ("almalinux", "rocky"):
                pkg = "dnf"
            elif oid == "centos":
                try:
                    major = int((ver_id or "7").split(".")[0])
                    pkg = "dnf" if major >= 8 else "yum"
                except (TypeError, ValueError):
                    pkg = "yum"
            elif oid == "rhel" or "rhel" in id_like:
                try:
                    major = int((ver_id or "7").split(".")[0])
                    pkg = "dnf" if major >= 8 else "yum"
                except (TypeError, ValueError):
                    pkg = "yum"
            elif oid in ("opensuse-leap", "opensuse-tumbleweed") or "suse" in id_like:
                pkg = "zypper"
            elif oid in ("arch", "manjaro", "endeavouros") or "arch" in id_like:
                pkg = "pacman"
            elif oid == "alpine":
                pkg = "apk"
            elif oid == "amzn":
                try:
                    pkg = "dnf" if ver_id.startswith("202") and int(ver_id[:4]) >= 2023 else "yum"
                except (TypeError, ValueError):
                    pkg = "yum"

        if not shell:
            if oid == "alpine" or pkg == "apk":
                shell = "ash"
            elif pkg == "apt" or oid in (
                "ubuntu",
                "debian",
                "linuxmint",
                "pop",
                "raspbian",
                "kali",
            ) or "debian" in id_like:
                shell = "bash"
            elif pkg in ("yum", "dnf", "zypper", "pacman"):
                shell = "bash"

    elif ht == "macOS":
        if not pkg:
            pkg = "brew"
        if not shell:
            shell = "zsh"

    elif ht == "FreeBSD":
        if not pkg:
            pkg = "pkg"
        if not shell:
            shell = "sh"
    elif ht == "OpenBSD":
        if not pkg:
            pkg = "pkg_add"
        if not shell:
            shell = "ksh"
    elif ht == "NetBSD":
        if not pkg:
            pkg = "pkgin"
        if not shell:
            shell = "sh"

    elif ht == "Windows":
        if not pkg:
            pkg = "winget"
        if not shell:
            shell = "cmd"

    result["shell"] = shell if shell else _UNKNOWN
    result["package_manager"] = pkg if pkg else _UNKNOWN


async def _detect_unix_shell_and_pkg(auth: dict, result: HostEnvResult) -> None:
    """Unix：登录 Shell（passwd / SHELL）与 command -v 包管理器。末尾 true 避免 which 失败导致整段非 0 退出码。"""
    try:
        out, err, code = await run_ssh_command(
            **auth,
            command=(
                "u=$(id -un 2>/dev/null); "
                "sh7=$(getent passwd \"$u\" 2>/dev/null | cut -d: -f7); "
                "echo \"${sh7:-${SHELL:-}}\"; echo '---'; "
                "command -v apt >/dev/null 2>&1 && echo apt; "
                "command -v apt-get >/dev/null 2>&1 && echo apt; "
                "command -v dnf >/dev/null 2>&1 && echo dnf; "
                "command -v yum >/dev/null 2>&1 && echo yum; "
                "command -v apk >/dev/null 2>&1 && echo apk; "
                "command -v zypper >/dev/null 2>&1 && echo zypper; "
                "command -v pacman >/dev/null 2>&1 && echo pacman; "
                "command -v brew >/dev/null 2>&1 && echo brew; "
                "command -v port >/dev/null 2>&1 && echo port; "
                "true"
            ),
        )
        text = (out or "").strip()
        if not text:
            return
        parts = text.split("---", 1)
        shell_line = (parts[0] or "").strip().split("\n")[0].strip()
        which_block = (parts[1] or "").strip() if len(parts) > 1 else ""

        if shell_line and shell_line != "unknown":
            shell_name = shell_line.split("/")[-1].strip()
            if shell_name:
                result["shell"] = shell_name

        # 包管理器优先级（从高到低常见度）
        found_lines = [ln.strip() for ln in which_block.split("\n") if ln.strip()]
        priority = ("apt", "dnf", "yum", "zypper", "pacman", "apk", "brew", "port")
        for name in priority:
            if name in found_lines:
                if name == "port":
                    result["package_manager"] = "port"
                else:
                    result["package_manager"] = name
                break
    except Exception as e:
        logger.debug("Unix shell/pkg 检测失败 %s: %s", auth.get("host"), e)


async def _detect_windows_shell_and_pkg(auth: dict, result: HostEnvResult) -> None:
    """Windows：COMSPEC 判断 cmd/powershell；choco / winget。末尾 exit /b 0。"""
    try:
        out, err, code = await run_ssh_command(
            **auth,
            command=(
                "echo %COMSPEC% & where powershell 2>nul & echo --- & where choco 2>nul & where winget 2>nul"
                " & ver >nul"
            ),
        )
        text = ((out or "") + (err or "")).strip()
        if not text:
            return
        low = text.lower()
        parts = re.split(r"\s*---\s*", text, maxsplit=1)
        shell_block = (parts[0] or "").strip()
        which_block = (parts[1] or "").strip() if len(parts) > 1 else ""

        lines = [ln.strip() for ln in shell_block.split("\n") if ln.strip()]
        comspec_line = (lines[0] if lines else shell_block).lower()
        # Windows OpenSSH 默认会话多为 cmd；以 COMSPEC 行为准
        if "cmd.exe" in comspec_line:
            result["shell"] = "cmd"
        elif "powershell" in comspec_line:
            result["shell"] = "powershell"
        elif "powershell.exe" in shell_block.lower() and "cmd.exe" not in comspec_line:
            result["shell"] = "powershell"

        wlow = which_block.lower()
        if "choco" in wlow:
            result["package_manager"] = "chocolatey"
        elif "winget" in wlow:
            result["package_manager"] = "winget"
    except Exception as e:
        logger.debug("Windows shell/pkg 检测失败 %s: %s", auth.get("host"), e)


def _parse_os_release(lines: list) -> str:
    name = ""
    version = ""
    for line in lines:
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip().upper(), v.strip().strip('"').strip("'")
        if k == "PRETTY_NAME" and v:
            return v[:200]
        if k == "NAME":
            name = v
        if k == "VERSION" or k == "VERSION_ID":
            version = v
    if name or version:
        return f"{name} {version}".strip()[:200]
    return ""


def _parse_sw_vers(lines: list) -> str:
    name = ""
    version = ""
    for line in lines:
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "ProductName" and v:
            name = v
        if k == "ProductVersion" and v:
            version = v
    if name and version:
        return f"{name} {version}"[:200]
    return version[:200] if version else ""


def _parse_windows_ver(text: str) -> str:
    m = re.search(r"\[Version\s+([^\]]+)\]", text, re.IGNORECASE)
    if m:
        return _normalize_win_version(m.group(1).strip())
    m = re.search(r"\[[^\]]*?(\d+\.\d+(?:\.\d+)*)\s*\]", text)
    if m:
        return _normalize_win_version(m.group(1).strip())
    m = re.search(r"Microsoft Windows[^\[]*\[?([^\]\r\n]+)\]?", text, re.IGNORECASE)
    if m:
        return _normalize_win_version(m.group(1).strip()[:120])
    return ""


def _normalize_win_version(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", s)
    if m:
        return "Windows " + m.group(1)
    return ""
