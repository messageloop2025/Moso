"""终端输入：控制键占位符展开，供 SSH 终端与本机终端共用。"""
import re

# 常用控制键：<Ctrl+C> -> 对应 ASCII 控制字符（Ctrl+字母 = 0x01+ord(letter)-ord('a')）
_CONTROL_KEYS = {
    "ctrl+c": "\x03",   # 中断 (SIGINT)
    "ctrl+d": "\x04",   # EOF
    "ctrl+l": "\x0c",   # 清屏
    "ctrl+r": "\x12",   # 反向搜索 (bash)
    "ctrl+u": "\x15",   # 删到行首 (bash)
    "ctrl+z": "\x1a",   # 挂起 (SIGTSTP)
    "ctrl+\\": "\x1c",  # 退出 (SIGQUIT)
    "ctrl+[": "\x1b",   # Escape
}


def expand_control_keys(text: str) -> str:
    """将 text 中的 <Ctrl+X> 占位符替换为实际控制字符。不区分大小写。"""
    if not text:
        return text
    result = text
    # 按长度降序替换，避免 <Ctrl+C> 被部分匹配
    for key, char in sorted(_CONTROL_KEYS.items(), key=lambda x: -len(x[0])):
        # 匹配 <Ctrl+C>、<Ctrl+c>、<Ctrl+[> 等
        pattern = r"<" + re.escape(key) + r">"
        result = re.sub(pattern, char, result, flags=re.IGNORECASE)
    return result


def is_control_only(text: str) -> bool:
    """若 text 仅由控制字符组成（不含换行/回车/制表符等“可显示”控制符），返回 True，此时不应自动补换行。"""
    if not text:
        return False
    for c in text:
        o = ord(c)
        # 仅允许典型“按键”控制符 0x01-0x1f（不含 0x09 \t, 0x0a \n, 0x0d \r）
        if o >= 32:
            return False
        if o in (9, 10, 13):  # \t \n \r 视为“可提交”字符，不当作纯控制键
            return False
    return True
