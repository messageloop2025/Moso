#!/usr/bin/env python3
"""
从 web/intro/index.html（主语言）和 web/intro/en/index.html 抽取可本地化片段，
生成 web/locales/{zh-CN,en}/intro.json，并写出内层留空的 index.html
（结构壳 + /static/js/intro-page.js），并把 web/intro/en/index.html
写成跳转到 /intro/?lang=en 的短重定向页。

当 index.html 或 en/index.html 已被替换为短壳/重定向且体积过小时，
会改用 web/intro/sources/{zh,en}.full.html（上成功构建的备份，须存在）。

用法（仓库根目录）:
  python tools/build_intro_json.py
依赖: beautifulsoup4
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
INTRO = ROOT / "web" / "intro"
SHELL_BACKUP_ZH = INTRO / "sources" / "zh.full.html"
SHELL_BACKUP_EN = INTRO / "sources" / "en.full.html"
OUT_ZH = ROOT / "web" / "locales" / "zh-CN" / "intro.json"
OUT_EN = ROOT / "web" / "locales" / "en" / "intro.json"
OUT_SHELL = INTRO / "index.shell.html"

# 过小的 HTML 视为重定向或仅壳，拒绝抽取以免清空 JSON
MIN_HTML_BYTES = 10_000

EN_REDIRECT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>毛竹</title>
  <link rel="canonical" href="/intro/?lang=en">
  <link rel="alternate" hreflang="zh-CN" href="/intro/?lang=zh-CN">
  <link rel="alternate" hreflang="en" href="/intro/?lang=en">
  <meta http-equiv="refresh" content="0;url=/intro/?lang=en">
  <script>location.replace("/intro/?lang=en");</script>
</head>
<body>
  <p><a href="/intro/?lang=en">毛竹 (English)</a></p>
</body>
</html>
"""


def pick_source(primary: Path, backup: Path) -> Path:
    if primary.is_file() and primary.stat().st_size >= MIN_HTML_BYTES:
        return primary
    if backup.is_file() and backup.stat().st_size >= MIN_HTML_BYTES:
        if primary.is_file() and primary.stat().st_size < MIN_HTML_BYTES:
            print(
                f"注意: {primary} 仅 {primary.stat().st_size} 字节，"
                f"改用备份 {backup}",
                file=sys.stderr,
            )
        return backup
    print(
        f"错误: 需要完整 intro HTML (≥ {MIN_HTML_BYTES} 字节) 位于 {primary} 或 {backup}。",
        file=sys.stderr,
    )
    raise SystemExit(1)


def read_soup(path: Path) -> BeautifulSoup:
    return BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")


def meta_of(soup: BeautifulSoup) -> dict:
    t = soup.find("title")
    m = soup.find("meta", attrs={"name": "description"})
    return {
        "title": t.get_text(strip=True) if t else "",
        "description": m.get("content", "").strip() if m and m.get("content") else "",
    }


def nav_inner(soup: BeautifulSoup) -> str:
    el = soup.select_one("nav.nav .nav-inner")
    return (el.decode_contents() or "").strip() if el else ""


def hero_inner(soup: BeautifulSoup) -> str:
    el = soup.select_one("section#hero .hero-inner, section.hero#hero .hero-inner, section.hero .hero-inner")
    return (el.decode_contents() or "").strip() if el else ""


def section_html(soup: BeautifulSoup, sec_id: str) -> str:
    el = soup.select_one(f"section#{sec_id}")
    return (el.decode_contents() or "").strip() if el else ""


def foot_inner(soup: BeautifulSoup) -> str:
    f = soup.find("footer")
    if not f:
        return ""
    return (f.decode_contents() or "").strip()


PARTIAL_ORDER = [
    "navInner",
    "heroInner",
    "start-here",
    "positioning",
    "ai",
    "multihost",
    "features",
    "architecture",
    "security",
    "tasks",
    "tech-stack",
    "faq",
    "cta",
    "footer",
]


def normalize_nav_inner(html: str, locale: str) -> str:
    """统一为 /intro/?lang= 切换方式，避免维护多份平行 HTML 路径。"""
    html = html.replace('href="/intro/en/"', 'href="/intro/?lang=en"')
    html = html.replace("href='/intro/en/'", "href='/intro/?lang=en'")
    # 语言切换：英文页里「中文」链到简中
    html = re.sub(
        r'(<a[^>]*title="中文"[^>]*\bhref=")([^"]*)(")',
        lambda m: m.group(1) + "/intro/?lang=zh-CN" + m.group(3)
        if m.group(2).rstrip("/") in ("/intro", "/intro/")
        else m.group(1) + m.group(2) + m.group(3),
        html,
    )
    # Logo 回首页，语言由 URL 或 intro-page 决定
    html = html.replace('class="logo" href="/intro/?lang=en"', 'class="logo" href="/intro/"')
    # 英文页常见顺序：先 href 后 title
    if locale == "en":
        html = html.replace(
            'href="/intro/" style="color:#a8c8ff" title="中文"',
            'href="/intro/?lang=zh-CN" style="color:#a8c8ff" title="中文"',
        )
    return html


