"""SCP 客户端：通过系统 scp 命令在 毛竹 服务端与用户 web/fs、远程主机之间传输大文件/目录。"""
from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal, Optional, Tuple

import config
from services.paramiko_banner_fix import patch_banner_encoding, unpatch_banner_encoding
from services.ssh_connect import establish_ssh_client


def scp_command_available() -> bool:
    """当前运行环境是否可调用 scp。"""
    return shutil.which("scp") is not None


def sshpass_command_available() -> bool:
    """当前运行环境是否可调用 sshpass。"""
    return shutil.which("sshpass") is not None


def _known_hosts_file() -> str:
    return "NUL" if sys.platform == "win32" else "/dev/null"


def _legacy_rsa_scp_opts() -> list[str]:
    return [
        "-o",
        "HostKeyAlgorithms=+ssh-rsa",
        "-o",
        "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "-o",
        "PubkeyAcceptedKeyTypes=+ssh-rsa",
    ]


def _scp_attempt_modes(legacy_rsa: bool) -> list[tuple[bool, str]]:
    if legacy_rsa:
        return [(True, "legacy")]
    if not getattr(config, "SSH_LEGACY_RSA", True):
        return [(False, "modern")]
    if getattr(config, "SSH_TRY_LEGACY_RSA_FIRST", False):
        return [(True, "legacy"), (False, "modern")]
    return [(False, "modern"), (True, "legacy")]


def _scp_error_suggests_legacy_rsa(stderr: str, stdout: str) -> bool:
    text = f"{stderr or ''}\n{stdout or ''}".lower()
    markers = (
        "no matching host key",
        "no matching pubkey",
        "unable to negotiate",
        "algorithm negotiation fail",
        "ssh-rsa",
        "no mutual signature",
    )
    return any(m in text for m in markers)


def _build_scp_argv(
    *,
    direction: Literal["push", "pull"],
    local_abs: str,
    remote_user: str,
    remote_host: str,
    remote_path: str,
    port: int,
    compress: bool,
    recursive: bool,
    legacy_rsa: bool,
    identity_file: Optional[str],
) -> list[str]:
    argv: list[str] = ["scp"]
    if compress:
        argv.append("-C")
    if recursive:
        argv.append("-r")
    argv.extend(
        [
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"UserKnownHostsFile={_known_hosts_file()}",
            "-P",
            str(int(port or 22)),
        ]
    )
    if legacy_rsa:
        argv.extend(_legacy_rsa_scp_opts())
    if identity_file:
        argv.extend(["-i", identity_file])
    remote_spec = f"{remote_user}@{remote_host}:{remote_path}"
    if direction == "push":
        argv.extend(["--", local_abs, remote_spec])
    else:
        argv.extend(["--", remote_spec, local_abs])
    return argv


def _create_askpass_launcher(password: str) -> tuple[list[str], dict[str, str]]:
    """创建 SSH_ASKPASS 启动器（Windows/Linux/macOS 通用）。返回待清理路径列表与环境变量。"""
    py_fd, py_path = tempfile.mkstemp(prefix="edgeops-askpass-", suffix=".py")
    try:
        os.write(
            py_fd,
            b"import os,sys\nsys.stdout.write(os.environ.get('EDGEOPS_SCP_ASKPASS_PW',''))\n",
        )
    finally:
        os.close(py_fd)

    cleanup_paths = [py_path]
    if sys.platform == "win32":
        cmd_fd, cmd_path = tempfile.mkstemp(prefix="edgeops-askpass-", suffix=".cmd")
        try:
            launcher = f'@echo off\r\n"{sys.executable}" "{py_path}"\r\n'
            os.write(cmd_fd, launcher.encode("utf-8"))
        finally:
            os.close(cmd_fd)
        askpass_path = cmd_path
        cleanup_paths.append(cmd_path)
    else:
        os.chmod(py_path, 0o700)
        askpass_path = py_path

    env_extra = {
        "EDGEOPS_SCP_ASKPASS_PW": password or "",
        "SSH_ASKPASS": askpass_path,
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": ":0",
    }
    return cleanup_paths, env_extra


