"""生成 RSA/ECC 密钥对（设计：凭证支持自动生成公私钥）"""
from typing import Literal, Tuple

from cryptography.hazmat.primitives.asymmetric import rsa, ec
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend


def generate_rsa_key(bits: int = 2048) -> Tuple[str, str]:
    """生成 RSA 密钥对，返回 (private_pem, public_pem) 字符串。"""
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=bits,
        backend=default_backend(),
    )
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def generate_ecc_key(curve_name: Literal["secp256r1", "secp384r1", "secp521r1"] = "secp256r1") -> Tuple[str, str]:
    """生成 ECC 密钥对，返回 (private_pem, public_pem)。"""
    curve = getattr(ec, curve_name.upper(), ec.SECP256R1())
    key = ec.generate_private_key(curve, default_backend())
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem
