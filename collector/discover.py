"""Endpoint discovery pass 2: advisory list form + detail pages."""
import os, re, sys, json, urllib.request, urllib.parse, datetime
OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Groundwatch/1.0 (+https://github.com/jmsurma/groundwatch)"}
log = []
def get(url, name):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
            body = r.read(); meta = {"url": url, "status": r.status, "len": len(body), "final": r.geturl()}
    except Exception as e:
        body = b""; meta = {"url": url, "error": repr(e)}
    open(os.path.join(OUT, name), "wb").write(body); meta["file"] = name; log.append(meta); print(meta)
    return body.decode("latin-1", "replace")
today = datetime.datetime.utcnow()
lists = []
for d in [today, today - datetime.timedelta(days=1)]:
    for which in ["ATCSCC", "Canadian"]:
        q = {"whichAdvisories": which, "advisoryCategory": "All", "date": d.strftime("%Y-%m-%d"),
             "airflow": "true", "_airflow": "on", "ctop": "true", "_ctop": "on", "gStop": "true", "_gStop": "on",
             "gDelay": "true", "_gDelay": "on", "route": "true", "_route": "on", "other": "true", "_other": "on"}
        u = "https://www.fly.faa.gov/adv/adv_list?" + urllib.parse.urlencode(q)
        t = get(u, f"list_{which}_{d:%Y%m%d}.html"); lists.append(t)
links = []
for t in lists:
    links += re.findall(r'href="([^"]*adv_(?:otherdis|spt|dis)[^"]*)"', t, re.I)
links = list(dict.fromkeys(links))
open(os.path.join(OUT, "links.txt"), "w").write("\n".join(links))
pick = [l for l in links if re.search(r"GROUND\s*STOP|GS|CNX", urllib.parse.unquote(l), re.I)][:6] + links[:3]
for i, l in enumerate(dict.fromkeys(pick)):
    get(urllib.parse.urljoin("https://www.fly.faa.gov/adv/adv_list", html_un := l.replace("&amp;", "&")), f"detail_{i}.html")
json.dump(log, open(os.path.join(OUT, "_log.json"), "w"), indent=1)