def _password_scp_backends() -> list[str]:
    """密码认证时尝试的 scp 后端顺序。"""
    backends: list[str] = []
    if sshpass_command_available():
        backends.append("sshpass")
    backends.append("askpass")
    return backends


def _run_subprocess(
    argv: list[str],
    *,
    env: Optional[dict],
    timeout: int,
    use_askpass: bool = False,
) -> Tuple[str, str, int]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            env=env,
            timeout=max(5, int(timeout)),
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL if use_askpass else None,
        )
        return proc.stdout or "", proc.stderr or "", int(proc.returncode)
    except subprocess.TimeoutExpired:
        return "", f"scp 传输超时（>{timeout}s）", 124
    except FileNotFoundError as e:
        return "", str(e), 127


def _run_scp_with_password_backend(
    *,
    backend: str,
    argv: list[str],
    password: str,
    timeout: int,
) -> Tuple[str, str, int, list[str]]:
    """用指定密码后端执行 scp。返回 (stdout, stderr, code, cleanup_paths)。"""
    cleanup_paths: list[str] = []
    env = os.environ.copy()
    cmd = argv
    use_askpass = False

    if backend == "sshpass":
        env["SSHPASS"] = password
        cmd = ["sshpass", "-e", *argv]
    elif backend == "askpass":
        cleanup_paths, ask_env = _create_askpass_launcher(password)
        env.update(ask_env)
        use_askpass = True
    else:
        return "", f"未知密码后端: {backend}", 1, cleanup_paths

    try:
        return (*_run_subprocess(cmd, env=env, timeout=timeout, use_askpass=use_askpass), cleanup_paths)
    except Exception:
        for p in cleanup_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise


def _sftp_mkdir_p(sftp, remote_dir: str) -> None:
    parts = [p for p in remote_dir.replace("\\", "/").split("/") if p]
    cur = ""
    for part in parts:
        cur = f"{cur}/{part}" if cur else f"/{part}"
        try:
            sftp.mkdir(cur)
        except OSError:
            pass


def _paramiko_transfer_sync(
    *,
    direction: Literal["push", "pull"],
    local_path: str,
    remote_user: str,
    remote_host: str,
    remote_path: str,
    port: int,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    recursive: bool,
    timeout: int,
) -> Tuple[str, str, int]:
    """密码 scp 不可用时的 Paramiko SFTP 流式回退（支持单文件与 recursive 目录）。"""
    from services.ssh_client import _sftp_get_to_local_path_sync

    local_abs = str(Path(local_path).resolve())
    client = None
    try:
        patch_banner_encoding()
        client = establish_ssh_client(
            hostname=remote_host,
            port=port,
            username=remote_user,
            auth_type=auth_type,
            password=password,
            key_path=key_path,
            private_key_pem=private_key_pem,
            timeout=timeout,
        )
        sftp = client.open_sftp()
        try:
            if direction == "push":
                lp = Path(local_abs)
                if lp.is_file():
                    sftp.put(local_abs, remote_path)
                    return "", "已通过 SFTP 回退上传（Paramiko 流式）", 0
                if lp.is_dir() and recursive:
                    remote_base = remote_path.replace("\\", "/").rstrip("/")
                    _sftp_mkdir_p(sftp, remote_base)
                    for root, _dirs, files in os.walk(local_abs):
                        rel = os.path.relpath(root, local_abs)
                        remote_root = remote_base if rel == "." else posixpath.join(
                            remote_base, rel.replace("\\", "/")
                        )
                        _sftp_mkdir_p(sftp, remote_root)
                        for fname in files:
                            src = os.path.join(root, fname)
                            dst = posixpath.join(remote_root, fname)
                            sftp.put(src, dst)
                    return "", "已通过 SFTP 回退上传目录（Paramiko 流式）", 0
                return "", "SFTP 回退仅支持单文件或 recursive 目录", 2

            # pull
            try:
                st = sftp.stat(remote_path)
            except FileNotFoundError:
                return "", "远程文件不存在", 2
            except Exception as e:
                return "", str(e), 1

            is_dir = (st.st_mode is not None) and ((st.st_mode & 0o170000) == 0o040000)
            if is_dir:
                if not recursive:
                    return "", "远程路径为目录，请设置 recursive=true", 2
                local_base = Path(local_abs)
                local_base.mkdir(parents=True, exist_ok=True)

                def _walk_remote(rdir: str, ldir: Path) -> None:
                    for ent in sftp.listdir_attr(rdir):
                        name = ent.filename
                        if name in (".", ".."):
                            continue
                        rpath = posixpath.join(rdir, name) if rdir != "/" else f"/{name}"
                        lpath = ldir / name
                        if (ent.st_mode & 0o170000) == 0o040000:
                            lpath.mkdir(parents=True, exist_ok=True)
                            _walk_remote(rpath, lpath)
                        else:
                            lpath.parent.mkdir(parents=True, exist_ok=True)
                            sftp.get(rpath, str(lpath))

                _walk_remote(remote_path.replace("\\", "/"), local_base)
                return "", "已通过 SFTP 回退下载目录（Paramiko 流式）", 0

            cap = int(getattr(config, "SCP_PULL_MAX_BYTES", 200 * 1024 * 1024))
            err = _sftp_get_to_local_path_sync(
                host=remote_host,
                port=port,
                username=remote_user,
                auth_type=auth_type,
                password=password,
                key_path=key_path,
                private_key_pem=private_key_pem,
                remote_path=remote_path,
                local_path=local_abs,
                max_bytes=cap,
                timeout=timeout,
            )
            if err:
                return "", err, 1
            return "", "已通过 SFTP 回退下载（Paramiko 流式）", 0
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    except Exception as e:
        return "", str(e), 1
    finally:
        unpatch_banner_encoding()
        if client:
            try:
                client.close()
            except Exception:
                pass


