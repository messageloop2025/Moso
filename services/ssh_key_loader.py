"""从 PEM 字符串加载 SSH 私钥，支持 RSA / ECDSA / Ed25519，避免「Invalid key curve identifier」等错误。"""
from io import StringIO
from typing import Optional

import paramiko


def load_private_key_pem(pem: str):
    """
    从 PEM 字符串加载私钥，依次尝试 RSA → ECDSA → Ed25519。
    若私钥为 Ed25519（如 ssh-keygen -t ed25519），仅试 RSA/ECDSA 会报 Invalid key curve identifier。
    返回 paramiko.PKey 子类实例；全部失败则抛出最后一次的异常。
    """
    key_classes = [
        paramiko.RSAKey,
        paramiko.ECDSAKey,
    ]
    try:
        key_classes.append(paramiko.Ed25519Key)
    except AttributeError:
        pass
    last_err = None
    for key_cls in key_classes:
        try:
            return key_cls.from_private_key(StringIO(pem))
        except Exception as e:
            last_err = e
    if last_err is not None:
        raise last_err
    raise ValueError("无法解析私钥")
