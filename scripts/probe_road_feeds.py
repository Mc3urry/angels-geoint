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
import hashlib
import io
import json
import re
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

    # ---- added 2026-10-06, for G2b -----------------------------------
    # The border selection needs state polygons: a transit feed is relevant
    # to the publication-regime test when its service area straddles a line
    # across which the duty to publish changes. 184 of 191 feeds carry a
    # usable bounding box; nothing in this project carries a state boundary.
    #
    # Two candidates, probed rather than chosen. The Census page lists the
    # 2024 cartographic bundle at 1:20,000,000 and does NOT list a
    # state-only file at that scale for 2024, so the national bundle is the
    # documented url and the per-geography one would be a guess -- which is
    # how two non-existent ArcGIS services got into fetch_limits.py and
    # stayed for five days.
    #
    # The TIGERweb service would be better if it answers: GeoJSON over HTTP,
    # no zip, no shapefile reader, and no dependency on the analysis extra.
    # Whether it answers is measured here.
    {"name": "census-cb-2024-bundle",
     "url": "https://www2.census.gov/geo/tiger/GENZ2024/shp/"
            "cb_2024_us_all_20m.zip",
     "why": "G2b boundaries. The documented 2024 cartographic bundle, "
            "2.2 MB, holding every geography at 1:20,000,000. A zip, so "
            "this only confirms it exists and its size."},
    {"name": "tigerweb-states",
     "url": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
            "State_County/MapServer/0/query"
            "?f=geojson&where=1%3D1&outFields=STATE,NAME&resultRecordCount=3",
     "why": "G2b boundaries, preferred if it answers: state polygons as "
            "GeoJSON over HTTP, needing no zip, no shapefile reader and "
            "nothing from the analysis extra."},

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

# WZDx v4 calls it `feed_info`. v3 called it `road_event_feed_info`. Tried in
# that order, and the key that answered is recorded rather than assumed: a
# feed that still uses the v3 name is telling you something about itself.
FEED_INFO_KEYS = ("feed_info", "road_event_feed_info")


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
    """A body that is an error even though the status said 200.

    CORRECTED AGAIN 2026-10-05, by a test written against a recorded body.

    On 2026-10-05 this check was moved to run after the body's shape was
    known, which stopped two valid ArcGIS responses being flagged as errors.
    **It did not fix this function.** The `exceededTransferLimit` early
    return was left in place, so the predicate still answered True for a
    paginated FeatureCollection -- the wrong answer had merely been made
    unreachable through one caller.

    That is not the same thing as being right, and it was found the moment a
    test called the predicate directly instead of going through `summarise`.
    Every synthetic check written that afternoon went through `summarise` and
    so agreed with it.

    A fix that hides a wrong answer leaves the next caller to find it. The
    clause is gone; truncation has its own handling and is not an error.
    """
    if not isinstance(obj, dict):
        return False
    low = {str(k).lower() for k in obj.keys()}
    # An error object is small and says so. A data object that merely has a
    # 'message' field among many is not one.
    return bool(low & set(ERROR_KEYS)) and len(obj) <= 4


def summarise_json(body: bytes, truncated: bool,
                   requested_limit: bool = False) -> dict:
    """Shape of a JSON body. Pure: no network, no clock, no filesystem.

    `requested_limit` says whether the URL asked the service for a limited
    number of records. It is passed in rather than parsed here so this stays
    a function of its arguments alone.
    """
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

                # CORRECTED 2026-10-05. This looked only for
                # `road_event_feed_info`, which is what WZDx v3 calls the
                # object. v4 renamed it to `feed_info`, every feed in the
                # registry is v4 or later, and MDOT's is v4.1 -- so the
                # lookup found nothing, recorded `"feed_info": null`, and
                # printed nothing at all. A reader of that report would
                # conclude the feed does not declare its version. It
                # declares 4.1, in a key this code was not asking for.
                #
                # That is sighting 26 with a different field name: a probe
                # that looks in the wrong place and reports a confident
                # nothing. The difference is only that this one reported its
                # nothing as a silent null rather than as a printed zero.
                #
                # Both names are now tried, in v4-first order, and WHICH one
                # answered is recorded -- because a value whose source is
                # unstated is the next defect along.
                out["feed_info"] = None
                out["feed_info_key"] = None
                out["declared_version"] = None
                for fk in FEED_INFO_KEYS:
                    if isinstance(obj.get(fk), dict):
                        out["feed_info_key"] = fk
                        out["feed_info"] = record_shape(obj[fk])
                        out["declared_version"] = obj[fk].get("version")
                        break
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
        # REFINED 2026-10-05, and the refinement is rule 16.
        #
        # The first version of this flag fired on both ArcGIS layers. It was
        # telling the truth -- the layers are paginated -- but the query had
        # passed `resultRecordCount=5`, so the truncation was REQUESTED. A
        # flag that fires on a condition the caller created spends the
        # reader's attention on nothing, and the flags share one channel, so
        # it degrades every other check in this file. That is the same
        # failure as the ERROR OBJECT false positive two sections up, in a
        # smaller and more forgivable form, which is exactly why it gets
        # fixed rather than tolerated.
        #
        # So a requested cap is RECORDED and an imposed one is FLAGGED.
        if obj.get("exceededTransferLimit"):
            if requested_limit:
                out["pagination"] = ("a page, because the query asked for "
                                     "one: the URL passes a record limit, "
                                     "so exceededTransferLimit here is not "
                                     "a decision the service made")
            else:
                out["flags"].append("SERVER-SIDE TRUNCATION: the service set "
                                    "exceededTransferLimit without being "
                                    "asked to, so this is a page of the "
                                    "layer and not the layer")

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