def _run_scp_sync(
    *,
    direction: Literal["push", "pull"],
    local_path: str,
    remote_user: str,
    remote_host: str,
    remote_path: str,
    port: int,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    compress: bool,
    recursive: bool,
    legacy_rsa: bool,
    timeout: int,
) -> Tuple[str, str, int]:
    local_abs = str(Path(local_path).resolve())
    if direction == "push":
        lp = Path(local_abs)
        if not lp.exists():
            return "", f"本地路径不存在: {local_path}", 2
        if lp.is_dir() and not recursive:
            return "", "本地路径为目录，请设置 recursive=true", 2
    else:
        Path(local_abs).parent.mkdir(parents=True, exist_ok=True)

    use_password = (auth_type or "").strip().lower() == "password"
    temp_key_path: Optional[str] = None
    identity_file: Optional[str] = None

    if use_password:
        if not scp_command_available():
            out, err, code = _paramiko_transfer_sync(
                direction=direction,
                local_path=local_abs,
                remote_user=remote_user,
                remote_host=remote_host,
                remote_path=remote_path,
                port=port,
                auth_type=auth_type,
                password=password,
                key_path=key_path,
                private_key_pem=private_key_pem,
                recursive=recursive,
                timeout=timeout,
            )
            return out, err, code
    else:
        if not scp_command_available():
            return "", "当前运行环境未找到 scp 命令；Docker 镜像需安装 openssh-client，本机需安装 OpenSSH 客户端。", 127
        if private_key_pem:
            fd, temp_key_path = tempfile.mkstemp(prefix="edgeops-scp-", suffix=".key")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as wf:
                    wf.write(private_key_pem.strip() + "\n")
                os.chmod(temp_key_path, 0o600)
                identity_file = temp_key_path
            except OSError as e:
                if temp_key_path:
                    try:
                        os.unlink(temp_key_path)
                    except OSError:
                        pass
                return "", f"写入临时私钥失败: {e}", 1
        elif key_path and os.path.isfile(key_path):
            identity_file = key_path
        else:
            return "", "密钥认证缺少可用私钥", 1

    last_out, last_err, last_code = "", "", 1
    try:
        for use_legacy, _mode in _scp_attempt_modes(legacy_rsa):
            argv = _build_scp_argv(
                direction=direction,
                local_abs=local_abs,
                remote_user=remote_user,
                remote_host=remote_host,
                remote_path=remote_path,
                port=port,
                compress=compress,
                recursive=recursive,
                legacy_rsa=use_legacy,
                identity_file=identity_file,
            )

            if use_password:
                scp_ok = False
                for backend in _password_scp_backends():
                    cleanup_paths: list[str] = []
                    try:
                        out, err, code, cleanup_paths = _run_scp_with_password_backend(
                            backend=backend,
                            argv=argv,
                            password=password or "",
                            timeout=timeout,
                        )
                        last_out, last_err, last_code = out, err, code
                        if code == 0:
                            scp_ok = True
                            if backend == "askpass":
                                last_err = (err + "\n（Windows/本机通过 SSH_ASKPASS 完成密码认证）").strip()
                            break
                    finally:
                        for p in cleanup_paths:
                            try:
                                os.unlink(p)
                            except OSError:
                                pass
                if scp_ok:
                    return last_out, last_err, 0
            else:
                out, err, code = _run_subprocess(argv, env=os.environ.copy(), timeout=timeout)
                last_out, last_err, last_code = out, err, code
                if code == 0:
                    return out, err, 0

            if _mode == "modern" and _scp_error_suggests_legacy_rsa(last_err, last_out):
                continue
            break

        if use_password:
            out, err, code = _paramiko_transfer_sync(
                direction=direction,
                local_path=local_abs,
                remote_user=remote_user,
                remote_host=remote_host,
                remote_path=remote_path,
                port=port,
                auth_type=auth_type,
                password=password,
                key_path=key_path,
                private_key_pem=private_key_pem,
                recursive=recursive,
                timeout=timeout,
            )
            if code == 0:
                note = (err or "已通过 SFTP 回退完成传输（Paramiko 流式）").strip()
                return out, note, 0
            combined = (last_err or last_out or err or "").strip()
            return last_out, combined or "scp 与 SFTP 回退均失败", last_code or code

        return last_out, last_err, last_code
    finally:
        if temp_key_path:
            try:
                os.unlink(temp_key_path)
            except OSError:
                pass


