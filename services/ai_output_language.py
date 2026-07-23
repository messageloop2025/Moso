"""AI 回复语言策略：显式要求优先；否则按用户设置 → 站点设置 → 界面/浏览器语言。

推理（reasoning / thinking / CoT）与最终叙述使用同一自然语言，不因英文日志/术语改用英文。
"""
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


def _strip_non_conversational_for_lang_infer(s: str) -> str:
    """去掉代码块、典型英文日志行后再判语言，避免「中文指令 + 英文报错粘贴」被误判为英文。"""
    t = (s or "").strip()
    if not t:
        return ""
    t = re.sub(r"```[\s\S]*?```", " ", t)
    kept: list[str] = []
    for line in t.splitlines():
        ln = line.strip()
        if not ln:
            continue
        if re.match(
            r"(?i)^(error|warning|info|debug|traceback|permission denied|failed|note:|checking for|found\s|not found|configure:|meson|ninja|\$ )",
            ln,
        ):
            continue
        if re.match(r"^[\x00-\x7F\s\W]+$", ln) and len(ln) > 40 and ln.count(" ") >= 4:
            continue
        kept.append(ln)
    out = "\n".join(kept).strip()
    return out or (s or "").strip()


def infer_input_language_for_output(user_message: str) -> str:
    """从本轮用户消息推测自然语言；返回 zh-CN / en / undetermined。"""
    s = _strip_non_conversational_for_lang_infer(user_message)
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

    优先级：
    1. 用户本条消息显式指定语言
    2. 用户个人「回复语言」设置
    3. 站点全局设置
    4. 界面/浏览器语言（前端 ui_locale）
    5. 从本条输入推测（仅作兜底）
    6. 硬默认 zh-CN

    不再把「跟输入语言一致」放在浏览器语言之前——运维对话常夹杂英文术语/日志，
    若优先跟输入会导致推理大段英文化。
    """
    exp = parse_explicit_output_language(user_message)
    if exp and exp in ALLOWED_LOCALE_CODES:
        return exp, "explicit", "用户显式指定"
    u = _clean_locale_for_chain(user_output_locale)
    if u:
        return u, "user_setting", "用户个人默认（设置）"
    g = _clean_locale_for_chain(global_output_locale)
    if g:
        return g, "site_setting", "站点默认（全局设置）"
    b = _clean_locale_for_chain(browser_ui_locale) if browser_ui_locale else ""
    if b:
        return b, "browser", "界面语言/浏览器"
    det = infer_input_language_for_output(user_message)
    if det in ALLOWED_LOCALE_CODES and det != "undetermined":
        return det, "from_input", "与用户本条输入语言一致（兜底）"
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
    强调：推理/thinking 与最终叙述使用同一自然语言，禁止因英文上下文改用英文推理。
    """
    final, reason_code, reason_zh = resolve_output_language(
        user_message,
        user_output_locale=user_output_locale,
        global_output_locale=global_output_locale,
        browser_ui_locale=browser_ui_locale,
    )
    _reason_en = {
        "explicit": "user explicitly requested a language in the message (highest priority).",
        "from_input": "fallback: inferred from the language of the user's current message.",
        "user_setting": "user default in AI settings.",
        "site_setting": "site-wide default in global settings.",
        "browser": "UI / browser / device language reported by the client.",
        "hard_default": "fallback when nothing else is set.",
    }.get(reason_code, reason_code)
    lang_name_zh = "简体中文" if final == "zh-CN" else ("English" if final == "en" else final)
    lang_name_en = "Simplified Chinese" if final == "zh-CN" else ("English" if final == "en" else final)
    zh_first = final == "zh-CN"

    zh_block = f"""**中文（本回合必须遵守）**
- 若用户**显式**要求某种输出语言，必须**全程**按该语言写自然语言（代码/命令/原始日志/标识符不强行翻译）。
- 否则本回合统一使用：**{lang_name_zh}**（依据：{reason_zh}，`{reason_code}`）。来源为用户设置 → 站点设置 → **界面/浏览器语言**，不是「跟英文日志走」。
- **推理 / 思考 / 规划 / 步骤说明**（含厂商 `reasoning` / `reasoning_content` / `thinking` 流式字段、工具调用前的内心独白、中间小结）**必须**用上述自然语言书写；禁止无故中英混切，禁止因终端输出、配置、报错、包名是英文就改用英文整段推理。
- 命令、路径、配置键、日志原文、工具名可保持英文引用；解释与结论仍用 {lang_name_zh}。
- 即使用户消息很短、夹杂英文专有名词，或上下文几乎全是英文工具输出，**仍不要**把推理改成英文，除非用户显式要求英文。

**本回合自然语言输出（含推理）：{lang_name_zh}。**"""

    en_block = f"""**English (same rules)**
1) If the user **explicitly** asks for a language, use that language for all natural-language text.
2) Otherwise use **{lang_name_en}** for this turn (reason: {_reason_en}). Chain: user setting → site setting → **UI/browser locale** — do **not** switch to English because logs/tools are English.
3) **Reasoning / thinking / planning / step narration** (including vendor `reasoning` / `reasoning_content` / `thinking` streams) MUST be in **{lang_name_en}**. Do not mix languages. Do not rewrite analysis in English just because configs, errors, or package names are English.
4) Tool names, paths, shell commands, identifiers, and raw log lines may stay as-is; your explanation stays in {lang_name_en}.
5) Short user messages with English proper nouns still do **not** authorize English reasoning unless explicitly requested.

**Natural language for this turn (including reasoning): {lang_name_en}.**"""

    body = (zh_block + "\n\n" + en_block) if zh_first else (en_block + "\n\n" + zh_block)
    return f"""## Response language policy (HIGHEST PRIORITY — overrides generic bilingual habits)

{body}
""".strip()
