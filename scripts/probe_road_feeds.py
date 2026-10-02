"""What do the road-domain feeds actually return? Ask them, before writing code.

    python scripts/probe_road_feeds.py
    python scripts/probe_road_feeds.py --only chart-speed
    python scripts/probe_road_feeds.py --list

Writes a shape report into data/reference/roads/ and prints a summary. It
fetches nothing it keeps: no feed contents are stored, only the SHAPE of
what came back.

WHY THIS SCRIPT EXISTS AND WHY IT RUNS BEFORE ANYTHING ELSE

Phase G needs five feeds nobody in this project has looked at. The cost of
guessing at one of them is already measured. `alt_baro` in the adsb.fi
response is a STRING column carrying an integer-looking value plus the
sentinel 'ground'; the aviation analysis filtered it numerically, dropped
every real altitude, and reported 100 percent of independent targets at 0
feet. That ran, produced a number, and the number was wrong in a direction
nobody would question. It was caught only because 100 percent at exactly
zero is implausible on its face.

So this script does the one thing that would have prevented it: fetch each
endpoint once, and report the TYPE of every value next to its name.

WHAT IT REPORTS, AND WHY EACH ITEM IS THERE

    all keys, not the expected ones   On 2026-10-02 a query for
                                      `p_shift_control` against an artefact
                                      storing `p_control_shift` returned
                                      None, and None was one sentence from
                                      being published as a defect in the
                                      analysis. A record is summarised by
                                      every key it has.

    the type of each value            The alt_baro sighting above.

    an exact zero, flagged loudly     Mechanism R. A defaultdict reported
                                      122 aircraft holding nothing, and a
                                      dict comprehension turned 565 units
                                      into 78 and flipped a result toward
                                      the hypothesis. Both announced
                                      themselves as a suspiciously round
                                      zero first.

    an error object carrying HTTP 200 ArcGIS and Socrata both answer a bad
                                      query with 200 and an error body.
                                      `describe_sla` was written to catch
                                      exactly this and was defeated the next
                                      day by 483 polygons. A status code is
                                      not a verdict on the body.

    truncation, named as truncation   The read is capped. A capped read that
                                      reported a row COUNT would be a
                                      container reporting a size it does not
                                      hold -- the same defect class as above.
                                      So when the cap is hit the field is
                                      named `rows_at_least`, and `rows` is
                                      absent.

    the status, with no cause         BOEM's HTTP 500 was written down three
                                      times as "the service is down". It was
                                      not down; the request was too large.
                                      This script records the status and the
                                      reason phrase and infers nothing from
                                      either.

WHAT IT DELIBERATELY DOES NOT DO

It does not import `_bootstrap`, and it imports nothing from `angels`. Every
other script in this directory does both. This one is the script whose output
decides what the next script is allowed to assume, so it must be runnable
under any interpreter on the machine -- a probe that fails to start because
the project is not importable has told you nothing about the feeds.

It probes only endpoints that need NO credential. Nothing here reads .env,
and no key is sent anywhere. Virginia's WZDx feed and WMATA's GTFS-Realtime
feed both require keys and are therefore absent from the list, named below
as excluded rather than quietly skipped.

It does not parse GTFS-Realtime protobuf. That needs a dependency, and the
registry step chooses which transit feeds to poll; probing a guessed one
would be answering a question nobody asked.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "reference" / "roads"

CAP_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 30.0
UA = "ANGELS-capstone-probe/1 (+github.com/Mc3urry) python-urllib"

# Endpoints that need no credential. `why` is the column of the Phase G
# architecture this feed belongs to, so the report says what each one is for.
ENDPOINTS: tuple[dict[str, str], ...] = (
    {"name": "wzdx-registry",
     "url": "https://data.transportation.gov/api/views/69qe-yiui/rows.csv"
            "?accessType=DOWNLOAD",
     "why": "G0 registry. 43 feeds; which are keyless is read from here, "
            "not assumed."},
    {"name": "mobility-registry",
     "url": "https://files.mobilitydatabase.org/feeds_v2.csv",
     "why": "G0 registry. GTFS-Realtime sources with authentication_type "
            "per feed. Expect this one to exceed the cap."},
    {"name": "mdot-wzdx",
     "url": "https://filter.ritis.org/wzdx_v4.1/mdot.geojson",
     "why": "G1 declared layer, Maryland. The only AOI jurisdiction that "
            "publishes work zones openly."},
    {"name": "chart-speed",
     "url": "https://chart.maryland.gov/TrafficSpeedData/GetSpeedData",
     "why": "G2 independent layer. A roadside sensor counts EVERY vehicle, "
            "private cars included. This is the all-vehicles feed."},
    {"name": "chart-cameras",
     "url": "https://chart.maryland.gov/TrafficCameras/GetTrafficCameras",
     "why": "G3 mask. Camera locations as the denominator -- where an "
            "observation was possible. No imagery is fetched."},
    {"name": "chart-incidents",
     "url": "https://chart.maryland.gov/Incidents/GetIncidents",
     "why": "G1 declared layer, second channel. An agency saying what it "
            "believes is happening."},
    {"name": "mta-crz",
     "url": "https://data.ny.gov/resource/t6yz-b64h.json?$limit=5",
     "why": "Tier 3. The only US boundary where a PRIVATE vehicle acquires "
            "a duty by crossing a line."},

    # ---- added 2026-10-02, after the first run ------------------------
    # The three chart.maryland.gov paths each answered HTTP 200 with 40 to
    # 80 kB of HTML. Three explanations fit that evidence equally well and
    # the first run cannot distinguish them: the URL is a human page and the
    # machine feed lives elsewhere; the URL is right and content-negotiates
    # on Accept, which the first run sent as */*; or the URL is right, the
    # page is live, and the data behind it is down -- which the page's own
    # table claims, in the words "This data is currently unavailable".
    #
    # So the second run asks a question per explanation instead of choosing
    # one. The rule being obeyed is the script's own flag text: do not infer
    # which. It is also mechanism M, the error that is true and the cause
    # that is a guess.
    {"name": "chart-speed-json",
     "url": "https://chart.maryland.gov/TrafficSpeedData/GetSpeedData",
     "accept": "application/json",
     "why": "Same URL as chart-speed, asking for JSON. Separates content "
            "negotiation from a wrong URL."},
    {"name": "chart-incidents-json",
     "url": "https://chart.maryland.gov/Incidents/GetIncidents",
     "accept": "application/json",
     "why": "Same URL as chart-incidents, asking for JSON."},
    {"name": "chart-cameras-arcgis",
     "url": "https://mdgeodata.md.gov/imap/rest/services/Transportation/"
            "MD_TrafficCameras/FeatureServer/0/query"
            "?f=geojson&where=1%3D1&outFields=*&resultRecordCount=5",
     "why": "G3 mask, resolved. CHART camera locations as a queryable "
            "FeatureServer rather than a web page. maxRecordCount 550."},
    {"name": "md-aadt-arcgis",
     "url": "https://geodata.md.gov/imap/rest/services/Transportation/"
            "MD_AnnualAverageDailyTraffic/FeatureServer/0/query"
            "?f=geojson&where=1%3D1&outFields=*&resultRecordCount=5",
     "why": "G3 exposure denominator. Annual average daily traffic counts "
            "EVERY vehicle including private cars. Static, not real time."},
    # ---- added 2026-10-02, after the second run ----------------------
    # The AADT layer answers with every vehicle-class column NULL in the
    # first record: CAR_AADT, TRUCK_AADT, BUS_AADT, MOTORCYCLE_AADT and
    # every _PCT_ derived from them. If that holds across the layer then
    # the class split -- the one field that would separate private cars
    # from trucks and buses -- does not exist, and `AADT` is an
    # all-vehicles total with no breakdown.
    #
    # One record is not the layer, and guessing which it is would be the
    # 483-polygon mistake in miniature. So the question is asked of the
    # layer with returnCountOnly, which is also the query that once
    # answered instantly while a full fetch returned HTTP 500 and got
    # written down three times as the service being down.
    {"name": "md-aadt-count-all",
     "url": "https://mdgeodata.md.gov/imap/rest/services/Transportation/"
            "MD_AnnualAverageDailyTraffic/FeatureServer/0/query"
            "?where=1%3D1&returnCountOnly=true&f=json",
     "why": "How many segments the AADT layer holds in total. The "
            "denominator of the denominator."},
    {"name": "md-aadt-count-car",
     "url": "https://mdgeodata.md.gov/imap/rest/services/Transportation/"
            "MD_AnnualAverageDailyTraffic/FeatureServer/0/query"
            "?where=CAR_AADT%20IS%20NOT%20NULL&returnCountOnly=true&f=json",
     "why": "How many segments have a private-car count at all. This "
            "number decides whether private vehicles can be separated "
            "from trucks and buses, or only counted among them."},

    {"name": "md-aadt-arcgis-alt",
     "url": "https://mdgeodata.md.gov/imap/rest/services/Transportation/"
            "MD_AnnualAverageDailyTraffic/FeatureServer/0/query"
            "?f=geojson&where=1%3D1&outFields=*&resultRecordCount=5",
     "why": "The same layer on the other iMAP host. Two hostnames appear "
            "in Maryland documentation and this settles which one serves "
            "it, rather than one being chosen by pattern."},
)

# Named, not skipped. Kill criterion 3 of PHASE-G.md: an exclusion appears
# in the artefact or it did not happen.
EXCLUDED: tuple[dict[str, str], ...] = (
    {"name": "vdot-wzdx", "reason": "API key required (SmarterRoads token)"},
    {"name": "wmata-gtfs-rt", "reason": "API key required (free, not yet held)"},
    {"name": "fdot-volume-speed",
     "reason": "ArcGIS REST endpoint not yet resolved; portal page only"},
    {"name": "gtfs-rt-vehicle-positions",
     "reason": "protobuf; feed chosen by the registry step, not guessed here"},
)

# `exceededtransferlimit` was in this tuple and is deliberately not any
# more: it is a truncation notice, not an error, and it appears alongside
# real data. See the correction note in summarise_json.
ERROR_KEYS = ("error", "errors", "errorcode", "code", "message", "fault")


def type_name(v: object) -> str:
    """The type of a value, plus the shape of a container.

    Reported instead of the value for everything except short scalars,
    because the point of this script is the schema and because a feed can
    carry a plate number, a driver name or a credential in a field nobody
    expected. A type is safe to paste into a chat; a value is not.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        # The alt_baro case: a string that looks like a number is the trap.
        looks_numeric = False
        s = v.strip()
        if s:
            try:
                float(s)
                looks_numeric = True
            except ValueError:
                looks_numeric = False
        return "str(numeric-looking)" if looks_numeric else f"str(len={len(v)})"
    if isinstance(v, list):
        return f"list(len={len(v)})"
    if isinstance(v, dict):
        return f"dict(keys={len(v)})"
    return type(v).__name__


