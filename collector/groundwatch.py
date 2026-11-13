#!/usr/bin/env python3
"""Groundwatch collector.

Runs server-side (GitHub Actions) every 15 minutes. Fetches FAA sources, parses
national advisories and airport programs separately, diffs against the previous
snapshot, and writes status.json for the dashboard.

Ground rules this code enforces:
  * An empty airport feed never implies normal nationwide operations.
  * A stop is only "cancelled" when an FAA cancellation/release advisory says so.
  * A passed nominal end time or a vanished advisory is reported as UNCONFIRMED.
  * When FAA advisory sources fail, national status is "unverified".
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Groundwatch/1.0 (FAA ground stop dashboard; +https://github.com/jmsurma/groundwatch)"

URL_AIRPORT_XML = "https://nasstatus.faa.gov/api/airport-status-information"
URL_NAS = "https://nasstatus.faa.gov/"
URL_EVENTS = "https://nasstatus.faa.gov/api/airport-events"
URL_ADB = "https://www.fly.faa.gov/adv/advADB"
URL_ADV_FORM = "https://www.fly.faa.gov/adv/advAdvisoryForm"
# Filled in from live discovery; see ADVISORY_LIST_* below.
SCHEMA = 1

AIRPORT_STALE_MIN = 20      # FAA Update_Time older than this vs fetch = stale
NATIONAL_KEEP_HOURS = 30    # keep national items visible after last seen
UNLISTED_KEEP_HOURS = 3     # keep "no longer listed" airport programs visible


# ----------------------------------------------------------------- utilities
def now_utc():
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except Exception:
        return None


def http_get(url, timeout=40, data=None, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                cs = (re.search(r"charset=([\w-]+)", r.headers.get("content-type") or "") or [None, None])[1]
                try:
                    text = body.decode(cs or "utf-8")
                except (UnicodeDecodeError, LookupError):
                    text = body.decode("latin-1")
                return {"ok": True, "status": r.status, "body": text,
                        "headers": dict(r.headers), "final_url": r.geturl()}
        except Exception as e:  # noqa: BLE001
            last = e
            code = getattr(e, "code", None)
            if code and 400 <= code < 500 and code != 429:
                break
            time.sleep(3 * (i + 1))
    return {"ok": False, "error": f"{type(last).__name__}: {last}"[:300],
            "status": getattr(last, "code", None)}


_AIRPORTS = None


def airport_info(code):
    global _AIRPORTS
    if _AIRPORTS is None:
        try:
            _AIRPORTS = json.load(open(os.path.join(HERE, "airports.json"), encoding="utf-8"))
        except Exception:
            _AIRPORTS = {}
    c = (code or "").strip().upper()
    cands = [c]
    if len(c) == 3 and c.startswith("Y"):
        cands.append("C" + c)           # Canadian IATA -> ICAO
    if len(c) == 4 and c[0] in "CK":
        cands.append(c[1:])
    for k in cands:
        v = _AIRPORTS.get(k)
        if v:
            name, city, region, country = v
            reg = region.split("-", 1)[1] if "-" in region else region
            if country == "PR":
                reg = "PR"
            loc = ", ".join(x for x in [city, reg if country in ("US", "CA") else None,
                                         None if country == "US" else country] if x)
            return {"name": name, "location": loc, "country": country, "verified": True,
                    "name_source": "OurAirports"}
    return {"name": None, "location": None, "country": "CA" if c.startswith("Y") or c.startswith("CY") else None,
            "verified": False, "name_source": None}


def clean(s):
    if s is None:
        return None
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    return s or None


# ------------------------------------------------------- airport XML feed
TZ_ABBR = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
           "AKST": -9, "AKDT": -8, "HST": -10, "AST": -4, "ADT": -3, "NST": -3.5, "NDT": -2.5,
           "UTC": 0, "GMT": 0, "Z": 0, "CHST": 10, "SST": -11}


def parse_update_time(s):
    """'Mon Oct 26 20:59:52 2026 GMT' -> aware datetime."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a %b %d %H:%M:%S %Y %Z", "%a %b %d %H:%M:%S %Z %Y", "%a %b %d %Y %H:%M:%S %Z"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except Exception:
            pass
    try:
        return parsedate_to_datetime(s).astimezone(UTC)
    except Exception:
        return None


