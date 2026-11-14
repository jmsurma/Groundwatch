"""Scenario tests for the collector (offline). Run: python3 tests/test_scenarios.py"""
import datetime as dt, json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
import groundwatch as gw, advisories as av

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 13, 23, 0, tzinfo=UTC)
TMP = tempfile.mkdtemp()
FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {info}"))
    if not cond:
        FAILS.append(name)


def xml(update, gs=(), gdp=()):
    g = "".join(f"<Program><ARPT>{a}</ARPT><Reason>{r}</Reason><End_Time>{e}</End_Time></Program>" for a, r, e in gs)
    d = "".join(f"<Ground_Delay><ARPT>{a}</ARPT><Reason>{r}</Reason><Avg>{av_}</Avg><Max>1 hour</Max></Ground_Delay>" for a, r, av_ in gdp)
    body = f"<AIRPORT_STATUS_INFORMATION><Update_Time>{update.strftime('%a %b %d %H:%M:%S %Y')} GMT</Update_Time>"
    if g:
        body += f"<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List>{g}</Ground_Stop_List></Delay_type>"
    if d:
        body += f"<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List>{d}</Ground_Delay_List></Delay_type>"
    return {"ok": True, "body": body + "</AIRPORT_STATUS_INFORMATION>"}


def adv(n, title, event, eff, sig, reason="COMMUNICATIONS FAILURE", extra="DESTINATION AIRPORT:ALL\nFACILITIES INCLUDED: ALL"):
    txt = f"ATCSCC ADVZY {n:03d} DCC 11/13/2026 {title}\nMESSAGE:\nEVENT TIME: {event}\n{extra}\nREASON: {reason}\nEFFECTIVE TIME:\n{eff}\nSIGNATURE:\n{sig}"
    a = gw.parse_advisory_text(txt, url=f"https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn={n}",
                               meta={"control": "DCC"})
    a["adv_date"] = "11132026"
    a["listed_now"] = True
    return a


def advres(items, ok=True):
    return {"ok": ok, "items": items, "errors": [] if ok else ["HTTP 503"], "list_count": 120 if ok else 0,
            "checked": [{"day": "2026-11-13", "ok": ok, "rows": 120 if ok else 0, "max_number": 200}, {"day": "2026-11-12", "ok": ok, "rows": 150 if ok else 0, "max_number": 200}]}


def run(fx, prev=None, now=T0):
    pp = None
    if prev:
        pp = os.path.join(TMP, "prev.json")
        json.dump(prev, open(pp, "w"))
    fx = {"now": now, "news": {"ok": True, "items": []}, "airport_events": {"ok": True, "body": "[]"}, **fx}
    return gw.run(os.path.join(TMP, "out.json"), pp, fx)


NAT1 = adv(111, "NATIONAL GROUND STOP", "13/2050 - 13/2300", "132054-132300", "26/11/13 20:54")
NAT2 = adv(120, "NATIONWIDE GROUND STOP", "13/2245 - 14/0400", "132246-140430", "26/11/13 22:46")
CNX = adv(130, "NATIONWIDE GROUND STOP CANCELLATION", "13/2330 - 13/2330", "132330-132330", "26/11/13 23:30")

# 1 nationwide stop, empty airport feed
s = run({"airport_xml": xml(T0), "advisories": advres([NAT1, NAT2])})
n = s["national"]
check("1 national stop with empty airport feed -> active", n["status"] == "national_stop_active", n["status"])
check("1 airport-specific count stays 0 and separate", s["counts"]["ground_stops"] == 0)
r = n["restrictions"][0]
check("1 scope national, reason + windows parsed", r["scope"] == "national" and r["governing"]["reason"] == "COMMUNICATIONS FAILURE"
      and r["governing"]["event_window"]["end"] == "2026-11-14T04:00:00Z" and r["governing"]["effective_window"]["start"] == "2026-11-13T22:46:00Z", r["governing"])
check("1 issuance time from signature", r["latest"]["issued_utc"] == "2026-11-13T22:46:00Z")
# 2 extension
check("2 second advisory with later end = extension", [a["role"] for a in r["advisories"]] == ["issued", "extension"], r["advisories"])
# 3 cancellation
s = run({"airport_xml": xml(T0), "advisories": advres([NAT1, NAT2, CNX])}, now=T0 + dt.timedelta(minutes=40))
r = s["national"]["restrictions"][0]
check("3 cancellation advisory -> cancelled", r["state"] == "cancelled" and s["national"]["status"] == "none_found", (r["state"], s["national"]["status"]))
# 4 expired nominal end, no cancellation
s = run({"airport_xml": xml(T0), "advisories": advres([NAT1])}, now=T0 + dt.timedelta(hours=1))
check("4 nominal end passed, no CNX -> unconfirmed, never 'ended'", s["national"]["status"] == "national_stop_unconfirmed"
      and s["national"]["restrictions"][0]["state"] == "unconfirmed", s["national"]["status"])
