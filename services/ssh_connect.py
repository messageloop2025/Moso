"""Paramiko SSH 连接公共封装：优先现代算法，失败时自动回退 ssh-rsa（OpenWrt / 老旧 Linux）。"""
from __future__ import annotations

import logging
import socket
from typing import Any, Optional

import paramiko

import config
from services.ssh_key_loader import load_private_key_pem

logger = logging.getLogger("edgeops.ssh")


def ssh_legacy_rsa_fallback_enabled() -> bool:
    """是否允许在首次连接失败后，自动以 legacy ssh-rsa 模式重试。"""
    return getattr(config, "SSH_LEGACY_RSA", True)


def ssh_try_legacy_rsa_first() -> bool:
    """是否优先使用 legacy ssh-rsa（适用于已知仅支持 ssh-rsa 的设备）。"""
    return getattr(config, "SSH_TRY_LEGACY_RSA_FIRST", False)


def ssh_legacy_disabled_algorithms() -> dict[str, list[str]]:
    """等价于 OpenSSH 的 HostKeyAlgorithms=+ssh-rsa / PubkeyAcceptedAlgorithms=+ssh-rsa。"""
    return {
        "keys": ["rsa-sha2-256", "rsa-sha2-512"],
        "pubkeys": ["rsa-sha2-256", "rsa-sha2-512"],
    }


def _should_retry_with_legacy_rsa(e: Exception, auth_type: str) -> bool:
    """判断是否属于「现代算法连不上、可尝试 legacy ssh-rsa」的场景。"""
    if isinstance(e, paramiko.ssh_exception.IncompatiblePeer):
        return True
    if isinstance(e, paramiko.ssh_exception.SSHException):
        msg = (str(e) or "").lower()
        if "no acceptable host key" in msg or "incompatible ssh peer" in msg:
            return True
    # 老旧 dropbear 常不支持 server-sig-algs，RSA 公钥认证可能在 auth 阶段失败
    if isinstance(e, paramiko.ssh_exception.AuthenticationException):
        return auth_type in ("key", "key_pair")
    return False


