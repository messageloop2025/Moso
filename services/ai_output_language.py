"""AI 回复语言策略：与用户输入语言一致；无法判断时按用户设置 → 站点设置 → 浏览器；显式要求优先。"""
from __future__ import annotations

import re

# 与前端 I18n 支持一致，并便于写入 system 提示
ALLOWED_LOCALE_CODES = frozenset({"en", "zh-CN"})


def normalize_ui_locale(s: str | None) -> str:
    t = (s or "").strip().replace("_", "-")
    if not t:
        return "zh-CN"
    lo = t.lower()
    if lo == "en" or lo.startswith("en-"):
        return "en"
    if lo in ("zh", "zh-cn", "zh-hans", "zh-sg", "zh-hk", "zh-tw", "zh-mo"):
        return "zh-CN"
    if lo.startswith("zh-"):
        return "zh-CN"
    return "en"


def parse_explicit_output_language(user_message: str) -> str | None:
    """若用户显式要求某种输出语言，返回 en / zh-CN；无法识别则 None。"""
    m = (user_message or "")
    if not m.strip():
        return None
    low = m.lower()
    if any(
        p in low
        for p in (
            "in english",
            "answer in english",
            "reply in english",
            "respond in english",
            "use english",
            "english only",
        )
    ):
        return "en"
    if any(
        p in m
        for p in (
            "用英文",
            "用英语",
            "英文回答",
            "英语回答",
            "请用英文",
            "回答请用英文",
        )
    ):
        return "en"
    if any(
        p in low
        for p in (
            "in chinese",
            "chinese only",
        )
    ):
        return "zh-CN"
    if any(
        p in m
        for p in (
            "用中文",
            "中文回答",
            "请用中文",
            "以中文",
        )
    ):
        return "zh-CN"
    if re.search(
        r"(用|以|改|切|换)(繁体?)?(中文|汉语|華文)",
        m,
    ) or re.search(
        r"(回复|答|说|寫|写|输出|显示).{0,6}(繁体?)?(中文|汉语)",
        m,
    ):
        return "zh-CN"
    return None


def infer_input_language_for_output(user_message: str) -> str:
    """从本轮用户消息推测自然语言；返回 zh-CN / en / undetermined。"""
    s = (user_message or "").strip()
    if len(s) < 2:
        return "undetermined"
    cjk = 0
    latin = 0
    for c in s:
        if "\u4e00" <= c <= "\u9fff":
            cjk += 1
        elif "a" <= c.lower() <= "z":
            latin += 1
    if cjk >= 2 and cjk >= latin * 0.2 + 0.1:
        return "zh-CN"
    if cjk == 0 and latin >= 8 and len(s) > 6:
        return "en"
    if cjk >= 1:
        return "zh-CN"
    if latin >= 4 and len(s) > 4:
        return "en"
    return "undetermined"


def _clean_locale_for_chain(raw: str | None) -> str:
    v = (raw or "").strip()
    if not v:
        return ""
    n = normalize_ui_locale(v)
    return n if n in ALLOWED_LOCALE_CODES else ""


def resolve_output_language(
    user_message: str,
    *,
    user_output_locale: str,
    global_output_locale: str,
    browser_ui_locale: str | None,
) -> tuple[str, str, str]:
    """
    返回 (final_locale, reason_code, reason_label_zh)
    reason_code: explicit | from_input | user_setting | site_setting | browser | hard_default
    """
    exp = parse_explicit_output_language(user_message)
    if exp and exp in ALLOWED_LOCALE_CODES:
        return exp, "explicit", "用户显式指定"
    det = infer_input_language_for_output(user_message)
    if det in ALLOWED_LOCALE_CODES and det != "undetermined":
        return det, "from_input", "与用户本条输入语言一致"
    u = _clean_locale_for_chain(user_output_locale)
    if u:
        return u, "user_setting", "用户个人默认（设置）"
    g = _clean_locale_for_chain(global_output_locale)
    if g:
        return g, "site_setting", "站点默认（全局设置）"
    b = _clean_locale_for_chain(browser_ui_locale) if browser_ui_locale else ""
    if b:
        return b, "browser", "界面语言/浏览器"
    return "zh-CN", "hard_default", "未配置时兜底"


def build_output_language_system_section(
    user_message: str,
    *,
    user_output_locale: str,
    global_output_locale: str,
    browser_ui_locale: str | None,
) -> str:
    """
    注入主 AI system 的「回复语言策略」段（中英双语，便于各模型遵守）。
    """
    final, reason_code, reason_zh = resolve_output_language(
        user_message,
        user_output_locale=user_output_locale,
        global_output_locale=global_output_locale,
        browser_ui_locale=browser_ui_locale,
    )
    det = infer_input_language_for_output(user_message)
    det_en = "Chinese (Simplified context)" if det == "zh-CN" else (
        "English" if det == "en" else "undetermined / mixed / too short"
    )
    # reason labels in English for the model
    _reason_en = {
        "explicit": "user explicitly requested a language in the message (highest priority).",
        "from_input": "inferred from the language of the user's current message.",
        "user_setting": "user default in settings (used when the message language is unclear).",
        "site_setting": "site-wide default in global settings.",
        "browser": "UI / browser language (request header or client-reported locale).",
        "hard_default": "fallback when nothing else is set.",
    }.get(reason_code, reason_code)
    return f"""
## Response language policy (HIGHEST PRIORITY — follow over any generic "reply in Chinese" elsewhere)

**English**
1) If the user **explicitly** asks for a specific language (e.g. "in English", "用中文回答", "answer in French"), you MUST use **that** language for the entire natural-language answer (code/commands/logs stay as-is).
2) Otherwise, if the user's **current** message is clearly in one language, **match** that language.
3) If the input is **too short, ambiguous, or language cannot be told**, use the **effective default** below.
4) **Effective default for this turn**: **{final}** (reason: {_reason_en})
5) **Planning / reasoning / step narration** that the user can see—including streamed "thinking", pre-tool explanations, interim plans, and tool-step summaries—MUST use the **same natural language** as your final reply under this policy. Do not switch languages mid-stream unless the user explicitly asked for mixed languages. Tool names, file paths, shell commands, identifiers, and raw log lines stay unchanged.

**Inferred from this user message (for step 2)**: {det_en} (`{det}`)

**中文（与上一段等价）**
- 若用户**显式**要求某种输出语言，必须**全程**按该语言写自然语言（代码/命令/原始日志/标识符不强行翻译）。
- 否则在可判断时，**与用户本条消息的主要语言**保持一致。
- 若无法从本条判断，则本回合采用 **{final}**（依据：{reason_zh}，内部代码 `{reason_code}`）。
- **对用户可见的规划与推理**（含流式思考、工具调用前的说明、中间步骤小结）须与**本条策略确定的自然语言**一致，不要无故中英混切；工具名、路径、命令与日志原文保持原样。

**本回合必须使用的自然语言输出：{"简体中文为主" if final == "zh-CN" else "English" if final == "en" else final}。**
""".strip()
