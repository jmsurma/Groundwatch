"""ATCSCC Advisories Database access (formats verified against live responses, Nov 2026).

List:   GET https://www.fly.faa.gov/adv/adv_list?whichAdvisories=ATCSCC&advisoryCategory=All&date=YYYY-MM-DD&...
        Table rows: NUMBER (link adv_otherdis?adv_date=MMDDYYYY&advn=N) | CONTROL ELEMENT | DATE | BRIEF TITLE | SEND TIME
Detail: GET https://www.fly.faa.gov/adv/adv_otherdis?adv_date=MMDDYYYY&advn=N   (plain-text fields inside HTML)
"""
import datetime as dt
import time
import html
import re
import urllib.parse

BASE = "https://www.fly.faa.gov/adv/"
DEADLINE = [None]
_LAST_BAD = [""]   # monotonic time after which walks stop (set by fetch_advisory_list)


def _out_of_time():
    return DEADLINE[0] is not None and time.monotonic() > DEADLINE[0]
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
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").replace("\ufffd", " ")
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


def _send_time_iso(st):
    """List SEND TIME '11/13/26 22:46' is UTC (matches advisory SIGNATURE)."""
    try:
        return dt.datetime.strptime(st.strip(), "%m/%d/%y %H:%M").strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _scope_from_row(row):
    first = (row.get("control") or "").split("/")[0].strip().upper()
    if NATIONAL_TITLE.search(row.get("brief") or "") or first == "ALL":
        return "national"
    if re.fullmatch(r"Z[A-Z]{2}", first):
        return "facility"
    if first and first not in ("DCC", "ATCSCC"):
        return "airport"
    return "unknown"


def long_detail_url(row):
    """Long-form link used by nasstatus.faa.gov (adv_otherdis.jsp with facId/title); FAA sometimes
    answers 'NO Advisory.' on the short form while the long form works."""
    d = row.get("adv_date") or ""
    title_date = f"{d[0:2]}/{d[2:4]}/{d[4:8]}" if len(d) == 8 else row.get("date", "")
    fac = (row.get("control") or "DCC").replace(" ", "")
    title = f"ATCSCC ADVZY {str(row['number']).zfill(3)} {fac} {title_date} {row.get('brief', '')}".strip()
    q = urllib.parse.urlencode({"advn": int(row["advn"]), "adv_date": d, "facId": fac.split("/")[0] if fac != "DCC" else "ATCSCC",
                                "title": title, "titleDate": title_date}, quote_via=urllib.parse.quote)
    return BASE + "adv_otherdis.jsp?" + q


def _is_relevant(title, control=""):
    return bool(RELEVANT.search(title or "") or NATIONAL_TITLE.search(control or ""))


def _fetch_detail(url, http_get, parse_advisory_text, meta):
    if _out_of_time():
        return None, "time budget exhausted"
    r = http_get(url, timeout=15, tries=1)
    if not r.get("ok"):
        return None, r.get("error")
    txt = detail_text(r["body"])
    if not re.search(r"ADVZY\s+\d+", txt):
        title = re.search(r"<title>([^<]*)</title>", r["body"], re.I)
        _LAST_BAD[0] = (title.group(1).strip() if title else "") + " | " + re.sub(r"\s+", " ", txt)[:120]
        return None, "not an advisory page"
    return parse_advisory_text(txt, url=url, meta=meta), None


def _probe_day(day, http_get, parse_advisory_text, cache, errors, max_misses=3, max_n=400, floor=0):
    """Fallback when the list page is empty/broken: walk advisory numbers via the detail pages.
    Detail pages are cached (advisories never change once sent), so later runs only probe new numbers."""
    from concurrent.futures import ThreadPoolExecutor
    mmddyyyy = day.strftime("%m%d%Y")
    found, n, misses = [], 1, 0
    known = {}
    for u, a in cache.items():
        if f"adv_date={mmddyyyy}&" in u and not a.get("detail_missing"):
            try:
                known[int(re.search(r"advn=(\d+)", u).group(1))] = a
            except Exception:
                pass
    hard_fail = 0
    incomplete = False
    while n <= max_n:
        if _out_of_time():
            incomplete = True
            errors.append(f"Advisories {mmddyyyy}: stopped at {n - 1}, time budget exhausted")
            break
        batch = list(range(n, n + 8))
        todo = [k for k in batch if k not in known]
        res = {}
        if todo:
            with ThreadPoolExecutor(max_workers=3) as ex:
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
            elif err == "not an advisory page" and not _out_of_time():
                a2, err2 = _fetch_detail(detail_url(mmddyyyy, k), http_get, parse_advisory_text, {"number": str(k).zfill(3)})
                if a2:
                    a2["adv_date"] = mmddyyyy
                    found.append(a2); misses = hard_fail = 0
                    continue
                if k <= floor:
                    hard_fail += 1   # an advisory we know exists failed to load: transient
                    errors.append(f"Advisory {k} ({mmddyyyy}) known to exist but did not load")
                else:
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