def record_shape(rec: object) -> dict[str, str]:
    """Every key of a record with the type of its value. Every key."""
    if not isinstance(rec, dict):
        return {"<not a mapping>": type_name(rec)}
    return {str(k): type_name(v) for k, v in rec.items()}


def looks_like_error_object(obj: object) -> bool:
    """A body that is an error even though the status said 200."""
    if not isinstance(obj, dict):
        return False
    low = {str(k).lower() for k in obj.keys()}
    if "exceededtransferlimit" in low and obj.get("exceededTransferLimit"):
        return True
    # An error object is small and says so. A data object that merely has a
    # 'message' field among many is not one.
    return bool(low & set(ERROR_KEYS)) and len(obj) <= 4


def summarise_json(body: bytes, truncated: bool) -> dict:
    """Shape of a JSON body. Pure: no network, no clock, no filesystem."""
    out: dict = {"kind": "json", "flags": []}
    if truncated:
        out["flags"].append("TRUNCATED: body was capped, so JSON could not "
                            "be parsed in full")
        out["parsed"] = False
        return out
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        out["parsed"] = False
        out["flags"].append(f"NOT VALID JSON: {exc.msg} at line {exc.lineno}")
        return out
    out["parsed"] = True
    out["top_level"] = type_name(obj)

    if isinstance(obj, dict):
        out["keys"] = sorted(str(k) for k in obj.keys())
        # GeoJSON, or an envelope with the records one level down.
        if str(obj.get("type", "")).lower() == "featurecollection":
            feats = obj.get("features")
            feats = feats if isinstance(feats, list) else []
            out["kind"] = "geojson"
            out["features"] = len(feats)
            if not feats:
                out["flags"].append("EXACT ZERO features -- instrument this "
                                    "before believing it")
            else:
                out["geometry_types"] = sorted({
                    str(((f or {}).get("geometry") or {}).get("type"))
                    for f in feats[:200]})
                props = (feats[0] or {}).get("properties")
                out["first_feature_properties"] = record_shape(props)
                out["feed_info"] = record_shape(obj.get("road_event_feed_info")) \
                    if isinstance(obj.get("road_event_feed_info"), dict) else None
        else:
            lists = {k: v for k, v in obj.items() if isinstance(v, list)}
            if lists:
                k = max(lists, key=lambda k: len(lists[k]))
                out["record_list_key"] = k
                out["records"] = len(lists[k])
                if not lists[k]:
                    out["flags"].append(f"EXACT ZERO records under '{k}' -- "
                                        "instrument this before believing it")
                else:
                    out["first_record"] = record_shape(lists[k][0])

        # CORRECTED 2026-10-02, on the run that first exercised it.
        #
        # This check used to run FIRST, before the shape was known, and it
        # fired on both ArcGIS GeoJSON responses -- 5 real features each,
        # with real fields -- because an ArcGIS FeatureCollection carries
        # `exceededTransferLimit` beside `type` and `features`, which is
        # three keys, one of them in ERROR_KEYS, and the test was "any
        # error-ish key AND at most four keys". Two valid data responses
        # were labelled ERROR OBJECT.
        #
        # That is mechanism N a second time: a guard written for a class,
        # defeated by the next instance of it. It is also the exact failure
        # named in the comment above `_CT_OK` in this same file -- flagging
        # a body that is fine trains the reader to ignore the flags, which
        # is how a real one gets missed. Writing the warning down did not
        # prevent committing the thing it warned about.
        #
        # The fix is an ordering, not a longer key list: a body that parsed
        # as a recognised DATA shape is not an error object, whatever its
        # keys are called. Only an unrecognised body is asked the question.
        recognised = ("features" in out) or ("records" in out)
        if not recognised and looks_like_error_object(obj):
            out["flags"].append("ERROR OBJECT: the body is an error even "
                                "though the transport succeeded")

        # And `exceededTransferLimit` is not an error at all. It is the
        # server saying it returned fewer rows than the query asked for --
        # truncation, decided upstream, which is the one thing this script
        # already refuses to let pass silently on the client side. It gets
        # its own flag because treating it as an error would have hidden it
        # behind a false positive, and ignoring it would be the 483-polygon
        # mistake again: a partial answer used as a whole one.
        if obj.get("exceededTransferLimit"):
            out["flags"].append("SERVER-SIDE TRUNCATION: the service set "
                                "exceededTransferLimit, so this is a page "
                                "of the layer and not the layer")

        # A tiny envelope of scalars gets its VALUES reported, not just its
        # types. This is the one exception to the types-only rule, and it is
        # narrow on purpose: at most four keys, each a number, a bool, a
        # null or a string of 32 characters or fewer. That is the shape of a
        # `returnCountOnly` answer and of a short error message, and neither
        # can carry a plate, a name or a token. A count whose value was
        # withheld would make the question unanswerable, which is the only
        # reason to relax the rule at all.
        if not recognised and len(obj) <= 4 and all(
                v is None or isinstance(v, (int, float, bool))
                or (isinstance(v, str) and len(v) <= 32)
                for v in obj.values()):
            out["scalars"] = {str(k): v for k, v in obj.items()}
    elif isinstance(obj, list):
        out["records"] = len(obj)
        if not obj:
            out["flags"].append("EXACT ZERO records -- instrument this "
                                "before believing it")
        else:
            out["first_record"] = record_shape(obj[0])
            keys: set[str] = set()
            for rec in obj[:200]:
                if isinstance(rec, dict):
                    keys |= {str(k) for k in rec.keys()}
            out["union_of_keys_first_200"] = sorted(keys)
    return out


