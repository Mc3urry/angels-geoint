"""Context layers for the 3D view: keyless public sources, parsed.

    satellites   CelesTrak TLEs (stations, visual, GPS)      positions in the browser (SGP4)
    alpr         OpenStreetMap nodes tagged surveillance:type=ALPR, all of the US

CONTEXT, NOT EVIDENCE. Neither is part of any ANGELS measurement and none is
read by any analysis script. They are on the map to orient, the way the
reference project (God's Eye View) uses them. Every payload says so.

Earthquakes, wildfire perimeters and weather radar were here until
2026-10-28, when Joshua asked for them to go.

The ALPR layer is shown under the viewer-only scope exception (README,
"Scope"). It shows where licence-plate cameras are mapped, from public
OpenStreetMap tags. It never shows what they read.

This file is pure: parsing and validation only. The network and the cache
live in angels/api/routes/context.py.
"""

from __future__ import annotations

from typing import Any

CONTEXT = "context, not evidence: not read by any ANGELS analysis"


def parse_tle(text: str, group: str) -> list[dict[str, str]]:
    """Three-line TLE text to [{name, line1, line2, group}]. Malformed
    triples are skipped, never guessed at."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out = []
    i = 0
    while i + 2 < len(lines):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 ") and len(l1) >= 69 and len(l2) >= 69:
            out.append({"name": name.strip(), "line1": l1, "line2": l2, "group": group})
            i += 3
        else:
            i += 1
    return out


# THE WHOLE COUNTRY, ONCE A DAY. The first version asked Overpass for the
# view in front of the camera and refused anything over 4 square degrees,
# so the default DC view (about 9) never asked at all and the layer read 0
# (2026-10-28). Now one query covers the United States, and the API keeps
# the answer for a day: one polite request instead of one per pan.
def overpass_query_us() -> str:
    return ('[out:json][timeout:240][maxsize:536870912];'
            'area["ISO3166-1"="US"][admin_level=2]->.us;'
            'node["surveillance:type"="ALPR"](area.us);'
            'out body qt;')


def slim_alpr(doc: dict[str, Any]) -> dict[str, Any]:
    """Overpass JSON to compact rows: [lon, lat, osm_id, operator,
    manufacturer, direction]. Compact because the country has tens of
    thousands, and a FeatureCollection of them is several times the size."""
    if not isinstance(doc, dict) or "elements" not in doc:
        raise ValueError("Overpass did not return elements")
    rows = []
    for el in doc.get("elements") or []:
        if el.get("type") != "node" or el.get("lat") is None:
            continue
        t = el.get("tags") or {}
        rows.append([round(float(el["lon"]), 6), round(float(el["lat"]), 6), el.get("id"),
                     t.get("operator", ""), t.get("manufacturer", ""),
                     t.get("camera:direction") or t.get("direction", "")])
    return {"cols": ["lon", "lat", "osm_id", "operator", "manufacturer", "direction"],
            "rows": rows, "n": len(rows),
            "source": "OpenStreetMap (surveillance:type=ALPR), via Overpass",
            "evidence": CONTEXT,
            "scope": "viewer-only exception: locations only, never what a camera reads"}