def fetch_advisory_list(now, http_get, parse_advisory_text, cache=None, floors=None, sleep_ok=True, budget_s=330):
    """Today's + yesterday's (UTC) ATCSCC advisories.

    1. List page (fast). FAA's list sometimes answers "NO ADVISORIES MATCH" even while advisories exist
       (observed live 2026-11-13 during a nationwide ground stop), so an empty list is never trusted.
    2. Fallback: walk detail pages by advisory number (cached between runs).
    cache: {url: advisory} from previous runs.
    """
    cache = cache or {}
    DEADLINE[0] = time.monotonic() + budget_s
    floors = dict(floors or {})
    for u in cache:   # highest advisory number already seen per date is a floor for completeness
        m = re.search(r"adv_date=(\d{8})&advn=(\d+)", u)
        if m:
            floors[m.group(1)] = max(floors.get(m.group(1), 0), int(m.group(2)))
    checked, errors, items, latest = [], [], [], None
    today_ok = False
    stubs = []
    for idx, day in enumerate((now, now - dt.timedelta(days=1))):
        u = list_url(day)
        entry = {"url": u, "day": day.strftime("%Y-%m-%d"), "ok": False, "method": "list", "attempts": 0}
        rows = []
        for attempt in range(3):   # FAA's list intermittently answers "NO ADVISORIES MATCH"
            entry["attempts"] = attempt + 1
            r = http_get(u, timeout=30, tries=1)
            if r.get("ok"):
                cap, rows, no_match, valid = parse_list(r["body"])
                entry.update(caption=cap, rows=len(rows), no_match=no_match, valid=valid, error=None)
                if rows:
                    break
            else:
                entry["error"] = r.get("error")
            time.sleep(4 if sleep_ok else 0)
        floor = floors.get(day.strftime("%m%d%Y"), 0)
        entry["expected_min_number"] = floor
        day_items = []
        if rows:
            entry["max_number"] = max(int(r_["advn"]) for r_ in rows if str(r_["advn"]).isdigit())
            entry["ok"] = entry["max_number"] >= floor
            if not entry["ok"]:
                errors.append(f"List {entry['day']} ends at {entry['max_number']} but advisory {floor} was already seen; list looks truncated")
            for row in rows:
                if idx == 0 and latest is None:
                    latest = {"number": row["number"], "brief": row["brief"], "send_time": row["send_time"], "url": row["url"]}
                if not _is_relevant(row["brief"], row["control"]):
                    continue
                url = row["url"]
                a = dict(cache[url]) if url in cache and cache[url].get("title") and not cache[url].get("detail_missing") else None
                if a is None:
                    tries = 3 if re.search(r"GROUND\s*STOP|\bGS\b|NATION", row["brief"], re.I) else 1
                    for t_ in range(tries):
                        a, err = _fetch_detail(url if t_ % 2 == 0 else long_detail_url(row), http_get, parse_advisory_text,
                                               {"title": row["brief"], "number": row["number"], "control": row["control"]})
                        if a is not None:
                            a["url"] = url
                        if a is not None or _out_of_time():
                            break
                        if sleep_ok:
                            time.sleep(2 + 2 * t_)
                    if a is None:
                        errors.append(f"Advisory {row['number']} detail: {err}" + (f" [FAA returned: {_LAST_BAD[0]}]" if err == "not an advisory page" else ""))
                        a = {"number": row["number"], "title": f"ATCSCC ADVZY {row['number']} {row['control']} {row['date']} {row['brief']}",
                             "type": row["brief"].upper(), "url": url, "detail_missing": True,
                             "is_ground_stop": bool(re.search(r"GROUND\s*STOP|\bGS\b", row["brief"], re.I)),
                             "scope": _scope_from_row(row),
                             "ctl_element": (row["control"].split("/")[0].strip() or None) if row["control"].split("/")[0].strip() not in ("DCC", "ALL", "ATCSCC") else None,
                             "action": "cancellation" if re.search(r"\bCNX\b|CANCEL", row["brief"], re.I) else "issued"}
                a.update(list_control=row["control"], list_brief=row["brief"], send_time=row["send_time"], adv_date=row["adv_date"])
                if not a.get("issued_utc"):
                    a["issued_utc"] = _send_time_iso(row["send_time"])
                day_items.append(a)
        else:
            entry["fallback"] = "detail-walk"
            entry["method"] = "detail-walk"
            walked, incomplete = _probe_day(day, http_get, parse_advisory_text, cache, errors, floor=floor)
            entry["walked"] = len(walked)
            entry["max_number"] = max([int(re.sub(r"\D", "", str(a.get("number") or 0)) or 0) for a in walked] or [0])
            if walked and entry["max_number"] < floor:
                incomplete = True
                errors.append(f"Advisories {entry['day']}: number walk reached {entry['max_number']}, expected at least {floor}")
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
            # Just after 00:00Z a new day legitimately has no advisories yet. Accept only if the
            # list page itself answered validly for the new date, the number walk found nothing
            # (not blocked by errors), and yesterday verifies below.
            entry["new_day_empty"] = (not entry["ok"] and now.hour < 2 and entry.get("valid") and entry.get("no_match")
                                      and not entry.get("incomplete") and not entry.get("walked"))
        for a in day_items:
            a["listed_now"] = True
        items += day_items
        checked.append(entry)
    if not today_ok and checked and checked[0].get("new_day_empty") and len(checked) > 1 and checked[1]["ok"]:
        today_ok = True
        checked[0]["note"] = "No advisories published yet for this UTC day; verified against the previous day's list."
        errors = [e for e in errors if not e.startswith(f"Advisories for {checked[0]['day']}")]
    return {"ok": today_ok, "partial": not all(c["ok"] for c in checked), "items": items, "errors": errors,
            "checked": checked, "latest": latest, "list_count": sum(c.get("rows", 0) or c.get("walked", 0) for c in checked),
            "stubs": stubs}
