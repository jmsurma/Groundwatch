"""One-off endpoint discovery: saves raw FAA responses so parsers match reality."""
import os, re, sys, json, urllib.request, urllib.parse, datetime
OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Groundwatch/1.0 (FAA ground stop dashboard; github.com/jmsurma/groundwatch)"}
log = []
def get(url, name, data=None):
    try:
        req = urllib.request.Request(url, headers=UA, data=data)
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read()
            meta = {"url": url, "status": r.status, "ctype": r.headers.get("content-type"), "len": len(body), "final": r.geturl(),
                    "last_modified": r.headers.get("last-modified"), "date": r.headers.get("date")}
    except Exception as e:
        body = b""; meta = {"url": url, "error": repr(e)}
        if hasattr(e, "read"):
            try: body = e.read()
            except Exception: pass
    open(os.path.join(OUT, name), "wb").write(body)
    meta["file"] = name; log.append(meta); print(meta)
    return body.decode("utf-8", "replace")

get("https://nasstatus.faa.gov/api/airport-status-information", "asi.xml")
for p in ["airport-events", "operations-plan", "active-nas-events", "national-programs", "ndp", "advisories", "airport-status", "nas-events", "enroute-events", "programs"]:
    get("https://nasstatus.faa.gov/api/" + p, "nas_api_" + p + ".txt")
idx = get("https://nasstatus.faa.gov/", "nas_index.html")
apis = set()
for src in re.findall(r'src="([^"]+\.js)"', idx):
    js = get(urllib.parse.urljoin("https://nasstatus.faa.gov/", src), "nas_js_" + os.path.basename(src))
    apis |= set(re.findall(r'["\'`](/?api/[A-Za-z0-9_\-/]+)', js))
    apis |= set(re.findall(r'https?://[A-Za-z0-9.\-]+faa\.gov[A-Za-z0-9_\-/.?=&%]*', js))
open(os.path.join(OUT, "nas_api_paths.txt"), "w").write("\n".join(sorted(apis)))
for a in sorted(apis):
    if a.startswith("http"): continue
    get("https://nasstatus.faa.gov/" + a.lstrip("/"), "nas_found_" + re.sub(r"[^A-Za-z0-9]", "_", a) + ".txt")

get("https://www.fly.faa.gov/adv/advADB", "adv_ADB.html")
form = get("https://www.fly.faa.gov/adv/advAdvisoryForm", "adv_form.html")
for href in set(re.findall(r'(?:href|action|src)="([^"]+)"', form)):
    if "adv" in href.lower() and not href.startswith("http://www.faa.gov"):
        get(urllib.parse.urljoin("https://www.fly.faa.gov/adv/advAdvisoryForm", href), "adv_link_" + re.sub(r"[^A-Za-z0-9]", "_", href)[:80] + ".html")
today = datetime.datetime.utcnow()
for d in [today, today - datetime.timedelta(days=1)]:
    md = d.strftime("%m/%d/%Y")
    for path in ["adv_list.jsp?whichDate=" + urllib.parse.quote(md, safe=""), "advList?whichDate=" + urllib.parse.quote(md, safe="")]:
        get("https://www.fly.faa.gov/adv/" + path, "adv_try_" + re.sub(r"[^A-Za-z0-9]", "_", path) + ".html")
get("https://news.google.com/rss/search?q=%22ground+stop%22+FAA+when:1d&hl=en-US&gl=US&ceid=US:en", "news.xml")
json.dump(log, open(os.path.join(OUT, "_log.json"), "w"), indent=1)
