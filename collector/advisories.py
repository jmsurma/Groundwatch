"""ATCSCC Advisories Database access (formats verified against live responses, Nov 2026).

List:   GET https://www.fly.faa.gov/adv/adv_list?whichAdvisories=ATCSCC&advisoryCategory=All&date=YYYY-MM-DD&...
        Table rows: NUMBER (link adv_otherdis?adv_date=MMDDYYYY&advn=N) | CONTROL ELEMENT | DATE | BRIEF TITLE | SEND TIME
Detail: GET https://www.fly.faa.gov/adv/adv_otherdis?adv_date=MMDDYYYY&advn=N   (plain-text fields inside HTML)
"""
import datetime as dt
import html
import re
import urllib.parse

BASE = "https://www.fly.faa.gov/adv/"
CHECKBOXES = {"airflow": "true", "_airflow": "on", "ctop": "true", "_ctop": "on", "gStop": "true", "_gStop": "on",
              "gDelay": "true", "_gDelay": "on", "route": "true", "_route": "on", "other": "true", "_other": "on"}
# titles worth fetching in full: ground stops, their cancellations/extensions, and nationwide/systemwide items
RELEVANT = re.compile(r"GROUND\s*STOP|\bGS\b|NATION|SYSTEM\s*WIDE|NAS\s*WIDE|ALL\s+AIRPORTS|ALL\s+FLIGHTS|"
                      r"COMMUNICATIONS?\s+(?:FAILURE|OUTAGE)|NOTAM\s+SYSTEM|EQUIPMENT\s+OUTAGE", re.I)
NATIONAL_TITLE = re.compile(r"NATION\s*WIDE|NATIONAL|SYSTEM\s*WIDE|NAS\s*WIDE|ALL\s+AIRPORTS|ALL\s+CENTERS", re.I)


def list_url(day, which="ATCSCC"):
    q = {"whichAdvisories": which, "advisoryCategory": "All", "date": day.strftime("%Y-%m-%d"), **CHECKBOXES}
    return BASE + "adv_list?" + urllib.parse.urlencode(q)


def detail_url(adv_date_mmddyyyy, advn):
    return f"{BASE}adv_otherdis?adv_date={adv_date_mmddyyyy}&advn={int(advn)}"


def _text(cell):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell)).replace("\xa0", " ")).strip()


def parse_list(page):
    """Returns (caption, rows, no_match, looks_valid)."""
    cap = re.search(r"ADVISORIES FOR (\d{4}-\d{2}-\d{2})", page)
    no_match = "NO ADVISORIES MATCH" in page.upper()
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        if "adv_otherdis" not in tr:
            continue
        href = re.search(r'href="([^"]*adv_otherdis[^"]*)"', tr)
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(cells) < 5 or not href:
            continue
        h = html.unescape(href.group(1))
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(h).query)
        rows.append({"number": cells[0].zfill(3), "control": cells[1], "date": cells[2], "brief": cells[3],
                     "send_time": cells[4], "adv_date": (qs.get("adv_date") or [""])[0],
                     "advn": (qs.get("advn") or [cells[0]])[0], "url": urllib.parse.urljoin(BASE, h)})
    return (cap.group(1) if cap else None), rows, no_match, bool(cap)


def detail_text(page):
    i = page.find('class="mainArea"')
    s = page[i:] if i >= 0 else page
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|tr|div|th|td)>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    cut = s.find("FAA.gov Home")
    if cut > 0:
        s = s[:cut]
    lines = [ln.strip() for ln in s.split("\n")]
    lines = [ln for ln in lines if ln and ln != "ATCSCC Advisory"]
    # join "LABEL:" lines with their value on the next line (EFFECTIVE TIME / SIGNATURE layout)
    out = []
    for ln in lines:
        if out and re.fullmatch(r"[A-Z][A-Z /()&.-]{1,40}:", out[-1]) and not re.match(r"[A-Z][A-Z /()&.-]{1,40}:", ln):
            out[-1] = out[-1] + " " + ln
        else:
            out.append(ln)
    return "\n".join(out)


def fetch_advisory_list(now, http_get, parse_advisory_text, cache=None):
    """Fetch today's + yesterday's (UTC) ATCSCC lists, then details of relevant advisories.

    cache: {url: parsed advisory} from previous runs (advisories are immutable once issued).
    """
    cache = cache or {}
    checked, errors, items, list_rows = [], [], [], []
    ok_days = 0
    for day in (now, now - dt.timedelta(days=1)):
        u = list_url(day)
        r = http_get(u, timeout=45)
        entry = {"url": u, "day": day.strftime("%Y-%m-%d"), "ok": bool(r.get("ok"))}
        if not r.get("ok"):
            entry["error"] = r.get("error")
            errors.append(f"List {entry['day']}: {r.get('error')}")
            checked.append(entry)
            continue
        cap, rows, no_match, valid = parse_list(r["body"])
        entry.update(caption=cap, rows=len(rows), no_match=no_match, valid=valid)
        if not valid:
            entry["ok"] = False
            errors.append(f"List {entry['day']}: unrecognised page (FAA format change or error page)")
        else:
            ok_days += 1
            list_rows += rows
        checked.append(entry)
    today_ok = checked[0]["ok"] if checked else False
    latest = None
    for row in list_rows:
        if latest is None:
            latest = {"number": row["number"], "brief": row["brief"], "send_time": row["send_time"], "url": row["url"]}
        if not RELEVANT.search(row["brief"]) and not NATIONAL_TITLE.search(row["control"] or ""):
            continue
        url = row["url"]
        if url in cache and cache[url].get("title"):
            a = dict(cache[url])
        else:
            r = http_get(url, timeout=40)
            if not r.get("ok"):
                errors.append(f"Advisory {row['number']}: {r.get('error')}")
                a = {"number": row["number"], "title": f"ATCSCC ADVZY {row['number']} {row['control']} {row['date']} {row['brief']}",
                     "type": row["brief"].upper(), "url": url, "detail_missing": True,
                     "is_ground_stop": bool(re.search(r"GROUND\s*STOP|\bGS\b", row["brief"], re.I))}
            else:
                txt = detail_text(r["body"])
                a = parse_advisory_text(txt, url=url, meta={"title": row["brief"], "number": row["number"], "control": row["control"]})
        a["list_control"] = row["control"]
        a["list_brief"] = row["brief"]
        a["send_time"] = row["send_time"]
        a["adv_date"] = row["adv_date"]
        a["listed_now"] = True
        items.append(a)
    return {"ok": today_ok, "partial": ok_days < 2, "items": items, "errors": errors, "checked": checked,
            "latest": latest, "list_count": len(list_rows)}