# 5 advisory disappears from a list we fetched
prev = run({"airport_xml": xml(T0), "advisories": advres([NAT2])})
s = run({"airport_xml": xml(T0), "advisories": advres([])}, prev=prev, now=T0 + dt.timedelta(minutes=15))
r = s["national"]["restrictions"][0] if s["national"]["restrictions"] else {}
check("5 vanished advisory -> unconfirmed, not cancelled", s["national"]["status"] == "national_stop_unconfirmed" and r.get("state") == "unconfirmed"
      and "no cancellation" in r.get("state_note", "").lower(), (s["national"]["status"], r.get("state_note")))
# 5b UTC rollover: advisory dated yesterday not in today's list -> still active (cache), not dropped
prev = run({"airport_xml": xml(T0), "advisories": advres([NAT2])})
roll = advres([]); roll["checked"] = [{"day": "2026-11-14", "ok": True, "rows": 3}, {"day": "2026-11-13", "ok": True, "rows": 140}]
roll["items"] = [dict(NAT2)]  # yesterday list still has it
s = run({"airport_xml": xml(T0), "advisories": roll}, prev=prev, now=T0 + dt.timedelta(hours=1, minutes=10))
check("5b date rollover keeps stop active", s["national"]["status"] == "national_stop_active", s["national"]["status"])
# 6 stale airport data
s = run({"airport_xml": xml(T0 - dt.timedelta(minutes=45)), "advisories": advres([])})
check("6 stale FAA Update_Time flagged", s["sources"]["airport_xml"]["stale"] is True and s["sources"]["airport_xml"]["source_age_min"] == 45.0)
# 7 upstream failures keep last good data with warnings
prev = run({"airport_xml": xml(T0, gs=[("ORD", "weather / thunderstorms", "7:00 pm CST")]), "advisories": advres([NAT2])})
s = run({"airport_xml": {"ok": False, "error": "URLError timeout"}, "advisories": advres([], ok=False)}, prev=prev, now=T0 + dt.timedelta(minutes=15))
check("7 advisory failure -> 'unverified'", s["national"]["status"] == "unverified" and s["national"]["headline"] == "National status unverified")
check("7 last known national restriction retained", any(r["scope"] == "national" for r in s["national"]["restrictions"]))
check("7 airport failure keeps last report, marked carried over", s["airports"]["carried_over"] and s["counts"]["ground_stops"] == 1
      and s["sources"]["airport_xml"]["ok"] is False and s["sources"]["airport_xml"]["consecutive_failures"] == 1)
check("7 last success time preserved", s["sources"]["airport_xml"]["last_success_at"] == "2026-11-13T23:00:00Z")
# 8 empty list + nothing readable -> unverified (never all-clear)
def fake(url, timeout=40, data=None, tries=2):
    if "adv_list" in url:
        return {"ok": True, "body": "<th class=caption>ATCSCC ADVISORIES FOR 2026-11-13</th> NO ADVISORIES MATCH YOUR SELECTION CRITERIA!"}
    return {"ok": True, "body": "<div class=\"mainArea\">Advisories Database - Invalid/Missing Input Data</div>"}
res = av.fetch_advisory_list(T0, fake, gw.parse_advisory_text, {}, sleep_ok=False)
check("8 empty list + failed walk -> advisories not ok", res["ok"] is False, res)
s = run({"airport_xml": xml(T0), "advisories": res})
check("8 ... national shows unverified", s["national"]["status"] == "unverified")
# 8b empty list but walk finds advisories -> verified via fallback
def fake2(url, timeout=40, data=None, tries=2):
    if "adv_list" in url:
        return {"ok": True, "body": "ATCSCC ADVISORIES FOR 2026-11-13 NO ADVISORIES MATCH"}
    import re
    k = int(re.search(r"advn=(\d+)", url).group(1))
    if k <= 5:
        title = "NATIONWIDE GROUND STOP" if k == 5 else "OPERATIONS PLAN"
        return {"ok": True, "body": f'<div class="mainArea">ATCSCC\xa0ADVZY\xa0{k:03d}\xa0DCC\xa011/13/2026\xa0{title}<br>EVENT TIME: 13/2245 - 14/0400<br>DESTINATION AIRPORT:ALL<br>REASON: COMMUNICATIONS FAILURE<br>EFFECTIVE TIME:<br>132246-140430<br>SIGNATURE:<br>26/11/13 22:46</div>'}
    return {"ok": True, "body": "<div class=\"mainArea\">Invalid/Missing Input Data</div>"}