# A record cap the CALLER asked for. Named per service rather than guessed:
# ArcGIS uses resultRecordCount, Socrata uses $limit, and some ArcGIS
# deployments honour maxRecords. Anything not on this list is treated as a
# cap the caller did NOT ask for, which errs toward flagging -- the safe
# direction for a question about completeness.
_LIMIT_PARAMS = ("resultrecordcount", "$limit", "maxrecords", "limit=")


def caller_capped(url: str) -> bool:
    """Did this URL ask the service to return fewer records than it has?"""
    low = url.lower()
    return any(tok in low for tok in _LIMIT_PARAMS)


def summarise(body: bytes, content_type: str, truncated: bool,
              requested_limit: bool = False) -> dict:
    """Dispatch on what the body IS, not on what the URL suggested.

    A .geojson path that answers with HTML, or a JSON endpoint that answers
    with an XML error, is exactly the case worth catching, so the sniff is
    on the first non-space byte and the declared content type is recorded
    beside it rather than trusted.
    """
    head = body.lstrip()[:1]
    ct = (content_type or "").lower()
    sniff = body.lstrip()[:64].lower()

    # Binary first, before any text sniff gets a chance.
    #
    # ADDED 2026-10-06. The Census cartographic zip has commas in its first
    # four kilobytes -- compressed bytes contain every byte value -- so the
    # delimiter test called it CSV and `csv.reader` raised on a newline in
    # an unquoted field. A classifier whose cheapest test is "does it
    # contain a comma" will call almost any binary file a spreadsheet.
    #
    # Magic numbers are checked by name so the report says what arrived
    # rather than that it was unreadable: a zip where GeoJSON was expected
    # is a different fact from a corrupt response.
    magic = (
        (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip (empty)"),
        (b"\x1f\x8b", "gzip"), (b"%PDF", "pdf"),
        (b"\x89PNG", "png"), (b"GIF8", "gif"), (b"\xff\xd8\xff", "jpeg"),
        (b"SQLite format 3", "sqlite"), (b"\x00\x00\x00 ftyp", "mp4"),
    )
    for sig, name in magic:
        if body.startswith(sig):
            return {"kind": "binary", "binary_format": name,
                    "declared_content_type": content_type or "(none sent)",
                    "bytes": len(body),
                    "flags": [f"BINARY, {name}: nothing here is parsed as "
                              f"text. Status and size are the measurement"]}
    if b"\x00" in body[:8192]:
        return {"kind": "binary", "binary_format": "unknown",
                "declared_content_type": content_type or "(none sent)",
                "bytes": len(body),
                "flags": ["BINARY, format unrecognised: a NUL byte in the "
                          "first 8 kB. Not parsed as text"]}
    if sniff.startswith(b"<!doctype html") or sniff.startswith(b"<html"):
        # The likeliest real failure for a public feed: a sign-in page, a
        # terms interstitial or a CDN error, served with HTTP 200. Named as
        # HTML so it cannot be mistaken for malformed data.
        out = {"kind": "html",
               "flags": ["HTML, NOT DATA: the endpoint answered with a web "
                         "page. Expect a sign-in, a terms gate or a CDN "
                         "error -- but do not infer which."]}
    elif head in (b"{", b"["):
        out = summarise_json(body, truncated, requested_limit)
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


# How much of a body to keep as a test fixture. The Mobility Database CSV is
# 2.6 MB and nothing is learned from committing all of it; 256 kB holds the
# header and several thousand rows. A clipped fixture is RECORDED as clipped
# in the manifest, because a fixture whose length is unstated is a stored
# measurement without the input that defines it, and a test asserting a row
# count against a clipped file would be measuring the clip.
FIXTURE_BYTES = 256 * 1024

# A URL that looks like it carries a credential is never saved to a fixture,
# whatever the caller asked for. None of the endpoints in this file need a
# key -- that is why they are in this file -- but the flag is general and the
# cost of being wrong once is a secret in a public repository.
CREDENTIAL_HINTS = ("token", "key=", "apikey", "api_key", "secret",
                    "password", "signature", "sig=")


def looks_credentialed(url: str) -> bool:
    low = url.lower()
    return any(h in low for h in CREDENTIAL_HINTS)


# ADDED 2026-10-05, after GitHub secret scanning found a Google API key in a
# committed fixture.
#
# `looks_credentialed` checks the URL. It was written, tested, and felt like
# the credential guard -- and the exposure came through the other side. The
# CHART cameras page is a public web page that embeds a Google Maps browser
# key in its own markup, at line 292 of 79,692 bytes, and this script saved
# the page verbatim because the URL was clean.
#
# The guard covered the surface I was thinking about. Nothing checked the
# payload. A body is the larger surface and the one whose contents are not
# this project's to publish.
BODY_CREDENTIAL_PATTERNS = {
    "google api key": re.compile(rb"AIza[0-9A-Za-z_\-]{35}"),
    "google oauth client":
        re.compile(rb"[0-9]+-[0-9a-z]{32}\.apps\.googleusercontent\.com"),
    "aws access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "bearer token": re.compile(rb"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
    "key in an assignment": re.compile(
        rb"(?i)(api[_-]?key|apikey|access[_-]?token|client[_-]?secret)"
        rb"\s*[:=]\s*[\"'][A-Za-z0-9._\-]{16,}[\"']"),
    "token in a url": re.compile(rb"(?i)[?&]token=[A-Za-z0-9._\-]{20,}"),
    "private key block": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def body_credential(body: bytes) -> str | None:
    """The name of the first credential-shaped string in a body, or None.

    Reports the PATTERN NAME and never the match. A function that returned
    the secret so the caller could log it would be its own version of this
    mistake.
    """
    for name, rx in BODY_CREDENTIAL_PATTERNS.items():
        if rx.search(body):
            return name
    return None


def safe_summarise(body: bytes, content_type: str, truncated: bool,
                   requested_limit: bool = False) -> dict:
    """`summarise`, with its exceptions turned into a reported outcome.

    The summariser does a lot of speculative parsing of bodies it has never
    seen, which is the job. Any of it can raise. When it does, that is a
    finding about one response and not a reason to lose the others, so the
    error is recorded in the same shape as every other result and the run
    goes on.

    The exception TYPE and message are kept, because "it failed" without
    saying how is the defect this whole project is a catalogue of.
    """
    try:
        return summarise(body, content_type, truncated, requested_limit)
    except Exception as exc:                # noqa: BLE001 - that is the point
        return {
            "kind": "unsummarisable",
            "declared_content_type": content_type or "(none sent)",
            "bytes": len(body),
            "summary_error": f"{type(exc).__name__}: {str(exc)[:160]}",
            "flags": [f"THE SUMMARISER RAISED on this body: "
                      f"{type(exc).__name__}. The response arrived and was "
                      f"not understood, which is a fact about the parser as "
                      f"much as about the body"],
        }


def probe(url: str, cap: int = CAP_BYTES, timeout: float = TIMEOUT_S,
          accept: str = "*/*", keep_body: bool = False) -> dict:
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
                # CONTAINED 2026-10-06, after an unhandled parse error in
                # the summariser killed a whole run.
                #
                # This script's entire premise is that a failure is a fact
                # about ONE endpoint: it prints "NOT INFERRED" rather than
                # guessing causes, and it reports twelve results when the
                # thirteenth is down. Then a `csv.reader` raised on a zip
                # and took the process with it -- including the other
                # endpoint in the same run, which may well have answered.
                #
                # A probe that cannot survive a surprising response is not a
                # probe. The failure is recorded where every other failure
                # is recorded, and the run continues.
                "summary": safe_summarise(
                    body, resp.headers.get("Content-Type", ""),
                    truncated, caller_capped(url)),
            }
            if keep_body:
                # Underscored and popped by the caller before the report is
                # written. A raw body in a JSON report would be a 2 MB
                # artefact pretending to be a summary.
                rec["_body"] = body
                rec["_content_type"] = resp.headers.get("Content-Type", "")
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


def save_fixture(out_dir: Path, name: str, rec: dict,
                 limit: int = FIXTURE_BYTES) -> dict | None:
    """Write what the server actually sent, so a test can be a recording.

    WHY THIS EXISTS, IN ONE SENTENCE FROM THE DEFECTS NOTE

    Sighting 28 -- a guard that called two valid ArcGIS responses errors --
    was caught by a human reading a flag that contradicted the data four
    lines above it, and section 4 of `docs/reporting-defects.md` says plainly
    that nothing automated could have caught it, because "the fixtures were
    written from what the author expected servers to send, so the test suite
    agreed with the bug."

    The remedy is not more fixtures. It is fixtures that are **recordings**.
    An ArcGIS FeatureCollection really does carry `exceededTransferLimit`
    beside `type` and `features`; nobody writing a fixture by hand puts it
    there, which is exactly why the bug survived twenty-two tests.

    Returns the manifest entry, or None when nothing was saved.
    """
    if not rec.get("ok") or "_body" not in rec:
        return None
    if looks_credentialed(rec["url"]):
        return {"name": name, "saved": False,
                "reason": "the URL looks like it carries a credential"}
    # An HTML page is never saved, whatever it contains.
    #
    # The narrow lesson from the leak is that the body needs scanning too.
    # The wider one is that a scanner finds only the patterns it knows, and
    # the CHART pages were never worth keeping in the first place: what this
    # project learns from them is their status, their content type and their
    # length, all of which live in the manifest. The page itself is 80 kB of
    # somebody else's markup, carrying their analytics ids, their session
    # tokens and, as it turned out, their Google Maps key.
    #
    # Removing the class is a better guard than catching instances of it.
    kind = (rec.get("summary") or {}).get("kind")
    if kind == "html":
        return {"name": name, "saved": False,
                "url": rec["url"], "status": rec["status"],
                "bytes_read": rec["bytes_read"],
                "content_type": rec.get("_content_type", ""),
                "accept_sent": rec.get("accept_sent", "*/*"),
                "reason": "an HTML page is not saved as a fixture. Its "
                          "status, content type and length are recorded "
                          "here and are everything this project learns from "
                          "it; the markup is somebody else's and carries "
                          "their identifiers"}
    body = rec["_body"]
    clipped = len(body) > limit
    kept = body[:limit]

    # The body, which is the surface the URL check does not cover and the one
    # that actually leaked.
    #
    # NARROWED 2026-10-05, an hour after it was written. The first version
    # scanned the whole body read and refused the file if anything matched
    # anywhere. That refused the Mobility catalogue, whose 2 MB holds a feed
    # url with a token in it -- 1.8 MB past the clip, in bytes this script
    # was never going to write. The committed 262 kB is clean, and was
    # verified clean.
    #
    # The thing being protected is what gets PUBLISHED, so the scan is of
    # what will be written. A wider scan is not a stronger guard here, it is
    # a guard pointed at the wrong bytes, and the cost of pointing it wrong
    # is a usable recording thrown away -- which is how a suite ends up back
    # on hand-written fixtures.
    #
    # The wider fact is recorded rather than dropped: if the source carries
    # a credential outside the saved region, the manifest says so, because a
    # future clip at a different boundary would be a different question.
    hit = body_credential(kept)
    if hit:
        return {"name": name, "saved": False,
                "url": rec["url"],
                "status": rec["status"],
                "bytes_read": rec["bytes_read"],
                "content_type": rec.get("_content_type", ""),
                "reason": f"the body contains a {hit}. Not saved: this is "
                          f"somebody else's credential, embedded in their "
                          f"own page, and publishing it is not this "
                          f"project's to do"}
    beyond = body_credential(body) if clipped else None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.body"
    path.write_bytes(kept)
    return {
        "name": name,
        "saved": True,
        "file": path.name,
        "url": rec["url"],
        "accept_sent": rec.get("accept_sent", "*/*"),
        "status": rec["status"],
        "content_type": rec.get("_content_type", ""),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        # Stated, not implied. A test that asserted a row count against a
        # clipped body would be measuring the clip.
        "bytes_read": rec["bytes_read"],
        "bytes_saved": len(kept),
        "clipped": clipped,
        "clip_limit": limit,
        "sha256_of_saved": hashlib.sha256(kept).hexdigest(),
        # Stated when true, absent when not. The saved bytes are clean --
        # that is what the refusal above guarantees -- and the source is
        # not, which anyone choosing a different clip boundary needs to
        # know before they choose it.
        "credential_beyond_clip": beyond,
        "note": ("VERBATIM server response, clipped to the limit above. "
                 "Not a hand-written fixture, which is the entire point."
                 if clipped else
                 "VERBATIM server response, complete. Not a hand-written "
                 "fixture, which is the entire point."),
    }


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
    if s.get("pagination"):
        print(f"    note: {s['pagination']}")
    if s.get("scalars"):
        print(f"    body is a small envelope, values shown:")
        for k in sorted(s["scalars"]):
            print(f"      {k:<34} {s['scalars'][k]!r}")
    if s.get("geometry_types"):
        print(f"    geometry: {', '.join(s['geometry_types'])}")
    if s["kind"] == "geojson":
        # Printed either way, including when nothing was found. Silence is
        # how the v3/v4 key mix-up above went unnoticed: the field was null
        # in the report and absent from the console, which reads as a feed
        # that declares no version rather than as a lookup that missed.
        if s.get("feed_info"):
            print(f"    {s['feed_info_key']}, declared by the feed itself"
                  f" -- version {s.get('declared_version')!r}:")
            for k in sorted(s["feed_info"]):
                print(f"      {k:<34} {s['feed_info'][k]}")
        else:
            print(f"    no feed_info and no road_event_feed_info: this body "
                  f"does not declare a spec version")
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
    ap.add_argument("--save-bodies", type=Path, default=None, metavar="DIR",
                    help="also write each response VERBATIM into DIR, with a "
                         "manifest, for use as test fixtures. Recordings, "
                         "not hand-written expectations -- see sighting 28")
    ap.add_argument("--fixture-bytes", type=int, default=FIXTURE_BYTES,
                    help=f"clip each saved body at this many bytes "
                         f"(default {FIXTURE_BYTES}); the clip is recorded")
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
    fixtures: list[dict] = []
    for ep in chosen:
        rec = probe(ep["url"], cap=args.cap,
                    accept=ep.get("accept", "*/*"),
                    keep_body=args.save_bodies is not None)
        rec["why"] = ep["why"]
        if args.save_bodies is not None:
            entry = save_fixture(args.save_bodies, ep["name"], rec,
                                 limit=args.fixture_bytes)
            if entry:
                entry["why"] = ep["why"]
                fixtures.append(entry)
        # Popped unconditionally, before the record can reach the report.
        rec.pop("_body", None)
        rec.pop("_content_type", None)
        report["endpoints"][ep["name"]] = rec
        human(ep["name"], ep["why"], rec)

    if args.save_bodies is not None:
        man = args.save_bodies / "manifest.json"
        args.save_bodies.mkdir(parents=True, exist_ok=True)
        man.write_text(json.dumps({
            "_what": "Verbatim responses kept as test fixtures. Recordings, "
                     "not hand-written expectations. Sighting 28 survived "
                     "twenty-two tests because the fixtures were written "
                     "from what the author expected servers to send.",
            "_probed_at_utc": stamp.isoformat(timespec="seconds"),
            "_fixture_bytes": args.fixture_bytes,
            "bodies": fixtures,
        }, indent=2) + "\n", encoding="utf-8")
        kept = sum(1 for f in fixtures if f.get("saved"))
        refused = sum(1 for f in fixtures if not f.get("saved"))
        clip = sum(1 for f in fixtures if f.get("clipped"))
        beyond = sum(1 for f in fixtures if f.get("credential_beyond_clip"))
        print(f"\n  saved {kept} body/bodies to {args.save_bodies}, "
              f"refused {refused}")
        if clip:
            print(f"  {clip} clipped, and recorded as clipped")
        if beyond:
            print(f"  {beyond} source(s) carry a credential beyond the clip; "
                  f"the saved bytes do not, and the manifest says so")
        print(f"  manifest: {man}")

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