def end_time_info(raw, ref):
    """FAA End_Time like '6:00 pm EST'. Keep original; add a best-effort UTC estimate."""
    out = {"raw": raw, "tz": None, "utc_estimate": None}
    if not raw:
        return out
    m = re.search(r"(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?\s*([A-Z]{1,4})?", raw, re.I)
    if not m:
        return out
    h, mi = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").lower().replace(".", "")
    tz = (m.group(4) or "").upper() or None
    out["tz"] = tz
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if tz in TZ_ABBR and ref:
        off = dt.timedelta(hours=TZ_ABBR[tz])
        local_ref = ref + off
        cand = local_ref.replace(hour=h % 24, minute=mi, second=0)
        # choose the occurrence nearest to (and normally after) the reference time
        if cand < local_ref - dt.timedelta(hours=6):
            cand += dt.timedelta(days=1)
        out["utc_estimate"] = iso((cand - off).replace(tzinfo=UTC))
    return out


def flatten(el):
    d = {}
    for ch in el:
        tag = ch.tag
        if len(ch):
            sub = flatten(ch)
            if ch.attrib:
                sub.update({"@" + k: v for k, v in ch.attrib.items()})
            key = tag
            if ch.attrib.get("Type"):
                key = f"{tag}_{ch.attrib['Type']}"
            d[key] = sub
        else:
            d[tag] = clean(ch.text)
    return d


def parse_airport_xml(text):
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    upd_raw = clean(root.findtext("Update_Time"))
    res = {"update_time_raw": upd_raw, "update_time": iso(parse_update_time(upd_raw)),
           "ground_stops": [], "ground_delays": [], "closures": [], "arr_dep_delays": [],
           "other": [], "delay_type_names": []}
    upd = parse_update_time(upd_raw)
    for dtp in root.findall("Delay_type"):
        name = clean(dtp.findtext("Name")) or ""
        res["delay_type_names"].append(name)
        for lst in dtp:
            if lst.tag == "Name":
                continue
            for item in lst:
                f = flatten(item)
                code = (f.get("ARPT") or "").upper()
                info = airport_info(code)
                base = {"arpt": code, "reason": f.get("Reason"), **info}
                if lst.tag == "Ground_Stop_List":
                    base.update(kind="ground_stop", end_time=end_time_info(f.get("End_Time"), upd))
                    res["ground_stops"].append(base)
                elif lst.tag == "Ground_Delay_List":
                    base.update(kind="ground_delay", avg=f.get("Avg"), max=f.get("Max"))
                    res["ground_delays"].append(base)
                elif lst.tag == "Airport_Closure_List":
                    base.update(kind="closure", start=f.get("Start"), reopen=f.get("Reopen"))
                    res["closures"].append(base)
                elif lst.tag == "Arrival_Departure_Delay_List":
                    ad = []
                    for k, v in f.items():
                        if k.startswith("Arrival_Departure") and isinstance(v, dict):
                            ad.append({"type": v.get("@Type") or k.split("_")[-1], "min": v.get("Min"),
                                       "max": v.get("Max"), "trend": v.get("Trend")})
                    base.update(kind="arr_dep_delay", delays=ad)
                    res["arr_dep_delays"].append(base)
                else:
                    base.update(kind="other", list=lst.tag, fields=f)
                    res["other"].append(base)
    return res


# ------------------------------------------------------ ATCSCC advisories
NATIONAL_PAT = re.compile(
    r"NATION\s*WIDE|NATIONAL\s+GROUND\s+STOP|NAS[\s-]*WIDE|SYSTEM\s*WIDE|ALL\s+(?:US\s+|CONUS\s+)?AIRPORTS|"
    r"ALL\s+DEPARTURES\s+(?:TO|FROM)\s+ALL|ALL\s+NAS\s+AIRPORTS|NATIONAL\s+AIRSPACE\s+SYSTEM", re.I)
GS_PAT = re.compile(r"GROUND\s*STOP|\bGS\b", re.I)
CNX_PAT = re.compile(r"\bCNX\b|CANCEL|\bCANX\b|RELEASE|RESUM(?:E|PTION)|LIFTED|TERMINATED|ENDED", re.I)
EXT_PAT = re.compile(r"EXTEN|\bEXT\b|EXTENDED|EXTENSION", re.I)
MOD_PAT = re.compile(r"\bMOD(?:IFIED|IFICATION)?\b|REVISED|UPDATE|AMEND", re.I)
FIELD_RE = re.compile(r"^\s*([A-Z][A-Z0-9 /\-()&.]{1,40}?):\s*(.*)$")
AIRLINE_HINT = re.compile(r"\b(AAL|DAL|UAL|SWA|ASA|QXE|JBU|NKS|FFT|AAY|SKW|ENY|RPA|EDV|JIA|PDT|ASH|GJS|FDX|UPS|"
                          r"HAL|SCX|MXY|VXP|AWI|CPZ|ACA|WJA|ROU|JZA|TSC|POE)\b")


