"""凭证相关工具：私钥 PEM 规范化，避免 AI/API 传入格式导致存坏或登录失败。"""
import re
from typing import Optional


def normalize_private_key_pem(raw: Optional[str]) -> Optional[str]:
    """
    规范化私钥 PEM 字符串再入库，避免因格式问题导致无法使用。
    - 去除首尾空白、去掉可能的 Markdown 代码块包裹（```...```）
    - 将字面量 \\n 转为真实换行（AI/JSON 常把换行写成 \\n）
    - 保持 -----BEGIN...----- 与 -----END...----- 及中间 base64 不变
    若 raw 为空或 None，返回 None；否则返回规范化后的字符串。
    """
    if raw is None:
        return None
    s = (raw or "").strip()
    if not s:
        return None
    # 去掉 Markdown 代码块（AI 可能返回 ```pem\n...\n``` 或 ```\n...\n```）
    for _ in range(2):
        m = re.match(r"^```(?:\w*)\s*\n?(.*?)\n?```\s*$", s, re.DOTALL)
        if m:
            s = m.group(1).strip()
        else:
            break
    # 字面量 \n 转为真实换行（例如 AI 工具参数里有时会转义）
    s = s.replace("\\n", "\n")
    return s if s else None
