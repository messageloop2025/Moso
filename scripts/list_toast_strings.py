# -*- coding: utf-8 -*-
import re
p = open(r"d:\UGit\毛竹\web\js\app.js", encoding="utf-8").read()
pat = re.compile(r"showToast\(\s*'((?:\\.|[^'\\])*)'")
seen = set()
for m in pat.finditer(p):
    s = m.group(1)
    if len(s) < 200:
        seen.add(s)
import sys
out = open(r"d:\UGit\毛竹\scripts\_toast_literals.txt", "w", encoding="utf-8")
for s in sorted(seen, key=len, reverse=True):
    out.write(s[:200] + "\n")
out.close()
print("wrote", len(seen), "to scripts/_toast_literals.txt", file=sys.stderr)
