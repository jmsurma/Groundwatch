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


def _is_relevant(title, control=""):
    return bool(RELEVANT.search(title or "") or NATIONAL_TITLE.search(control or ""))


def _fetch_detail(url, http_get, parse_advisory_text, meta):
    r = http_get(url, timeout=40, tries=2)
    if not r.get("ok"):
        return None, r.get("error")
    txt = detail_text(r["body"])
    if not re.search(r"ADVZY\s+\d+", txt):
        return None, "not an advisory page"
    return parse_advisory_text(txt, url=url, meta=meta), None


def _probe_day(day, http_get, parse_advisory_text, cache, errors, max_misses=3, max_n=400):
    """Fallback when the list page is empty/broken: walk advisory numbers via the detail pages.
    Detail pages are cached (advisories never change once sent), so later runs only probe new numbers."""
    from concurrent.futures import ThreadPoolExecutor
    mmddyyyy = day.strftime("%m%d%Y")
    found, n, misses = [], 1, 0
    known = {}
    for u, a in cache.items():
        if f"adv_date={mmddyyyy}&" in u:
            try:
                known[int(re.search(r"advn=(\d+)", u).group(1))] = a
            except Exception:
                pass
    hard_fail = 0
    incomplete = False
    while n <= max_n:
        batch = list(range(n, n + 8))
        todo = [k for k in batch if k not in known]
        res = {}
        if todo:
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {k: ex.submit(_fetch_detail, detail_url(mmddyyyy, k), http_get, parse_advisory_text, {"number": str(k).zfill(3)}) for k in todo}
                for k, f in futs.items():
                    res[k] = f.result()
        stop = False
        for k in batch:
            if k in known:
                found.append(known[k]); misses = hard_fail = 0
                continue
            a, err = res.get(k, (None, "skipped"))
            if a:
                a["adv_date"] = mmddyyyy
                if not _is_relevant(a.get("type") or a.get("title")) and not a.get("is_ground_stop") and a.get("scope") != "national":
                    a = {"url": a.get("url"), "number": a.get("number"), "title": a.get("title"), "type": a.get("type"),
                         "issued_utc": a.get("issued_utc"), "adv_date": mmddyyyy, "irrelevant": True}
                found.append(a); misses = hard_fail = 0
            elif err == "not an advisory page":
                misses += 1
            else:
                hard_fail += 1
                errors.append(f"Advisory {k} ({mmddyyyy}): {err}")
            if misses >= max_misses:
                stop = True; break
            if hard_fail >= 6:
                stop = True; incomplete = True; break
        if stop:
            break
        n += 8
    return found, incomplete


def fetch_advisory_list(now, http_get, parse_advisory_text, cache=None):
    """Today's + yesterday's (UTC) ATCSCC advisories.

    1. List page (fast). FAA's list sometimes answers "NO ADVISORIES MATCH" even while advisories exist
       (observed live 2026-11-13 during a nationwide ground stop), so an empty list is never trusted.
    2. Fallback: walk detail pages by advisory number (cached between runs).
    cache: {url: advisory} from previous runs.
    """
    cache = cache or {}
    checked, errors, items, latest = [], [], [], None
    today_ok = False
    stubs = []
    for idx, day in enumerate((now, now - dt.timedelta(days=1))):
        u = list_url(day)
        r = http_get(u, timeout=45)
        entry = {"url": u, "day": day.strftime("%Y-%m-%d"), "ok": False, "method": "list"}
        rows = []
        if r.get("ok"):
            cap, rows, no_match, valid = parse_list(r["body"])
            entry.update(caption=cap, rows=len(rows), no_match=no_match, valid=valid)
        else:
            entry["error"] = r.get("error")
        day_items = []
        if rows:
            entry["ok"] = True
            for row in rows:
                if idx == 0 and latest is None:
                    latest = {"number": row["number"], "brief": row["brief"], "send_time": row["send_time"], "url": row["url"]}
                if not _is_relevant(row["brief"], row["control"]):
                    continue
                url = row["url"]
                a = dict(cache[url]) if url in cache and cache[url].get("title") and not cache[url].get("detail_missing") else None
                if a is None:
                    a, err = _fetch_detail(url, http_get, parse_advisory_text,
                                           {"title": row["brief"], "number": row["number"], "control": row["control"]})
                    if a is None:
                        errors.append(f"Advisory {row['number']} detail: {err}")
                        a = {"number": row["number"], "title": f"ATCSCC ADVZY {row['number']} {row['control']} {row['date']} {row['brief']}",
                             "type": row["brief"].upper(), "url": url, "detail_missing": True,
                             "is_ground_stop": bool(re.search(r"GROUND\s*STOP|\bGS\b", row["brief"], re.I)),
                             "scope": "national" if NATIONAL_TITLE.search(row["brief"]) else "unknown",
                             "action": "cancellation" if re.search(r"\bCNX\b|CANCEL", row["brief"], re.I) else "issued"}
                a.update(list_control=row["control"], list_brief=row["brief"], send_time=row["send_time"], adv_date=row["adv_date"])
                day_items.append(a)
        else:
            entry["fallback"] = "detail-walk"
            entry["method"] = "detail-walk"
            walked, incomplete = _probe_day(day, http_get, parse_advisory_text, cache, errors)
            entry["walked"] = len(walked)
            entry["incomplete"] = incomplete
            stubs += [a for a in walked if a.get("irrelevant")]
            if walked and not incomplete:
                entry["ok"] = True
                if idx == 0:
                    top = max(walked, key=lambda a: int(re.sub(r"\D", "", str(a.get("number") or 0)) or 0))
                    latest = {"number": top.get("number"), "brief": top.get("type"), "send_time": top.get("signature"), "url": top.get("url")}
            for a in walked:
                if a.get("irrelevant"):
                    continue
                if _is_relevant(a.get("type") or a.get("title"), a.get("list_control") or "") or a.get("is_ground_stop") or a.get("scope") == "national":
                    day_items.append(a)
            if not walked and idx == 0:
                errors.append(f"Advisories for {entry['day']}: list page returned no rows and no advisories could be read by number")
        if idx == 0:
            today_ok = entry["ok"]
        for a in day_items:
            a["listed_now"] = True
        items += day_items
        checked.append(entry)
    return {"ok": today_ok, "partial": not all(c["ok"] for c in checked), "items": items, "errors": errors,
            "checked": checked, "latest": latest, "list_count": sum(c.get("rows", 0) or c.get("walked", 0) for c in checked),
            "stubs": stubs}