def summarise_csv(body: bytes, truncated: bool) -> dict:
    """Shape of a CSV body.

    When the read was capped the row count is reported as `rows_at_least`
    and `rows` is absent, because a capped read that printed `rows` would be
    a container reporting a size it does not hold.
    """
    out: dict = {"kind": "csv", "flags": []}
    text = body.decode("utf-8-sig", errors="replace")
    if truncated:
        # Drop the final line: the cap almost certainly cut it in half.
        text = text[:text.rfind("\n") + 1] if "\n" in text else ""
        out["flags"].append("TRUNCATED: read was capped")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        out["flags"].append("EXACT ZERO rows -- instrument this")
        return out
    header = [h.strip() for h in rows[0]]
    out["columns"] = header
    out["column_count"] = len(header)
    data = rows[1:]
    if truncated:
        out["rows_at_least"] = len(data)
    else:
        out["rows"] = len(data)
        if not data:
            out["flags"].append("EXACT ZERO data rows -- instrument this")
    if data:
        first = dict(zip(header, data[0]))
        out["first_row"] = {k: type_name(v) for k, v in first.items()}
        ragged = sum(1 for r in data if len(r) != len(header))
        if ragged:
            out["flags"].append(f"RAGGED: {ragged} rows do not have "
                                f"{len(header)} fields")
    return out