async def run_scp_transfer(
    *,
    direction: Literal["push", "pull"],
    local_path: str,
    remote_user: str,
    remote_host: str,
    remote_path: str,
    port: int = 22,
    auth_type: str = "password",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    compress: bool = True,
    recursive: bool = False,
    legacy_rsa: bool = False,
    timeout: int = 600,
) -> Tuple[str, str, int]:
    """异步执行 scp 传输。成功时 exit_code=0。返回 (stdout, stderr, exit_code)。"""
    return await asyncio.to_thread(
        _run_scp_sync,
        direction=direction,
        local_path=local_path,
        remote_user=remote_user,
        remote_host=remote_host,
        remote_path=remote_path,
        port=port,
        auth_type=auth_type,
        password=password,
        key_path=key_path,
        private_key_pem=private_key_pem,
        compress=compress,
        recursive=recursive,
        legacy_rsa=legacy_rsa,
        timeout=timeout,
    )


async def probe_remote_path_kind_and_size(
    *,
    remote_host: str,
    port: int,
    remote_user: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    remote_path: str,
    timeout: int = 30,
) -> Tuple[str, Optional[int]]:
    """探测远程路径类型与大小。返回 (kind, size)；kind 为 file|dir|missing|error。"""
    from services.ssh_client import run_ssh_command

    rp = remote_path.replace("'", "'\\''")
    cmd = (
        f"RP='{rp}'; "
        "if [ -f \"$RP\" ]; then echo FILE:$(wc -c < \"$RP\" | tr -d ' '); "
        "elif [ -d \"$RP\" ]; then echo DIR; "
        "else echo MISSING; fi"
    )
    out, err, code = await run_ssh_command(
        host=remote_host,
        port=port,
        username=remote_user,
        auth_type=auth_type,
        password=password,
        key_path=key_path,
        private_key_pem=private_key_pem,
        command=cmd,
        timeout=timeout,
    )
    line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
    if line == "DIR":
        return "dir", None
    if line == "MISSING":
        return "missing", None
    if line.startswith("FILE:"):
        try:
            return "file", int(line.split(":", 1)[1].strip())
        except (TypeError, ValueError):
            return "file", None
    if code != 0:
        return "error", None
    return "error", None
