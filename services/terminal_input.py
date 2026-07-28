"""终端输入：控制键占位符展开，供 SSH 终端与本机终端共用。"""
import re

# 占位符：<Ctrl+X>、<Alt+M>、<Up>、<F1> 等，见 expand_control_keys 文档
_PLACEHOLDER_RE = re.compile(r"<([^<>]+)>")

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "c": "ctrl",
    "alt": "alt",
    "meta": "alt",
    "option": "alt",
    "m": "alt",
    "shift": "shift",
    "s": "shift",
}

# 无修饰键时的命名键（大小写不敏感）
_NAMED_KEYS = {
    "enter": "\n",
    "return": "\n",
    "cr": "\r",
    "lf": "\n",
    "tab": "\t",
    "esc": "\x1b",
    "escape": "\x1b",
    "space": " ",
    "backspace": "\x7f",
    "bspace": "\x7f",
    "bs": "\x08",
    "del": "\x1b[3~",
    "delete": "\x1b[3~",
    "insert": "\x1b[2~",
    "ins": "\x1b[2~",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pgup": "\x1b[5~",
    "pageup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "pagedown": "\x1b[6~",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
}

# CSI 方向/编辑键（可叠加 Shift/Alt/Ctrl 修饰）
_CSI_KEYS = {
    "up": "A",
    "down": "B",
    "right": "C",
    "left": "D",
    "home": "H",
    "end": "F",
    "insert": "2~",
    "ins": "2~",
    "delete": "3~",
    "del": "3~",
    "pgup": "5~",
    "pageup": "5~",
    "pgdn": "6~",
    "pagedown": "6~",
}

# xterm SS3（F1–F4）与 CSI（F5–F12）
_F_KEYS = {
    1: "\x1bOP",
    2: "\x1bOQ",
    3: "\x1bOR",
    4: "\x1bOS",
    5: "\x1b[15~",
    6: "\x1b[17~",
    7: "\x1b[18~",
    8: "\x1b[19~",
    9: "\x1b[20~",
    10: "\x1b[21~",
    11: "\x1b[23~",
    12: "\x1b[24~",
}

# Ctrl+符号（非字母）
_CTRL_SYMBOLS = {
    "@": 0,
    "`": 0,
    " ": 0,
    "[": 27,
    "{": 27,
    "\\": 28,
    "|": 28,
    "]": 29,
    "}": 29,
    "^": 30,
    "~": 30,
    "_": 31,
    "-": 31,
    "?": 127,
}


def _modifier_code(modifiers: frozenset[str]) -> int:
    """xterm 修饰键编码：1 + shift(1) + alt(2) + ctrl(4)。"""
    code = 1
    if "shift" in modifiers:
        code += 1
    if "alt" in modifiers:
        code += 2
    if "ctrl" in modifiers:
        code += 4
    return code


def _ctrl_char(key: str) -> str | None:
    """将单键映射为 Ctrl 组合对应的控制字符。"""
    if len(key) != 1:
        return None
    lower = key.lower()
    if "a" <= lower <= "z":
        return chr(ord(lower) - ord("a") + 1)
    if key in _CTRL_SYMBOLS:
        return chr(_CTRL_SYMBOLS[key])
    if key.isdigit():
        return chr(0 if key == "0" else int(key))
    return None


def _apply_alt(seq: str, modifiers: frozenset[str]) -> str:
    if "alt" in modifiers:
        return "\x1b" + seq
    return seq


def _csi_with_modifiers(suffix: str, modifiers: frozenset[str]) -> str:
    mod = _modifier_code(modifiers)
    if mod == 1:
        return f"\x1b[{suffix}"
    # 箭头/ Home/End：1;{mod}X；Insert/Delete/PgUp/PgDn：{n};{mod}~
    if suffix.endswith("~"):
        num = suffix[:-1]
        return f"\x1b[{num};{mod}~"
    return f"\x1b[1;{mod}{suffix}"


def _f_key_seq(num: int, modifiers: frozenset[str]) -> str | None:
    if num < 1 or num > 12:
        return None
    mod = _modifier_code(modifiers)
    if mod == 1:
        return _F_KEYS[num]
    # xterm：F1–F4 带修饰用 CSI；F5+ 用 [1;mod P] 等形式
    if num <= 4:
        base = ord("P") + (num - 1)
        return f"\x1b[1;{mod}{chr(base)}"
    fn = {5: 15, 6: 17, 7: 18, 8: 19, 9: 20, 10: 21, 11: 23, 12: 24}[num]
    return f"\x1b[{fn};{mod}~"


def _expand_key_parts(parts: list[str]) -> str | None:
    if not parts:
        return None

    modifiers: set[str] = set()
    for raw_mod in parts[:-1]:
        mod = _MODIFIER_ALIASES.get(raw_mod.strip().lower())
        if mod is None:
            return None
        modifiers.add(mod)
    mod_set = frozenset(modifiers)

    key = parts[-1].strip()
    if not key:
        return None
    key_lower = key.lower()

    # Shift+Tab → 反向 Tab
    if key_lower == "tab" and "shift" in mod_set:
        base = "\x1b[Z"
        return _apply_alt(base, mod_set - {"shift"})

    # F1–F12
    if key_lower.startswith("f") and key_lower[1:].isdigit():
        return _f_key_seq(int(key_lower[1:]), mod_set)

    # 命名 CSI 键（Up/Down/Home/…，可叠加 Ctrl/Shift/Alt）
    if key_lower in _CSI_KEYS:
        non_alt_mods = mod_set - {"alt"}
        if non_alt_mods:
            seq = _csi_with_modifiers(_CSI_KEYS[key_lower], non_alt_mods)
        else:
            seq = _NAMED_KEYS[key_lower]
        return _apply_alt(seq, mod_set & {"alt"})

    # 纯命名键（无修饰）
    if not mod_set and key_lower in _NAMED_KEYS:
        return _NAMED_KEYS[key_lower]

    # Ctrl + 单字符（含 nano/vi 所需的 Ctrl+X 等全字母表）
    if "ctrl" in mod_set and len(key) == 1 and mod_set <= {"ctrl", "alt", "shift"}:
        ctrl_key = key
        if "shift" in mod_set and key.isalpha():
            ctrl_key = key.upper()
        seq = _ctrl_char(ctrl_key)
        if seq is not None:
            return _apply_alt(seq, mod_set - {"ctrl", "shift"})

    # Alt/Meta + 单字符（未匹配 Ctrl 时）
    if mod_set == {"alt"} and len(key) == 1:
        return "\x1b" + key

    # Shift + 单字符 → 大写（可打印键）
    if mod_set == {"shift"} and len(key) == 1:
        return key.upper()

    return None


def _expand_placeholder(inner: str) -> str | None:
    parts = [p.strip() for p in inner.split("+") if p.strip()]
    return _expand_key_parts(parts)


def expand_control_keys(text: str) -> str:
    """将 text 中的 `<…>` 占位符替换为终端控制序列。

    支持（大小写不敏感，``+`` 连接修饰键）：

    - **Ctrl**：``<Ctrl+A>`` … ``<Ctrl+Z>`` 及 ``<Ctrl+[>``、``<Ctrl+\\>`` 等符号；
      覆盖 nano（如 ``<Ctrl+X>``）、less、bash 等 TUI 快捷键。
    - **Alt/Meta**：``<Alt+M>`` → ESC + ``m``；可与 Ctrl 组合 ``<Ctrl+Alt+X>``。
    - **Shift**：``<Shift+Tab>`` → 反向 Tab；``<Shift+F6>`` 等功能键修饰。
    - **命名键**：``<Enter>``、``<Tab>``、``<Esc>``、``<Backspace>``、``<Delete>``、
      ``<Up>``/``<Down>``/``<Left>``/``<Right>``、``<Home>``/``<End>``、``<PgUp>``/``<PgDn>``。
    - **功能键**：``<F1>`` … ``<F12>``（xterm 序列；可叠加 Ctrl/Alt/Shift）。

    未识别的 ``<…>`` 原样保留。
    """
    if not text:
        return text

    def repl(m: re.Match[str]) -> str:
        expanded = _expand_placeholder(m.group(1))
        return expanded if expanded is not None else m.group(0)

    return _PLACEHOLDER_RE.sub(repl, text)


def is_control_only(text: str) -> bool:
    """若 text 仅由控制字符/转义序列组成（不含可打印字符与换行），返回 True。

    纯控制键或方向键序列发送时不应自动补换行。
    """
    if not text:
        return False
    if text.startswith("\x1b"):
        return True
    for c in text:
        o = ord(c)
        if o in (10, 13):
            return False
        if o >= 32 and o != 0x7F:
            return False
    return True


def is_probe_input(text: str) -> bool:
    """空回车/换行或 <Enter>：用于 shell 已在提示符时探测（假 busy）。"""
    expanded = expand_control_keys(text or "")
    if not expanded:
        return False
    return all(c in "\r\n" for c in expanded)


# ── AI 提示词 / 工具 description 共用（import 此模块即可） ──

TERMINAL_KEY_PLACEHOLDER_HINT = (
    "控制键占位符：<Ctrl+A>…<Ctrl+Z>及符号、<Alt+M>、<Shift+Tab>、"
    "<Enter>/<Tab>/<Esc>/<Up>/<Down>/<F1>…<F12> 等；"
    "TUI 例：nano 退出 <Ctrl+X>，vi :wq <Esc>:wq<Enter>。"
)

TERMINAL_KEY_PLACEHOLDER_GUIDE = """
**终端 / SSH 通道 · 控制键占位符**（`send_to_terminal.text` / `ssh_channel_send.content`）：
- **语法**：`<修饰+键>`，大小写不敏感，用 `+` 连接；未识别的 `<…>` 原样保留。
- **Ctrl**：`<Ctrl+A>`…`<Ctrl+Z>` 及 `<Ctrl+[>` `<Ctrl+\\>` 等；nano 保存 `<Ctrl+O>`、退出 `<Ctrl+X>`；vi 退插入模式 `<Esc>` 或 `<Ctrl+[>`；中断 `<Ctrl+C>`。
- **Alt/Meta**：`<Alt+M>`；可组合 `<Ctrl+Alt+X>`。
- **Shift**：`<Shift+Tab>` 反向 Tab。
- **命名键**：`<Enter>` `<Tab>` `<Esc>` `<Backspace>` `<Delete>` `<Up>` `<Down>` `<Left>` `<Right>` `<Home>` `<End>` `<PgUp>` `<PgDn>` `<Insert>`。
- **功能键**：`<F1>`…`<F12>`（可叠加 Ctrl/Alt/Shift）。
- **TUI 示例**：nano 保存并退出 `<Ctrl+O><Enter><Ctrl+X>`；vi 写盘退出 `<Esc>:wq<Enter>`；less 用方向键翻页、按 `q` 退出。
- 纯控制键 / 方向键 / 转义序列**不会**自动补换行；普通命令末尾无换行时会自动补 `\\n`。
""".strip()

TERMINAL_PASSWORD_HINT = (
    "密码：优先 send_service_password（凭证库或 use_host_login）；"
    "无凭证时若用户已提供密码、或密码在主机知识/记忆/提示词中且 read 已见 password 提示，"
    "可用 send 发「密码+<Enter>」（勿与 sudo 同次 send）。禁止空回车探测、禁止在回复展示密码。"
)

TERMINAL_PASSWORD_GUIDE = """
**终端 / SSH 通道 · 密码输入**（sudo / su / SSH / MySQL 等交互提示）：
- **流程（必遵）**：先 send 命令 → **必须 read** 尾部（`get_terminal_buffer` / `ssh_channel_read_lines`；可用 `until_contains="password"`）→ **仅当**出现 `[sudo] password for` / `Password:` / `password:` / `口令：` 等提示再注入。
- **优先（凭证库开启时）**：`send_service_password`（密码不进 AI 上下文）；本机 sudo/su 有提示后 `use_host_login=true`+`host_id` 或 `credential_id`。
- **亦可直接 send 密码**：当 (1) 无可用凭证且**用户本轮明确提供**密码，或 (2) 密码已在**主机知识 / 会话记忆 / 用户提示词**中且 read 已确认 password 提示时，用 `send_to_terminal` / `ssh_channel_send` 发送「密码+<Enter>」（**勿与 sudo 同次 send**；分两次：先 sudo，read，再密码）。
- **禁止**：未 read 就注入；用空回车 / `<Enter>` 探测是否等待密码；sudo 免密成功仍调用凭证；在回复中展示或复述密码。
""".strip()