def summarise_xml(body: bytes, truncated: bool) -> dict:
    out: dict = {"kind": "xml", "flags": []}
    if truncated:
        out["flags"].append("TRUNCATED: body was capped, so XML could not "
                            "be parsed in full")
        out["parsed"] = False
        return out
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        out["parsed"] = False
        out["flags"].append(f"NOT VALID XML: {exc}")
        return out
    out["parsed"] = True
    out["root_tag"] = root.tag
    kids = list(root)
    out["child_count"] = len(kids)
    if not kids:
        out["flags"].append("EXACT ZERO child elements -- instrument this")
    else:
        tags: dict[str, int] = {}
        for k in kids:
            tags[k.tag] = tags.get(k.tag, 0) + 1
        out["child_tags"] = tags
        first = kids[0]
        out["first_child_fields"] = {c.tag: ("empty" if (c.text or "").strip()
                                             == "" else type_name(c.text))
                                     for c in first}
        out["first_child_attributes"] = sorted(first.attrib.keys())
    return out


# What a server may legitimately call each kind of body. A GeoJSON served as
# application/json is not a disagreement, and flagging it would train the
# reader to ignore the flags -- which is how a real one gets missed.
_CT_OK: dict[str, tuple[str, ...]] = {
    "json": ("json",),
    "geojson": ("json", "geo+json"),
    "csv": ("csv", "text/plain", "octet-stream"),
    "xml": ("xml",),
}


