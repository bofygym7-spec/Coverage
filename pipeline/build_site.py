#!/usr/bin/env python3
"""Emit a single self-contained dashboard.html with the data inlined.

The repo version of index.html fetches data.json at runtime, which needs a web
server. This build produces one file that opens directly from disk, which is
what a non-technical reviewer actually needs.
"""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tpl = open(os.path.join(ROOT, "site", "index.html"), encoding="utf-8").read()
data = open(os.path.join(ROOT, "site", "data.json"), encoding="utf-8").read()
assert "/*__DATA__*/ null" in tpl, "data placeholder missing from index.html"
out = tpl.replace("/*__DATA__*/ null", data.replace("</", "<\\/"), 1)
dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "site", "dashboard.html")
os.makedirs(os.path.dirname(dest), exist_ok=True)
open(dest, "w", encoding="utf-8").write(out)
print(f"wrote {dest}  ({len(out)/1024:.0f} KB, data {len(data)/1024:.0f} KB)")