def build_ssh_connect_kwargs(
    *,
    hostname: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    timeout: int = 30,
    use_legacy_rsa: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造 paramiko SSHClient.connect 参数字典（不含 client 本身）。"""
    connect_kw: dict[str, Any] = {
        "hostname": hostname,
        "port": port,
        "username": username,
        "timeout": timeout,
        "banner_timeout": timeout,
        "look_for_keys": False,
        "allow_agent": False,
    }
    if use_legacy_rsa:
        connect_kw["disabled_algorithms"] = ssh_legacy_disabled_algorithms()

    if auth_type in ("key", "key_pair") and (private_key_pem or key_path):
        if private_key_pem:
            connect_kw["pkey"] = load_private_key_pem(private_key_pem)
        else:
            connect_kw["key_filename"] = key_path
    else:
        connect_kw["password"] = password or ""

    if extra:
        connect_kw.update(extra)
    return connect_kw


def _close_client_quietly(client: Optional[paramiko.SSHClient]) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


def _new_ssh_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client


def _connect_attempt_modes(auth_type: str) -> list[tuple[bool, str]]:
    """返回 (use_legacy_rsa, mode_label) 列表。"""
    if not ssh_legacy_rsa_fallback_enabled():
        return [(False, "modern")]
    if ssh_try_legacy_rsa_first():
        return [(True, "legacy"), (False, "modern")]
    return [(False, "modern"), (True, "legacy")]


def establish_ssh_client(
    *,
    hostname: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    private_key_pem: Optional[str] = None,
    timeout: int = 30,
    **extra: Any,
) -> paramiko.SSHClient:
    """建立 SSH 连接并返回已连接的 SSHClient（每次尝试使用全新 client 实例）。"""
    base = dict(
        hostname=hostname,
        port=port,
        username=username,
        auth_type=auth_type,
        password=password,
        key_path=key_path,
        private_key_pem=private_key_pem,
        timeout=timeout,
        extra=extra or None,
    )
    modes = _connect_attempt_modes(auth_type)
    last_err: Optional[Exception] = None

    for idx, (use_legacy, mode_label) in enumerate(modes):
        client = _new_ssh_client()
        try:
            client.connect(**build_ssh_connect_kwargs(**base, use_legacy_rsa=use_legacy))
            if use_legacy:
                logger.info("SSH connected via legacy ssh-rsa: %s:%s user=%s", hostname, port, username)
            return client
        except Exception as e:
            last_err = e
            _close_client_quietly(client)
            is_last = idx >= len(modes) - 1
            if is_last:
                break
            if mode_label == "modern" and _should_retry_with_legacy_rsa(e, auth_type):
                logger.info(
                    "SSH connect retry with legacy ssh-rsa: %s:%s user=%s (%s: %s)",
                    hostname,
                    port,
                    username,
                    type(e).__name__,
                    e,
                )
                continue
            if mode_label == "legacy" and ssh_try_legacy_rsa_first():
                logger.info(
                    "SSH legacy-first failed, retry modern: %s:%s user=%s (%s: %s)",
                    hostname,
                    port,
                    username,
                    type(e).__name__,
                    e,
                )
                continue
            break

    assert last_err is not None
    if not isinstance(last_err, paramiko.ssh_exception.AuthenticationException):
        logger.warning(
            "SSH connect failed after %s attempt(s): %s:%s user=%s last=%s: %s",
            len(modes),
            hostname,
            port,
            username,
            type(last_err).__name__,
            last_err,
        )
    raise last_err


def friendly_ssh_error(e: Exception) -> str:
    """把常见 SSH/网络异常转成用户可读提示。"""
    if isinstance(e, paramiko.ssh_exception.AuthenticationException):
        return "SSH 认证失败：用户名/密码或密钥不正确，或该账号不允许登录。请检查主机凭证配置后重试。"
    if isinstance(e, paramiko.ssh_exception.BadAuthenticationType):
        return "SSH 认证方式不被目标主机接受。请在凭证中切换为正确的认证方式（密码/密钥）后重试。"
    if isinstance(e, paramiko.ssh_exception.IncompatiblePeer):
        try:
            major = int((getattr(paramiko, "__version__", "0") or "0").split(".")[0])
        except (TypeError, ValueError):
            major = 0
        if major >= 5:
            return (
                "SSH 算法不兼容：当前 Paramiko 5.x 已移除 ssh-rsa 支持，无法连接 OpenWrt 等老旧设备。"
                "请将 requirements.txt 中 paramiko 锁定为 <5.0 后重建 Docker 镜像。"
            )
        return (
            "SSH 算法不兼容：目标主机可能仅支持 ssh-rsa（常见于 OpenWrt / 老旧 dropbear）。"
            "系统已尝试兼容模式仍失败，请确认 毛竹 服务端能访问该主机、端口与凭证正确。"
        )

    if isinstance(e, (socket.timeout, TimeoutError)):
        return "SSH 连接超时：请检查主机 IP/端口、网络连通性、防火墙/安全组规则后重试。"
    if isinstance(e, EOFError):
        return "SSH 连接在握手或通信时被对方关闭：可能网络不稳定或主机 SSH 服务异常。请稍后重试或检查主机与网络。"
    if isinstance(e, ConnectionRefusedError):
        return "SSH 连接被拒绝：目标端口未开放或 SSH 服务未运行。请检查端口、SSH 服务状态与防火墙后重试。"

    if isinstance(e, paramiko.ssh_exception.SSHException):
        msg = str(e) or ""
        low = msg.lower()
        if "no acceptable host key" in low or "incompatible ssh peer" in low:
            return (
                "SSH 算法不兼容：目标主机可能仅支持 ssh-rsa（常见于 OpenWrt / 老旧 Linux）。"
                "系统已尝试兼容模式仍失败，请确认端口、凭证正确，且 毛竹 服务已更新。"
            )
        if "not a valid" in low or "invalid key" in low or "could not deserialize" in low:
            return "SSH 密钥不可用：私钥格式/口令/路径可能有误。请检查密钥内容或重新上传后重试。"
        if "error reading ssh protocol banner" in low or "banner" in low:
            return "SSH 握手失败：未收到 SSH Banner（可能端口不是 SSH、被中间设备拦截或网络不稳定）。请检查端口与网络后重试。"
        return "SSH 握手失败：请检查目标是否为 SSH 服务、端口是否正确、以及网络是否稳定。"

    if isinstance(e, OSError):
        return "SSH 连接失败：网络/主机不可达或地址解析失败。请检查主机地址与网络后重试。"

    return "SSH 连接失败：请检查主机地址、端口与凭证配置后重试。"