def parse_signature(sig):
    """'26/10/27 11:37' (yy/mm/dd HH:MM, UTC)."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{2})\s+(\d{2}):?(\d{2})", sig or "")
    if not m:
        return None
    yy, mo, dd, hh, mi = map(int, m.groups())
    try:
        return dt.datetime(2000 + yy, mo, dd, hh, mi, tzinfo=UTC)
    except ValueError:
        return None


def _ddhhmm(day, hh, mi, ref):
    """Resolve day-of-month + time relative to a reference date (handles month rollover)."""
    if ref is None:
        return None
    best = None
    for mshift in (-1, 0, 1):
        y, m = ref.year, ref.month + mshift
        if m == 0:
            y, m = y - 1, 12
        if m == 13:
            y, m = y + 1, 1
        try:
            c = dt.datetime(y, m, day, hh % 24, mi, tzinfo=UTC) + (dt.timedelta(days=1) if hh == 24 else dt.timedelta())
        except ValueError:
            continue
        if best is None or abs(c - ref) < abs(best - ref):
            best = c
    return best


def parse_window(raw, ref):
    """Handles '14/2115Z - 14/2230Z', '27/1330 to 27/1630', '142115 - 142230', '271137-271700'."""
    if not raw:
        return None
    out = {"raw": raw.strip(), "start": None, "end": None, "until_further_notice": bool(re.search(r"UFN|FURTHER NOTICE", raw, re.I))}
    toks = re.findall(r"(\d{2})/(\d{2})(\d{2})Z?|(?<!\d)(\d{2})(\d{2})(\d{2})Z?(?!\d)", raw)
    pts = []
    for t in toks:
        if t[0]:
            pts.append(_ddhhmm(int(t[0]), int(t[1]), int(t[2]), ref))
        else:
            pts.append(_ddhhmm(int(t[3]), int(t[4]), int(t[5]), ref))
    if pts:
        out["start"] = iso(pts[0])
    if len(pts) > 1:
        out["end"] = iso(pts[1])
    return out


def parse_advisory_text(text, url=None, meta=None):
    """Parse a raw ATCSCC advisory body into structured fields."""
    meta = meta or {}
    text = text.replace("\xa0", " ").replace("\ufffd", " ")
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    head = next((ln.strip() for ln in lines if re.search(r"ADVZY", ln, re.I)), meta.get("title") or (lines[0].strip() if lines else ""))
    fields, cur = {}, None
    for ln in lines:
        m = FIELD_RE.match(ln)
        if m and not re.search(r"ADVZY", ln, re.I):
            cur = m.group(1).strip().upper()
            fields[cur] = m.group(2).strip()
        elif cur and not re.search(r"ADVZY", ln, re.I):
            fields[cur] = (fields[cur] + " " + ln.strip()).strip()
    hm = re.search(r"ADVZY\s+(\d+)\s+(\S+)\s+(\d{2}/\d{2}/\d{2,4})\s*(.*)$", head, re.I)
    number = hm.group(1) if hm else meta.get("number")
    fac = hm.group(2) if hm else meta.get("facility")
    date_s = hm.group(3) if hm else meta.get("date")
    adv_type = (hm.group(4).strip() if hm else (meta.get("type") or "")).upper()
    adv_date = None
    if date_s:
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                adv_date = dt.datetime.strptime(date_s, fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                pass
    signature = fields.get("SIGNATURE") or next((m.group(0) for m in [re.search(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}", lines[-1] if lines else "")] if m), None)
    issued = parse_signature(signature) or adv_date
    ref = issued or adv_date
    gs_period = fields.get("GROUND STOP PERIOD") or fields.get("GS PERIOD") or fields.get("CNX PERIOD") or fields.get("GS CNX PERIOD")
    event = fields.get("EVENT TIME") or gs_period
    eff = fields.get("EFFECTIVE TIME") or fields.get("VALID")
    reason = (fields.get("IMPACTING CONDITION") or fields.get("REASON") or fields.get("CAUSE") or
              fields.get("REMARKS") or fields.get("COMMENTS"))
    ctl = (fields.get("CTL ELEMENT") or fields.get("CTL ELMNT") or fields.get("CONTROL ELEMENT") or "").upper()
    dest = (fields.get("DESTINATION AIRPORT") or fields.get("DESTINATION AIRPORTS") or fields.get("ARRIVAL AIRPORT") or "").upper().strip()
    fac_incl = (fields.get("FACILITIES INCLUDED") or fields.get("DEP FACILITIES INCLUDED") or "").upper().strip()
    list_ctl = (meta.get("control") or "").upper()
    if not ctl:
        first = list_ctl.split("/")[0].strip()
        if dest and dest != "ALL":
            ctl = dest.split()[0]
        elif first and first not in ("DCC", "ATCSCC"):
            ctl = first
    etype = (fields.get("ELEMENT TYPE") or "").upper()
    flt_incl = fields.get("FLT INCL") or fields.get("FLIGHTS INCLUDED") or ""
    body_up = text.upper()
    is_gs = bool(GS_PAT.search(adv_type)) or ("GROUND STOP PERIOD" in fields)
    if not adv_type and meta.get("title"):
        adv_type = meta["title"].upper()
    action = "issued"
    if CNX_PAT.search(adv_type) or "CNX PERIOD" in fields or "GS CNX PERIOD" in fields:
        action = "cancellation"
    elif EXT_PAT.search(adv_type):
        action = "extension"
    elif MOD_PAT.search(adv_type):
        action = "modification"

    # scope classification
    airlines = sorted(set(AIRLINE_HINT.findall(flt_incl.upper())))
    national_hit = bool(NATIONAL_PAT.search(adv_type) or NATIONAL_PAT.search(body_up[:1500]))
    ctl_all = (ctl in ("ALL", "NAS", "ALL AIRPORTS", "CONUS") or etype in ("NAS", "NATIONAL") or dest == "ALL"
               or (fac_incl.startswith("ALL") and not dest and not ctl))
    if airlines and not re.search(r"\bALL\b", flt_incl.upper().replace("ALL FLIGHTS", "")) and (ctl_all or national_hit or "CDM" in adv_type):
        scope = "airline"
    elif ctl_all or national_hit:
        scope = "national"
    elif re.fullmatch(r"Z[A-Z]{2}", ctl or "") or etype in ("ARTCC", "FCA", "TRACON", "CENTER"):
        scope = "facility"
    elif ctl:
        scope = "airport"
    else:
        scope = "unknown"
    return {
        "id": f"{fac or 'DCC'}-{number}-{date_s}" if number else None,
        "number": number, "facility": fac, "date": date_s, "type": adv_type, "title": head,
        "is_ground_stop": is_gs, "action": action, "scope": scope, "ctl_element": ctl or None,
        "element_type": etype or None, "airlines": airlines, "flt_incl": flt_incl or None,
        "reason": reason, "issued_utc": iso(issued), "signature": signature,
        "event_window": parse_window(event, ref), "effective_window": parse_window(eff, ref),
        "probability_of_extension": fields.get("PROBABILITY OF EXTENSION"),
        "expect_update": fields.get("EXPECT UPDATE"), "destination": dest or None, "facilities_included": fac_incl or None,
        "dep_facilities": fields.get("DEP FACILITIES INCLUDED"),
        "url": url, "excerpt": "\n".join(lines[:40])[:2400],
    }


# The advisory list/detail endpoints and their HTML shapes are confirmed against
# live responses captured by discover.py; see fetch_advisories().
def fetch_advisories(now, prev=None):
    """Return {"ok": bool, "items": [...], "errors": [...], "checked": [...]}"""
    from advisories import fetch_advisory_list  # kept separate so it can track FAA format changes
    cache = {a["url"]: a for a in ((prev or {}).get("advisory_cache") or []) if a.get("url")}
    floors = {}
    # Other FAA sources reference advisory numbers; use them as completeness floors.
    r = http_get("https://nasstatus.faa.gov/api/operations-plan", timeout=20)
    links = []
    if r.get("ok"):
        links.append(r["body"])
    r = http_get(URL_EVENTS, timeout=20)
    if r.get("ok"):
        links.append(r["body"])
    for m in re.finditer(r"advn=(\d+)&adv_date=(\d{8})", " ".join(links)):
        floors[m.group(2)] = max(floors.get(m.group(2), 0), int(m.group(1)))
    return fetch_advisory_list(now, http_get, parse_advisory_text, cache, floors)


ADV_CACHE_HOURS = 36


def merge_advisory_cache(adv, prev, now):
    """Advisories are immutable. Keep ones seen in the last 36h so a UTC date rollover or a
    list glitch never silently drops a stop. Flags advisories that vanished from a list we did fetch."""
    cur = {a["url"]: a for a in adv.get("items", []) if a.get("url")}
    coverage = {}
    for c in adv.get("checked", []):
        if c.get("ok") and not c.get("incomplete"):
            d = c["day"]
            coverage[d[5:7] + d[8:10] + d[0:4]] = int(c.get("max_number") or 0)
    merged = list(adv.get("items", []))
    for a in (prev or {}).get("advisory_cache") or []:
        u = a.get("url")
        if not u or u in cur:
            continue
        issued = parse_iso(a.get("issued_utc")) or parse_iso(a.get("cached_at"))
        if issued and now - issued > dt.timedelta(hours=ADV_CACHE_HOURS):
            continue
        if a.get("irrelevant"):
            continue
        b = dict(a)
        b["listed_now"] = False
        d = a.get("adv_date") or ""
        try:
            num = int(re.sub(r"\D", "", str(a.get("number") or "")) or 0)
        except ValueError:
            num = 0
        b["vanished"] = bool(d in coverage and num and num <= coverage[d])
        merged.append(b)
    adv["items"] = merged
    cache = []
    for a in merged + [x for x in adv.get("stubs") or [] if x.get("url") not in cur]:
        c = {k: v for k, v in a.items() if k not in ("listed_now", "vanished")}
        c.setdefault("cached_at", iso(now))
        cache.append(c)
    return cache


# ------------------------------------------------------------ news
NEWS_QUERIES = ['"nationwide ground stop"', '"FAA" "ground stop" nationwide OR "all flights"']


def fetch_news(now):
    items, errors = [], []
    for q in NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") +
               "&hl=en-US&gl=US&ceid=US:en")
        r = http_get(url, timeout=20)
        if not r["ok"]:
            errors.append(r["error"])
            continue
        try:
            root = ET.fromstring(r["body"].encode("utf-8"))
        except ET.ParseError as e:
            errors.append(str(e))
            continue
        for it in root.iter("item"):
            title = clean(it.findtext("title"))
            pub = it.findtext("pubDate")
            try:
                pdt = parsedate_to_datetime(pub).astimezone(UTC)
            except Exception:
                continue
            age_h = (now - pdt).total_seconds() / 3600
            if age_h > 24 or age_h < -1:
                continue
            src = it.find("source")
            items.append({"title": title, "link": clean(it.findtext("link")),
                          "source": clean(src.text) if src is not None else None,
                          "published_utc": iso(pdt), "age_hours": round(age_h, 1)})
    seen, uniq = set(), []
    for i in sorted(items, key=lambda x: x["published_utc"], reverse=True):
        k = (i["title"] or "").lower()[:90]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(i)
    return {"ok": not errors or bool(uniq), "items": uniq[:8], "errors": errors}


# ----------------------------------------------------- national roll-up
def build_national(adv_res, prev, now):
    """Combine advisories into restriction chains (national / airline / facility / airport) with explicit states."""
    prev_nat = (prev or {}).get("national", {}) or {}
    prev_items = {i["key"]: i for i in prev_nat.get("restrictions", []) if i.get("key")}
    items = adv_res.get("items", [])
    gs = [a for a in items if a.get("is_ground_stop")]
    related = [a for a in items if not a.get("is_ground_stop") and a.get("scope") == "national"]
    chains = {}
    for a in sorted(gs, key=lambda x: (x.get("issued_utc") or "", int(re.sub(r"\D", "", str(x.get("number") or 0)) or 0))):
        key_el = a.get("ctl_element") or "?"
        if a["scope"] == "national":
            key_el = "ALL"
        if a["scope"] == "airline":
            key_el = "AIRLINE:" + ("/".join(a["airlines"]) if a.get("airlines") else key_el)
        key = f"{a['scope']}|{key_el}"
        c = chains.setdefault(key, {"key": key, "scope": a["scope"], "ctl_element": a.get("ctl_element"),
                                    "airlines": a.get("airlines"), "advs": []})
        c["advs"].append(a)
    out = []
    for key, c in chains.items():
        advs = c.pop("advs")
        prev_end = None
        for i, a in enumerate(advs):
            w = a.get("event_window") or a.get("effective_window") or {}
            end = w.get("end")
            if a["action"] == "cancellation":
                a["role"] = "cancellation"
            elif i == 0:
                a["role"] = "issued"
            elif prev_end and end and end > prev_end:
                a["role"] = "extension"
            else:
                a["role"] = "update"
            if a["action"] != "cancellation":
                prev_end = end or prev_end
        latest = advs[-1]
        governing = next((x for x in reversed(advs) if x["action"] != "cancellation"), latest)
        win = governing.get("event_window") or governing.get("effective_window") or {}
        end = parse_iso(win.get("end"))
        any_listed = any(x.get("listed_now") for x in advs)
        vanished = [x for x in advs if x.get("vanished")]
        if latest["action"] == "cancellation":
            state = "cancelled"
            note = f"FAA advisory {latest.get('number')} cancels/releases this stop" + (f" (issued {latest['issued_utc']})." if latest.get("issued_utc") else ".")
        elif not adv_res.get("ok"):
            state = (prev_items.get(key) or {}).get("state", "unconfirmed") if prev_items.get(key) else "unconfirmed"
            note = "FAA advisory source unavailable this run. Showing the last known state; not re-verified."
        elif vanished and not any_listed:
            state = "unconfirmed"
            note = ("No longer listed in the FAA advisory list for its date, and no cancellation was found. "
                    "It may still be in effect. Confirm with FAA.")
        elif end and end < now:
            state = "unconfirmed"
            note = ("Scheduled end time has passed, but no FAA cancellation or release advisory was found. "
                    "Treat as possibly still in effect.")
        else:
            state = "active"
            note = "In FAA advisories with no cancellation found."
        if win.get("until_further_notice") and state == "active":
            note += " Until further notice."
        p = prev_items.get(key) or {}
        out.append({**c, "state": state, "state_note": note, "latest": _slim(latest), "governing": _slim(governing),
                    "advisory_count": len(advs), "listed_now": any_listed,
                    "first_seen": p.get("first_seen") or iso(now), "last_seen": iso(now),
                    "changed": bool(p) and (p.get("state") != state or (p.get("latest") or {}).get("number") != latest.get("number")),
                    "is_new": not p,
                    "hours_past_end": round((now - end).total_seconds() / 3600, 1) if end and end < now else None,
                    "advisories": [_slim(a, short=True) for a in advs]})
    order = {"national": 0, "airline": 1, "facility": 2, "airport": 3, "unknown": 4}
    st_order = {"active": 0, "unconfirmed": 1, "cancelled": 2}
    out.sort(key=lambda o: (order.get(o["scope"], 9), st_order.get(o["state"], 9), -(int(re.sub(r"\D", "", str(o["latest"].get("number") or 0)) or 0))))
    nat = [o for o in out if o["scope"] == "national"]
    if not adv_res.get("ok"):
        status, headline = "unverified", "National status unverified"
    elif any(o["state"] == "active" for o in nat):
        status, headline = "national_stop_active", "Nationwide ground stop in effect"
    elif any(o["state"] == "unconfirmed" for o in nat):
        status, headline = "national_stop_unconfirmed", "Nationwide ground stop: end not confirmed by FAA"
    else:
        status, headline = "none_found", "No nationwide ground stop in today's FAA advisories"
    return {"status": status, "headline": headline, "verified": bool(adv_res.get("ok")), "partial": bool(adv_res.get("partial")),
            "checked": adv_res.get("checked", []), "errors": adv_res.get("errors", []),
            "advisories_in_lists": adv_res.get("list_count", 0), "ground_stop_advisories": len(gs),
            "latest_advisory": adv_res.get("latest"),
            "related": [_slim(a) for a in sorted(related, key=lambda x: x.get("issued_utc") or "", reverse=True)[:6]],
            "restrictions": out}


def _slim(a, short=False):
    keys = ["number", "role", "action", "type", "title", "issued_utc", "url", "listed_now", "vanished", "scope"]
    if not short:
        keys += ["reason", "event_window", "effective_window", "ctl_element", "list_control", "destination",
                 "facilities_included", "airlines", "flt_incl", "expect_update", "probability_of_extension",
                 "signature", "send_time", "excerpt", "detail_missing"]
    return {k: a.get(k) for k in keys if a.get(k) not in (None, "", [])}


# ----------------------------------------------------- airport diffing
def prog_key(p):
    return f"{p['kind']}|{p['arpt']}"


def prog_sig(p):
    if p["kind"] == "ground_stop":
        return [p.get("reason"), (p.get("end_time") or {}).get("raw")]
    if p["kind"] == "ground_delay":
        return [p.get("reason"), p.get("avg"), p.get("max")]
    if p["kind"] == "closure":
        return [p.get("reason"), p.get("start"), p.get("reopen")]
    return [p.get("reason"), json.dumps(p.get("delays"), sort_keys=True)]


def diff_airports(cur, prev_air, now):
    prev_progs = {}
    for lst in ("ground_stops", "ground_delays", "closures", "arr_dep_delays"):
        for p in (prev_air or {}).get(lst, []):
            prev_progs[prog_key(p)] = p
    seen_now = set()
    for lst in ("ground_stops", "ground_delays", "closures", "arr_dep_delays"):
        for p in cur[lst]:
            k = prog_key(p)
            seen_now.add(k)
            old = prev_progs.get(k)
            if old is None:
                p["change"] = "new"
                p["first_seen"] = iso(now)
            else:
                p["first_seen"] = old.get("first_seen") or iso(now)
                if prog_sig(old) != prog_sig(p):
                    p["change"] = "changed"
                    p["previous"] = {"sig": prog_sig(old)}
                    p["changed_at"] = iso(now)
                else:
                    # keep "changed"/"new" flag visible for ~1 hour
                    keep = old.get("change") in ("new", "changed") and parse_iso(old.get("changed_at") or old.get("first_seen")) and \
                        now - parse_iso(old.get("changed_at") or old.get("first_seen")) < dt.timedelta(minutes=60)
                    p["change"] = old.get("change") if keep else None
                    if keep and old.get("changed_at"):
                        p["changed_at"] = old["changed_at"]
                        p["previous"] = old.get("previous")
    unlisted = []
    for k, old in prev_progs.items():
        if k in seen_now:
            continue
        o = dict(old)
        o["change"] = "unlisted"
        o["unlisted_at"] = old.get("unlisted_at") or iso(now)
        o["unlisted_note"] = "No longer listed in the FAA airport feed. FAA did not report a cancellation in this feed."
        unlisted.append(o)
    for o in (prev_air or {}).get("unlisted", []):
        k = prog_key(o)
        if k in seen_now or any(prog_key(u) == k for u in unlisted):
            continue
        t = parse_iso(o.get("unlisted_at"))
        if t and now - t < dt.timedelta(hours=UNLISTED_KEEP_HOURS):
            unlisted.append(o)
    cur["unlisted"] = unlisted
    return cur


def enrich_with_events(air, events_body):
    """nasstatus airport-events JSON carries ISO UTC times and advisory links for the same programs."""
    try:
        ev = json.loads(events_body)
    except Exception as e:  # noqa: BLE001
        return f"airport-events parse error: {e}"
    by = {}
    for e in ev if isinstance(ev, list) else []:
        by[(e.get("airportId") or "").upper()] = e
    mapping = {"ground_stops": "groundStop", "ground_delays": "groundDelay", "closures": "airportClosure"}
    seen = set()
    for lst, key in mapping.items():
        for p in air.get(lst, []):
            e = by.get(p["arpt"]) or {}
            g = e.get(key)
            if lst == "closures" and not g:
                g = e.get("freeForm")
            if not g:
                continue
            seen.add((lst, p["arpt"]))
            p["start_utc"] = g.get("startTime")
            p["end_utc"] = g.get("endTime") or g.get("programExpirationTime")
            p["advisory_url"] = (g.get("advisoryUrl") or "").replace(" ", "%20") or None
            p["probability_of_extension"] = g.get("probabilityOfExtension")
            p["included_flights"] = g.get("includedFlights")
            p["center"] = g.get("center")
            p["source_timestamp"] = g.get("sourceTimeStamp") or g.get("updatedAt")
            if e.get("airportLongName") and not p.get("name"):
                p["name"] = e["airportLongName"]
                p["name_source"] = "FAA airport-events"
    extra = []
    for code, e in by.items():
        for lst, key in mapping.items():
            if key != "airportClosure" and e.get(key) and (lst, code) not in seen:
                g = e[key]
                extra.append({"arpt": code, "kind": lst, "end_utc": g.get("endTime"), "reason": g.get("impactingCondition"),
                              "advisory_url": (g.get("advisoryUrl") or "").replace(" ", "%20") or None,
                              **airport_info(code)})
    air["events_only"] = extra
    return None


# ------------------------------------------------------------- main
def src_state(name, prev_src, ok, now, extra=None, error=None):
    p = prev_src or {}
    s = {"ok": ok, "fetched_at": iso(now), "error": error,
         "last_success_at": iso(now) if ok else p.get("last_success_at"),
         "consecutive_failures": 0 if ok else int(p.get("consecutive_failures") or 0) + 1}
    s.update(extra or {})
    return s


def run(out_path, prev_path=None, fixtures=None):
    now = fixtures.get("now") if fixtures and fixtures.get("now") else now_utc()
    prev = None
    if prev_path and os.path.exists(prev_path):
        try:
            prev = json.load(open(prev_path, encoding="utf-8"))
        except Exception:
            prev = None
    prev_sources = (prev or {}).get("sources", {})

    # --- airport XML
    if fixtures and "airport_xml" in fixtures:
        r = fixtures["airport_xml"]
    else:
        r = http_get(URL_AIRPORT_XML)
    air, air_err = None, None
    if r.get("ok"):
        try:
            air = parse_airport_xml(r["body"])
        except Exception as e:  # noqa: BLE001
            air_err = f"Parse error: {e}"
    else:
        air_err = r.get("error")
    ev_err = None
    if air is not None:
        er = fixtures.get("airport_events") if fixtures and "airport_events" in fixtures else http_get(URL_EVENTS)
        ev_err = enrich_with_events(air, er["body"]) if er.get("ok") else er.get("error")
    if air is not None:
        upd = parse_iso(air["update_time"])
        age = round((now - upd).total_seconds() / 60, 1) if upd else None
        stale = age is None or age > AIRPORT_STALE_MIN
        air = diff_airports(air, (prev or {}).get("airports"), now)
        sa = src_state("airport_xml", prev_sources.get("airport_xml"), True, now,
                       {"url": URL_AIRPORT_XML, "source_update_time": air["update_time"],
                        "source_update_raw": air["update_time_raw"], "source_age_min": age, "stale": stale})
        air["carried_over"] = False
    else:
        sa = src_state("airport_xml", prev_sources.get("airport_xml"), False, now,
                       {"url": URL_AIRPORT_XML, "stale": True,
                        "source_update_time": (prev_sources.get("airport_xml") or {}).get("source_update_time"),
                        "source_update_raw": (prev_sources.get("airport_xml") or {}).get("source_update_raw")}, air_err)
        air = dict((prev or {}).get("airports") or {"ground_stops": [], "ground_delays": [], "closures": [],
                                                   "arr_dep_delays": [], "unlisted": [], "other": []})
        air["carried_over"] = bool(prev)

    # --- advisories
    if fixtures and "advisories" in fixtures:
        adv = fixtures["advisories"]
    else:
        try:
            adv = fetch_advisories(now, prev)
        except Exception as e:  # noqa: BLE001
            adv = {"ok": False, "items": [], "errors": [f"{type(e).__name__}: {e}"], "checked": []}
    adv_cache = merge_advisory_cache(adv, prev, now)
    nat = build_national(adv, prev, now)
    sv = src_state("advisories", prev_sources.get("advisories"), bool(adv.get("ok")), now,
                   {"url": URL_ADB, "list_urls": adv.get("checked", []), "count": len(adv.get("items", [])),
                    "latest_issued": (adv.get("latest") or {}).get("issued_utc")},
                   "; ".join(adv.get("errors", [])) or None)

    # --- news corroboration (only meaningful when national status needs it)
    if fixtures and "news" in fixtures:
        news = fixtures["news"]
    else:
        news = fetch_news(now)
    # Corroboration only: reporting can flag a mismatch, never set or clear FAA status.
    rep = [i for i in news.get("items", []) if re.search(r"nation\s*wide|national|across (the )?(us|u\.s\.|country)|all (us )?flights", i.get("title") or "", re.I)
           and re.search(r"ground stop|grounded|halt", i.get("title") or "", re.I) and (i.get("age_hours") or 99) <= 6]
    nat["reports_recent"] = len(rep)
    if rep and nat["status"] in ("none_found", "unverified"):
        nat["reports_mismatch"] = ("News outlets reported a nationwide ground stop within the last 6 hours, "
                                   "but FAA advisory data checked by Groundwatch does not currently confirm one. Check FAA directly.")
    sn = src_state("news", prev_sources.get("news"), bool(news.get("ok")), now,
                   {"provider": "Google News RSS", "count": len(news.get("items", []))},
                   "; ".join(news.get("errors", [])) or None)

    counts = {k: len(air.get(k, [])) for k in ("ground_stops", "ground_delays", "closures", "arr_dep_delays")}
    counts["unlisted"] = len(air.get("unlisted", []))
    events = []
    for lst in ("ground_stops", "ground_delays", "closures"):
        for p in air.get(lst, []):
            if p.get("change") in ("new", "changed") and (p.get("changed_at") or p.get("first_seen")) == iso(now):
                events.append({"t": iso(now), "what": p["change"], "kind": p["kind"], "arpt": p["arpt"]})
    for p in air.get("unlisted", []):
        if p.get("unlisted_at") == iso(now):
            events.append({"t": iso(now), "what": "unlisted", "kind": p["kind"], "arpt": p["arpt"]})
    history = ((prev or {}).get("history") or [])[-95:]
    history.append({"t": iso(now), "nat": nat["status"], "gs": counts["ground_stops"], "gdp": counts["ground_delays"],
                    "air_ok": sa["ok"], "adv_ok": sv["ok"]})
    log = (events + ((prev or {}).get("event_log") or []))[:60]

    status = {
        "schema": SCHEMA, "generated_at": iso(now),
        "run": {"id": os.environ.get("GITHUB_RUN_ID"), "url": (f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}" if os.environ.get("GITHUB_RUN_ID") else None)},
        "sources": {"airport_xml": sa, "advisories": sv, "news": sn,
                    "airport_events": {"ok": ev_err is None and sa["ok"], "url": URL_EVENTS, "error": ev_err, "fetched_at": iso(now)}},
        "national": nat,
        "airports": air,
        "counts": counts,
        "news": news.get("items", []),
        "event_log": log,
        "advisory_cache": adv_cache,
        "history": history,
        "links": {"faa_events": URL_EVENTS, "faa_nas": URL_NAS, "faa_adb": URL_ADB, "faa_adv_form": URL_ADV_FORM, "faa_airport_xml": URL_AIRPORT_XML},
    }
    tmp = out_path + ".tmp"
    json.dump(status, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, out_path)
    return status


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    out = sys.argv[1]
    prev = sys.argv[2] if len(sys.argv) > 2 else None
    s = run(out, prev)
    print(json.dumps({"generated_at": s["generated_at"], "national": s["national"]["status"],
                      "counts": s["counts"], "sources": {k: v["ok"] for k, v in s["sources"].items()}}))
