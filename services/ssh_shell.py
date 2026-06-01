"""SSH 交互式 Shell（PTY），用于 WebSocket 终端桥接与 AI 介入"""
from typing import Optional

import paramiko

from services.paramiko_banner_fix import patch_banner_encoding, unpatch_banner_encoding
from services.ssh_connect import establish_ssh_client


def open_shell_session(
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: Optional[str],
    key_path: Optional[str],
    private_key_pem: Optional[str],
    timeout: int = 30,
) -> tuple:
    """
    打开 SSH 交互式 Shell（PTY），返回 (client, channel)。
    调用方负责在不用时关闭 channel 和 client。
    """
    try:
        patch_banner_encoding()
        client = establish_ssh_client(
            hostname=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password=password,
            key_path=key_path,
            private_key_pem=private_key_pem,
            timeout=timeout,
        )
    finally:
        unpatch_banner_encoding()
    transport = client.get_transport()
    if transport:
        transport.set_keepalive(30)
    channel = transport.open_session()
    channel.get_pty(term="xterm", width=80, height=24)
    channel.invoke_shell()
    return client, channel