res = av.fetch_advisory_list(T0, fake2, gw.parse_advisory_text, {}, sleep_ok=False)
s = run({"airport_xml": xml(T0), "advisories": res})
check("8b list empty, number walk finds national stop", res["ok"] and s["national"]["status"] == "national_stop_active", (res["checked"], s["national"]["status"]))
# 8c walk interrupted by network errors -> unverified
def fake3(url, timeout=40, data=None, tries=2):
    if "adv_list" in url:
        return {"ok": True, "body": "ATCSCC ADVISORIES FOR 2026-11-13 NO ADVISORIES MATCH"}
    return {"ok": False, "error": "HTTPError 503"}
res = av.fetch_advisory_list(T0, fake3, gw.parse_advisory_text, {}, sleep_ok=False)
check("8c walk blocked by 503s -> not verified", res["ok"] is False)
# 9 Canadian airport + airline-specific classification
s = run({"airport_xml": xml(T0, gs=[("YUL", "other", "6:30 pm EST")]), "advisories": advres([])})
g = s["airports"]["ground_stops"][0]
check("9 YUL resolved to Montreal, Canada; EST kept", g["country"] == "CA" and "Montr" in g["name"] and g["end_time"]["raw"] == "6:30 pm EST"
      and g["end_time"]["utc_estimate"] == "2026-11-13T23:30:00Z", g)
al = gw.parse_advisory_text("ATCSCC ADVZY 145 DCC 11/12/2026 CDM GROUND STOP\nCTL ELEMENT: ALL\nGROUND STOP PERIOD: 12/2217Z - 12/2315Z\nFLT INCL: AAL ONLY\nIMPACTING CONDITION: OTHER / OTHER\nSIGNATURE: 26/11/12 22:17", meta={})
check("9 airline-only stop classified airline, not national", al["scope"] == "airline" and al["airlines"] == ["AAL"], al["scope"])
ap = gw.parse_advisory_text("ATCSCC ADVZY 109 MDW/ZAU 11/13/2026 CDM GROUND STOP\nCTL ELEMENT: MDW\nELEMENT TYPE: APT\nGROUND STOP PERIOD: 13/2017Z - 13/2130Z\nIMPACTING CONDITION: EQUIPMENT / OUTAGE\nSIGNATURE: 26/11/13 20:17", meta={})
check("9 airport stop classified airport", ap["scope"] == "airport" and ap["ctl_element"] == "MDW")
# 10 airport diffs: new / changed / unlisted
p1 = run({"airport_xml": xml(T0, gs=[("ORD", "weather", "7:00 pm CST")], gdp=[("SFO", "wind", "1 hour")]), "advisories": advres([])})
p2 = run({"airport_xml": xml(T0, gs=[("ORD", "weather", "8:00 pm CST"), ("EWR", "volume", "7:30 pm EST")]), "advisories": advres([])}, prev=p1, now=T0 + dt.timedelta(minutes=15))
ch = {g["arpt"]: g.get("change") for g in p2["airports"]["ground_stops"]}
check("10 changed + new flagged", ch == {"ORD": "changed", "EWR": "new"}, ch)
un = p2["airports"]["unlisted"]
check("10 dropped program = 'unlisted', not 'cancelled'", len(un) == 1 and un[0]["arpt"] == "SFO" and "did not report a cancellation" in un[0]["unlisted_note"], un)
# 11 walk that stops short of an advisory number already known -> incomplete / unverified
cache = {f"https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn={k}": {"url": f"https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn={k}", "number": str(k), "irrelevant": True, "title": "x"} for k in (1, 2)}
cache["https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn=120"] = {"url": "https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn=120", "number": "120", "title": "ATCSCC ADVZY 120", "type": "NATIONWIDE GROUND STOP"}
res = av.fetch_advisory_list(T0, fake2, gw.parse_advisory_text, cache, sleep_ok=False)
check("11 walk short of known advisory 120 -> not verified", res["ok"] is False, res["checked"][0])
# 12 vanished only within verified coverage
prev = run({"airport_xml": xml(T0), "advisories": advres([NAT2])})
partial = advres([]); partial["checked"] = [{"day": "2026-11-13", "ok": True, "rows": 0, "max_number": 11}, {"day": "2026-11-12", "ok": True, "max_number": 160}]
s = run({"airport_xml": xml(T0), "advisories": partial}, prev=prev, now=T0 + dt.timedelta(minutes=15))
check("12 advisory beyond verified coverage is not 'vanished'", s["national"]["status"] == "national_stop_active", s["national"]["status"])
# 13 cached advisory without text is re-read during a number walk
cache = {"https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn=5": {"url": "https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn=5", "number": "005", "title": "x", "type": "NATIONWIDE GROUND STOP", "detail_missing": True, "is_ground_stop": True, "scope": "national"}}
res = av.fetch_advisory_list(T0, fake2, gw.parse_advisory_text, cache, sleep_ok=False)
a5 = [a for a in res["items"] if str(a.get("number")).lstrip("0") == "5"]
check("13 text-less cached advisory gets re-read", a5 and a5[0].get("reason") == "COMMUNICATIONS FAILURE" and not a5[0].get("detail_missing"), a5)
print("\n%d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
