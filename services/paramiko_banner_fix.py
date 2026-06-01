"""SSH 连接时服务器或认证消息可能含非 UTF-8（如中文 GBK），Paramiko 默认用 UTF-8 解码会报错。
在 connect 前临时改用 errors='replace' 解码，避免 'utf-8' codec can't decode byte 0xaa 等异常。
注意：多个模块使用 from paramiko.util import u，需同时 patch 这些模块的 u 才能对
banner、密钥交换、公钥认证、以及加载 OpenSSH 私钥时的解码全流程生效。"""
import paramiko.util
import paramiko.packet
import paramiko.message
import paramiko.auth_handler
import paramiko.pkey


_original_u = paramiko.util.u


def _u_replace(s, encoding="utf8"):
    if isinstance(s, bytes):
        return s.decode(encoding, errors="replace")
    return s


def patch_banner_encoding():
    paramiko.util.u = _u_replace
    paramiko.packet.u = _u_replace
    paramiko.message.u = _u_replace
    paramiko.auth_handler.u = _u_replace
    paramiko.pkey.u = _u_replace


def unpatch_banner_encoding():
    paramiko.util.u = _original_u
    paramiko.packet.u = _original_u
    paramiko.message.u = _original_u
    paramiko.auth_handler.u = _original_u
    paramiko.pkey.u = _original_u