def extract_bundle(soup: BeautifulSoup) -> dict:
    partials: dict = {
        "navInner": nav_inner(soup),
        "heroInner": hero_inner(soup),
    }
    for sec_id in (
        "start-here",
        "positioning",
        "ai",
        "multihost",
        "features",
        "architecture",
        "security",
        "tasks",
        "tech-stack",
        "faq",
        "cta",
    ):
        key = sec_id
        partials[key] = section_html(soup, sec_id)
    partials["footer"] = foot_inner(soup)
    return {"meta": meta_of(soup), "partials": partials}


def clear_partials_in_shell(soup: BeautifulSoup) -> None:
    el = soup.select_one("nav.nav .nav-inner")
    if el:
        el.clear()
    el = soup.select_one("section#hero .hero-inner, section.hero .hero-inner")
    if el:
        el.clear()
    for sec_id in (
        "start-here",
        "positioning",
        "ai",
        "multihost",
        "features",
        "architecture",
        "security",
        "tasks",
        "tech-stack",
        "faq",
        "cta",
    ):
        s = soup.select_one(f"section#{sec_id}")
        if s:
            s.clear()
    f = soup.find("footer")
    if f:
        f.clear()

    # 内联动效/粒子等改由 /static/js/intro-page.js 在注入文案后执行
    body = soup.body
    if body:
        for tag in list(body.find_all("script")):
            tag.decompose()
        s = soup.new_tag("script", src="/static/js/intro-page.js", defer="")
        body.append(s)

    t = soup.find("title")
    if t:
        t.string = "毛竹"
    m = soup.find("meta", attrs={"name": "description"})
    if m and m.get("content") is not None:
        m["content"] = ""

    inject_hreflang(soup)


def inject_hreflang(soup: BeautifulSoup) -> None:
    h = soup.head
    if not h:
        return
    for old in list(h.find_all("link", hreflang=True, rel="alternate")):
        old.decompose()
    for hreflang, href in (
        ("zh-CN", "/intro/?lang=zh-CN"),
        ("en", "/intro/?lang=en"),
        ("x-default", "/intro/?lang=zh-CN"),
    ):
        tag = soup.new_tag("link", rel="alternate", hreflang=hreflang, href=href)
        h.append("\n    ")
        h.append(tag)
    h.append("\n")


def main() -> int:
    zhi = pick_source(INTRO / "index.html", SHELL_BACKUP_ZH)
    eni = pick_source(INTRO / "en" / "index.html", SHELL_BACKUP_EN)
    zhs = read_soup(zhi)
    ens = read_soup(eni)
    bzh = extract_bundle(zhs)
    bzh["locale"] = "zh-CN"
    if bzh.get("partials", {}).get("navInner"):
        bzh["partials"]["navInner"] = normalize_nav_inner(
            bzh["partials"]["navInner"], "zh-CN"
        )
    ben = extract_bundle(ens)
    ben["locale"] = "en"
    if ben.get("partials", {}).get("navInner"):
        ben["partials"]["navInner"] = normalize_nav_inner(
            ben["partials"]["navInner"], "en"
        )
    OUT_ZH.parent.mkdir(parents=True, exist_ok=True)
    OUT_EN.parent.mkdir(parents=True, exist_ok=True)
    OUT_ZH.write_text(
        json.dumps(bzh, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    OUT_EN.write_text(
        json.dumps(ben, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Wrote", OUT_ZH, "and", OUT_EN)

    sources = INTRO / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    # 仅用「当前磁盘上的完整页」刷新备份；壳/重定向页过小则保留已有 *.full.html
    _idx = INTRO / "index.html"
    if _idx.is_file() and _idx.stat().st_size >= MIN_HTML_BYTES:
        shutil.copy2(_idx, SHELL_BACKUP_ZH)
    _enidx = INTRO / "en" / "index.html"
    if _enidx.is_file() and _enidx.stat().st_size >= MIN_HTML_BYTES:
        shutil.copy2(_enidx, SHELL_BACKUP_EN)

    shell = read_soup(zhi)
    clear_partials_in_shell(shell)
    shell_s = str(shell)
    OUT_SHELL.write_text(shell_s, encoding="utf-8")
    (INTRO / "index.html").write_text(shell_s, encoding="utf-8")
    (INTRO / "en" / "index.html").write_text(EN_REDIRECT_HTML, encoding="utf-8")
    print("Wrote", OUT_SHELL, "and", INTRO / "index.html")
    print("Wrote", INTRO / "en" / "index.html", "(redirect to /intro/?lang=en)")

    for k in PARTIAL_ORDER:
        a, b = bzh["partials"].get(k, ""), ben["partials"].get(k, "")
        if bool(a) != bool(b):
            print("warning: partial", k, "empty on one side", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
