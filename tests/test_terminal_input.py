"""terminal_input 占位符展开与 is_control_only。"""
from services.terminal_input import expand_control_keys, is_control_only, is_probe_input


def test_ctrl_letters_full_alphabet():
    assert expand_control_keys("<Ctrl+A>") == "\x01"
    assert expand_control_keys("<Ctrl+C>") == "\x03"
    assert expand_control_keys("<Ctrl+X>") == "\x18"  # nano 退出
    assert expand_control_keys("<ctrl+z>") == "\x1a"
    assert expand_control_keys("<CTRL+O>") == "\x0f"  # nano 保存


def test_ctrl_symbols():
    assert expand_control_keys("<Ctrl+[>") == "\x1b"
    assert expand_control_keys("<Ctrl+\\>") == "\x1c"
    assert expand_control_keys("<Ctrl+]>") == "\x1d"
    assert expand_control_keys("<Ctrl+_>") == "\x1f"
    assert expand_control_keys("<Ctrl+->") == "\x1f"


def test_named_keys():
    assert expand_control_keys("<Enter>") == "\n"
    assert expand_control_keys("<Tab>") == "\t"
    assert expand_control_keys("<Esc>") == "\x1b"
    assert expand_control_keys("<Backspace>") == "\x7f"
    assert expand_control_keys("<Up>") == "\x1b[A"
    assert expand_control_keys("<Down>") == "\x1b[B"
    assert expand_control_keys("<Delete>") == "\x1b[3~"
    assert expand_control_keys("<Home>") == "\x1b[H"


def test_function_keys():
    assert expand_control_keys("<F1>") == "\x1bOP"
    assert expand_control_keys("<F5>") == "\x1b[15~"
    assert expand_control_keys("<F12>") == "\x1b[24~"


def test_alt_and_combined():
    assert expand_control_keys("<Alt+x>") == "\x1bx"
    assert expand_control_keys("<Ctrl+Alt+X>") == "\x1b\x18"
    assert expand_control_keys("<Shift+Tab>") == "\x1b[Z"


def test_ctrl_with_modifiers_on_arrow():
    assert expand_control_keys("<Ctrl+Up>") == "\x1b[1;5A"


def test_mixed_text():
    assert expand_control_keys("y<Enter>") == "y\n"
    assert expand_control_keys(":wq<Enter>") == ":wq\n"


def test_unknown_placeholder_preserved():
    assert expand_control_keys("<Unknown>") == "<Unknown>"


def test_is_control_only():
    assert is_control_only("\x03") is True
    assert is_control_only("\x1b[A") is True
    assert is_control_only("\t") is True
    assert is_control_only("\x7f") is True
    assert is_control_only("ls") is False
    assert is_control_only("y\n") is False
    assert is_control_only("\n") is False


def test_is_probe_input():
    assert is_probe_input("<Enter>") is True
    assert is_probe_input("") is False
    assert is_probe_input("<Ctrl+C>") is False
