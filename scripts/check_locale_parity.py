#!/usr/bin/env python3
"""校验 web/locales/zh-CN 与 web/locales/en 下同名 JSON 的键是否递归一致。

部分 JSON 带 UTF-8 BOM，本脚本会容忍。用法：

    python scripts/check_locale_parity.py

退出码：0 表示完全一致；1 表示缺键、多余文件或解析错误。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be object")
    return data


def flatten_keys(obj: object, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            keys.add(p)
            keys |= flatten_keys(v, p)
    return keys


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    zh_dir = root / "web" / "locales" / "zh-CN"
    en_dir = root / "web" / "locales" / "en"
    if not zh_dir.is_dir() or not en_dir.is_dir():
        print("missing web/locales/zh-CN or web/locales/en", file=sys.stderr)
        return 1

    zh_files = {p.name for p in zh_dir.glob("*.json")}
    en_files = {p.name for p in en_dir.glob("*.json")}
    only_zh = sorted(zh_files - en_files)
    only_en = sorted(en_files - zh_files)
    if only_zh or only_en:
        if only_zh:
            print("JSON only in zh-CN:", only_zh, file=sys.stderr)
        if only_en:
            print("JSON only in en:", only_en, file=sys.stderr)
        return 1

    exit_code = 0
    for name in sorted(zh_files & en_files):
        pz, pe = zh_dir / name, en_dir / name
        try:
            jz, je = load_json(pz), load_json(pe)
        except Exception as e:
            print(f"{name}: {e}", file=sys.stderr)
            exit_code = 1
            continue
        kz, ke = flatten_keys(jz), flatten_keys(je)
        missing_zh = sorted(ke - kz)
        missing_en = sorted(kz - ke)
        if missing_zh or missing_en:
            exit_code = 1
            print(f"=== {name} ===")
            for k in missing_zh:
                print(f"  missing in zh-CN: {k}")
            for k in missing_en:
                print(f"  missing in en:    {k}")

    if exit_code == 0:
        print(f"OK: {len(zh_files & en_files)} locale JSON pairs, recursive keys match.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
