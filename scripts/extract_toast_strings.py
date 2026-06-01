# -*- coding: utf-8 -*-
import re, pathlib
s = pathlib.Path("web/js/app.js").read_text(encoding="utf-8", errors="replace")
pat = re.compile(r"showToast\(\s*'((?:\\.|[^'\\])*)'")
a = sorted(set(m.group(1) for m in pat.finditer(s)), key=lambda z: (len(z), z))
out = pathlib.Path("scripts/_toast_strings.txt")
out.write_text("\n".join(a) + "\n---\n" + str(len(a)), encoding="utf-8")
