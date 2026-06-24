"""检测终端/SSH 通道输出是否处于等待密码输入的状态。"""
from __future__ import annotations

import re

# 常见 sudo / SSH / DB 密码提示（末行或尾部窗口匹配）
_PASSWORD_PROMPT_PATTERNS = (
    re.compile(r"\[sudo\]\s+password\s+for\s+", re.I),
    re.compile(r"\bsudo:\s*.*password", re.I),
    re.compile(r"password\s*:\s*$", re.I),
    re.compile(r"passphrase\s+for\s+key", re.I),
    re.compile(r"enter\s+password\s+for\s+", re.I),
    re.compile(r"\'(?:password|passwd)\'\s*:\s*$", re.I),
    re.compile(r"mysql.*password:", re.I),
    re.compile(r"postgresql.*password\s+for\s+user", re.I),
    re.compile(r"redis.*auth", re.I),
)


def tail_text_for_prompt_check(text: str, max_chars: int = 1200) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(s) <= max_chars:
        return s
    return s[-max_chars:]


def looks_like_password_prompt(text: str) -> bool:
    """判断终端尾部是否像在等待密码/口令输入。"""
    tail = tail_text_for_prompt_check(text)
    if not tail.strip():
        return False
    last_lines = [ln.strip() for ln in tail.split("\n") if ln.strip()]
    window = "\n".join(last_lines[-4:]) if last_lines else tail
    for pat in _PASSWORD_PROMPT_PATTERNS:
        if pat.search(window):
            return True
    return False


def infer_service_from_prompt(text: str) -> str:
    """从提示文本粗略推断 service 类型，供凭证匹配。"""
    tail = tail_text_for_prompt_check(text).lower()
    if "sudo" in tail or "[sudo]" in tail:
        return "sudo"
    if "mysql" in tail:
        return "mysql"
    if "postgres" in tail or "psql" in tail:
        return "postgres"
    if "redis" in tail:
        return "redis"
    if "passphrase for key" in tail or "enter passphrase" in tail:
        return "ssh_key"
    if "password:" in tail and ("ssh" in tail or "authenticity" in tail):
        return "ssh"
    return "sudo"
