"""Relay step: validate the collector's status.json and write the page's data files.
Usage: python3 build.py <status.json> <outdir>"""
import json, sys, datetime as dt
src, out = sys.argv[1], sys.argv[2]
d = json.load(open(src, encoding="utf-8"))
assert d.get("schema") == 1 and "national" in d and "airports" in d, "unexpected status.json"
d.pop("advisory_cache", None)          # collector-internal; keeps the page payload small
d["relayed_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
body = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
open(f"{out}/status.json", "w", encoding="utf-8").write(body)
open(f"{out}/status.js", "w", encoding="utf-8").write("window.__GW=" + body.replace("</", "<\\/") + ";")
print(d["generated_at"], d["national"]["status"], len(body), "bytes")