def summarise(body: bytes, content_type: str, truncated: bool) -> dict:
    """Dispatch on what the body IS, not on what the URL suggested.

    A .geojson path that answers with HTML, or a JSON endpoint that answers
    with an XML error, is exactly the case worth catching, so the sniff is
    on the first non-space byte and the declared content type is recorded
    beside it rather than trusted.
    """
    head = body.lstrip()[:1]
    ct = (content_type or "").lower()
    sniff = body.lstrip()[:64].lower()
    if sniff.startswith(b"<!doctype html") or sniff.startswith(b"<html"):
        # The likeliest real failure for a public feed: a sign-in page, a
        # terms interstitial or a CDN error, served with HTTP 200. Named as
        # HTML so it cannot be mistaken for malformed data.
        out = {"kind": "html",
               "flags": ["HTML, NOT DATA: the endpoint answered with a web "
                         "page. Expect a sign-in, a terms gate or a CDN "
                         "error -- but do not infer which."]}
    elif head in (b"{", b"["):
        out = summarise_json(body, truncated)
    elif head == b"<":
        out = summarise_xml(body, truncated)
    elif b"," in body[:4096] or b"\t" in body[:4096]:
        out = summarise_csv(body, truncated)
    else:
        out = {"kind": "unknown", "flags": ["UNRECOGNISED: body is not "
                                            "JSON, XML or delimited text"]}
    out["declared_content_type"] = content_type or "(none sent)"
    allowed = _CT_OK.get(out["kind"], ())
    if ct and allowed and not any(tok in ct for tok in allowed):
        out["flags"].append(f"CONTENT TYPE DISAGREES: server said "
                            f"'{content_type}', body looks like {out['kind']}")
    return out


def probe(url: str, cap: int = CAP_BYTES, timeout: float = TIMEOUT_S,
          accept: str = "*/*") -> dict:
    """One GET. Records the status and infers no cause from it.

    `accept` is explicit and recorded in the result, because an endpoint
    that content-negotiates returns a different body for a different header
    and a report that did not say which header was sent would be a reading
    whose conditions are unknown -- the same omission as a stored distance
    with no record of what it is a distance to.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": accept})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(cap + 1)
            truncated = len(body) > cap
            body = body[:cap]
            rec = {
                "ok": True,
                "status": resp.status,
                "reason": resp.reason,
                "bytes_read": len(body),
                "truncated": truncated,
                "elapsed_s": round(time.monotonic() - started, 3),
                "content_length_header": resp.headers.get("Content-Length"),
                "summary": summarise(body,
                                     resp.headers.get("Content-Type", ""),
                                     truncated),
            }
    except urllib.error.HTTPError as exc:
        detail = b""
        try:
            detail = exc.read(2048)
        except Exception:
            pass
        rec = {"ok": False, "status": exc.code, "reason": exc.reason,
               "elapsed_s": round(time.monotonic() - started, 3),
               "error_class": "HTTPError",
               "body_first_2kb_kind": summarise(detail, "", False)["kind"]
               if detail else "(empty)",
               "cause": "NOT INFERRED -- a status code is a fact and its "
                        "cause is an inference (mechanism M)"}
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        rec = {"ok": False, "status": None, "reason": str(exc),
               "elapsed_s": round(time.monotonic() - started, 3),
               "error_class": type(exc).__name__,
               "cause": "NOT INFERRED -- could be the host, the network, a "
                        "proxy or TLS. Re-run before concluding anything."}
    rec["url"] = url
    rec["accept_sent"] = accept
    return rec


def human(name: str, why: str, rec: dict) -> None:
    print(f"\n  {name}")
    print(f"    {why}")
    if rec.get("accept_sent", "*/*") != "*/*":
        print(f"    Accept: {rec['accept_sent']}")
    if not rec.get("ok"):
        print(f"    FAILED  status={rec.get('status')}  "
              f"{rec.get('error_class')}: {rec.get('reason')}")
        print(f"    {rec.get('cause')}")
        return
    s = rec["summary"]
    print(f"    {rec['status']} {rec['reason']}  "
          f"{rec['bytes_read']} bytes in {rec['elapsed_s']} s"
          f"{'  (CAPPED)' if rec['truncated'] else ''}")
    print(f"    kind={s['kind']}  declared={s['declared_content_type']}")
    for field in ("features", "records", "rows", "rows_at_least",
                  "child_count", "column_count"):
        if field in s:
            print(f"    {field}={s[field]}")
    fields = (s.get("first_feature_properties") or s.get("first_record")
              or s.get("first_row") or s.get("first_child_fields") or {})
    if fields:
        print(f"    first record, every key with its type:")
        for k in sorted(fields):
            print(f"      {k:<34} {fields[k]}")
    if s.get("scalars"):
        print(f"    body is a small envelope, values shown:")
        for k in sorted(s["scalars"]):
            print(f"      {k:<34} {s['scalars'][k]!r}")
    if s.get("geometry_types"):
        print(f"    geometry: {', '.join(s['geometry_types'])}")
    if s.get("feed_info"):
        # The spec version the feed SAYS it is. G1 must compare this against
        # the registry's claim and refuse a disagreement, rather than
        # trusting a filename the way `limit_sets` once did.
        print(f"    road_event_feed_info, declared by the feed itself:")
        for k in sorted(s["feed_info"]):
            print(f"      {k:<34} {s['feed_info'][k]}")
    if s.get("columns"):
        print(f"    columns: {', '.join(s['columns'])}")
    for f in s.get("flags", []):
        print(f"    FLAG  {f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", action="append", default=[],
                    help="probe just this endpoint, by name; repeatable")
    ap.add_argument("--list", action="store_true",
                    help="print the endpoint names and exit")
    ap.add_argument("--cap", type=int, default=CAP_BYTES,
                    help=f"bytes to read per response (default {CAP_BYTES})")
    ap.add_argument("--out", type=Path, default=None,
                    help="where to write the report (default "
                         "data/reference/roads/probe-<date>.json)")
    args = ap.parse_args(argv)

    if args.list:
        for ep in ENDPOINTS:
            print(f"  {ep['name']:<20} {ep['url']}")
        print("\n  excluded, and why:")
        for ex in EXCLUDED:
            print(f"  {ex['name']:<20} {ex['reason']}")
        return 0

    chosen = [e for e in ENDPOINTS
              if not args.only or e["name"] in args.only]
    unknown = set(args.only) - {e["name"] for e in ENDPOINTS}
    if unknown:
        print(f"  no such endpoint: {', '.join(sorted(unknown))}",
              file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc)
    report = {
        "_what": "Shape of each road-domain feed as returned on the date "
                 "below. Not feed contents: types and key names only.",
        "_probed_at_utc": stamp.isoformat(timespec="seconds"),
        "_cap_bytes": args.cap,
        "_excluded": list(EXCLUDED),
        "_caution": "A shape recorded once is not a contract. Re-probe "
                    "before trusting a schema that a published result "
                    "depends on.",
        "endpoints": {},
    }

    print(f"\n  Probing {len(chosen)} endpoint(s). No credentials are sent.")
    for ep in chosen:
        rec = probe(ep["url"], cap=args.cap,
                    accept=ep.get("accept", "*/*"))
        rec["why"] = ep["why"]
        report["endpoints"][ep["name"]] = rec
        human(ep["name"], ep["why"], rec)

    # Named to the second, not to the day. The first version of this script
    # named the report `probe-<date>.json`, and the very next run -- a
    # follow-up on the same afternoon, probing different endpoints -- would
    # have replaced it without a word. A file whose name says "the probe of
    # 2 October" while holding only the last five endpoints asked that day
    # is the same defect as every other sighting in this catalogue: an
    # artefact describing something other than what it contains, with no
    # way to tell you so. Colons are omitted because Windows forbids them in
    # filenames, which is where this runs.
    default = OUT_DIR / f"probe-{stamp.strftime('%Y-%m-%dT%H%M%SZ')}.json"
    out = args.out or default
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and args.out is None:
        print(f"\n  refusing to overwrite {out}", file=sys.stderr)
        return 1
    out.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n",
                   encoding="utf-8")

    reached = sum(1 for r in report["endpoints"].values() if r.get("ok"))
    flagged = sum(1 for r in report["endpoints"].values()
                  if r.get("ok") and r["summary"].get("flags"))
    print(f"\n  {reached} of {len(chosen)} reached; {flagged} carry flags.")
    print(f"  report: {out}")
    if reached != len(chosen):
        print("  A failure here is a fact about one attempt, not about the "
              "service. Re-run before writing it down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
