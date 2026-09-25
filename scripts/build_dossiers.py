"""One record per unexplained detection: what the radar saw, what AIS said.

    python scripts/build_dossiers.py
    python scripts/build_dossiers.py --no-ais      # skip the slow parquet pass

WHAT A DOSSIER IS FOR

The project's claim is that the *discrepancy* between cooperative reporting
and independent sensing is the intelligence product. A geojson row is not
that claim; it is a coordinate. A dossier is the claim made checkable: the
radar evidence, the reporting evidence, the reason the two did not reconcile,
and -- given equal room -- every reason the discrepancy might be spurious.

So each record carries four things that argue *against* it as much as the
three that argue for it:

    reception        could AIS have been heard here at all? An unexplained
                     detection in thin water is not evidence of
                     non-cooperation, it is evidence of a thin receiver.
    persistence      has something been at this exact spot on other passes?
                     A structure detected three times is furniture, not a
                     dark vessel -- see the note on the filter below.
    p_vessel         is it a vessel at all? 20% of the labelled sample is
                     clutter and 8% is fixed structure.
    nearest AIS      how close did the nearest report actually come? A track
                     200 m outside a 132 m match radius is a near miss, not
                     a silence, and the record says which.

RANKING DELIBERATELY EXCLUDES DISTANCE TO ANY LIMIT

`strength` scores how well-evidenced the discrepancy is, from p_vessel,
reception, persistence and AIS isolation. Distance to the 12 or 24 nm line is
carried on every record and appears nowhere in the score.

This is not fastidiousness. The research question is whether non-cooperative
behaviour concentrates at governance discontinuities. If the ranking put
near-line candidates on top, anyone browsing the dossier would see a
concentration the ranking had manufactured, and would have no way to tell it
from one the world had. The ordering must be innocent of the hypothesis.

A NOTE ON THE PERSISTENCE FILTER, WHICH THESE RECORDS EXPOSE

`persistent_sites.py` drops detections at fixed sites; `is_fixed` requires
`n_dates >= 3`, `hit_fraction >= 0.5`, **and `not ever_matched`**. The
147-chip read found twelve fixed structures that survived it, and every one
is already in `persistent-sites.geojson`, correctly identified as a repeat
and then not acted on:

    Cove Point LNG pier     seen 7 of 7 passes, hit_fraction 1.00, and
                            classified "traffic" because AIS explained it at
                            least once. It is an LNG terminal. A berth is
                            precisely the kind of structure that has
                            reporting vessels sitting on it, so
                            `ever_matched` disqualifies exactly the fixed
                            sites most worth catching.
    the CVOW lease point    seen 2 of 2 passes since first seen,
                            hit_fraction 1.00, rejected for n_dates < 3.

So the dossier does not print a boolean. It prints `n_dates_seen`,
`n_dates_searched`, `hit_fraction`, `ever_matched` and the site's own
verdict, and lets the reader see why the filter let it through.

WHAT THESE RECORDS DO NOT CLAIM

Every record carries its caveats inline rather than in a methods section
nobody opens: the azimuth-displacement bias, the single relative orbit, the
missing 3 nm line, and the targeting boundary -- these are observations of
platforms and structures, never of people.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS, INTERIM, REFERENCE

CHIPS = INTERIM / "candidates"
AIS_DIR = REFERENCE / "ais"

AIS_WINDOW_S = 1800.0
AIS_CELL_DEG = 0.05
# Beyond this the nearest report says nothing useful; the dossier records
# "none within" rather than a number that invites over-reading.
AIS_MAX_M = 10_000.0

# A detection whose nearest AIS report is this many match-radii away is
# treated as fully isolated. Below it the discrepancy is a near miss and the
# score says so.
ISOLATION_RADII = 20.0

# A persistent site further away than this says nothing about this detection.
# The clustering radius that built the sites is 300 m; 500 m is generous
# without being a different place.
SITE_NEAR_M = 500.0

CAVEATS = [
    "Azimuth displacement: a moving target is imaged offset along-track. "
    "Bright ships in this study sat 400-470 m from their AIS positions. A "
    "position here is not a position to 10 m.",
    "One relative orbit (S1A, 106, ascending), one look direction, one "
    "sensor. Anything that depends on viewing geometry is unmodelled.",
    "The 3 nm state seaward limit is absent from NOAA's Maritime Limits "
    "product, so distances are to the 12 and 24 nm lines only.",
    "This is an observation of a platform or structure. It is not an "
    "observation of a person, and nothing here identifies one.",
]


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    return math.hypot((lat2 - lat1) * 111_320.0,
                      (lon2 - lon1) * 111_320.0 * math.cos(math.radians(lat1)))


def key_of(date: str, lat: float, lon: float) -> str:
    """The chip naming scheme, so a dossier and its image share an id."""
    return (f"{date}_{lat:.4f}".replace(".", "p")
            + f"_{lon:.4f}".replace(".", "p"))


def load_labels(path: Path) -> dict[tuple[float, float], dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("key"):
            out[rec["key"]] = rec
    return {(round(r["lon"], 6), round(r["lat"], 6)): r
            for r in out.values() if r.get("lon") is not None}


def scene_context() -> dict[str, dict]:
    """Per-scene properties from the sar- files: the conditions of the look."""
    out: dict[str, dict] = {}
    for p in sorted(EVENTS.glob("sar-*.geojson")):
        try:
            props = json.loads(p.read_text(encoding="utf-8")).get("properties", {})
        except (OSError, ValueError):
            continue
        name = props.get("scene")
        if not name:
            continue
        out[name] = {k: props.get(k) for k in (
            "k_sigma", "gate_min_snr", "gate_min_pixels", "searched_km2",
            "clutter_verdict", "clutter_headroom", "geolocation_rms_m",
            "geolocation_method", "detection_rate", "ais_window_s",
            "k_sigma_match", "coast_blind_m", "n_detections", "n_reports")}
    return out


def dark_evidence() -> dict[tuple[float, float], dict]:
    """Per-detection evidence recorded when matching failed."""
    out: dict[tuple[float, float], dict] = {}
    for p in sorted(EVENTS.glob("dark-*.geojson")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for f in doc.get("features", []):
            lon, lat = f["geometry"]["coordinates"][:2]
            out[(round(lon, 5), round(lat, 5))] = f["properties"].get(
                "evidence", {})
    return out


def persistence_index() -> list[tuple[float, float, dict]]:
    p = EVENTS / "persistent-sites.geojson"
    if not p.exists():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    return [(f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1],
             f["properties"]) for f in doc.get("features", [])]


def nearest_ais(feats: list[dict]) -> tuple[dict[int, dict], set[str]]:
    """Nearest AIS report in space, within the matching time window.

    Streamed and grid-indexed: the naive loop over every report for every
    candidate ran past two minutes on one date alone.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    by_date: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(feats):
        by_date[f["properties"]["date"]].append(i)

    found: dict[int, dict] = {}
    missing: set[str] = set()
    for date, idxs in sorted(by_date.items()):
        path = AIS_DIR / f"ais-{date}.parquet"
        if not path.exists():
            # A date with no AIS file is a date we did not look at. Returning
            # "no report found" for it would turn a missing file into maximum
            # evidence of silence -- the exact inversion this project has now
            # tripped over three times in other guises.
            missing.add(date)
            print(f"    {date}: NO AIS FILE -- {len(idxs)} candidates cannot "
                  f"be assessed for isolation")
            continue
        times = [datetime.fromisoformat(feats[i]["properties"]["t"])
                 for i in idxs]
        lo = (min(times) - timedelta(seconds=AIS_WINDOW_S)).replace(tzinfo=None)
        hi = (max(times) + timedelta(seconds=AIS_WINDOW_S)).replace(tzinfo=None)

        grid: dict[tuple[int, int], list] = defaultdict(list)
        pf = pq.ParquetFile(path)
        for b in pf.iter_batches(batch_size=500_000, columns=[
                "mmsi", "base_date_time", "vessel_name", "sog", "geometry"]):
            col = b.column("base_date_time")
            b = b.filter(pc.and_(
                pc.greater_equal(col, pa.scalar(lo, type=col.type)),
                pc.less_equal(col, pa.scalar(hi, type=col.type))))
            if b.num_rows == 0:
                continue
            ts = b.column("base_date_time").to_pylist()
            gs = b.column("geometry").to_pylist()
            mm = b.column("mmsi").to_pylist()
            nm = b.column("vessel_name").to_pylist()
            sg = b.column("sog").to_pylist()
            for j, w in enumerate(gs):
                x, y = struct.unpack_from("<dd", w, 5)
                tt = ts[j]
                if tt.tzinfo is None:
                    tt = tt.replace(tzinfo=timezone.utc)
                grid[(int(x / AIS_CELL_DEG), int(y / AIS_CELL_DEG))].append(
                    (x, y, tt, mm[j], nm[j], sg[j]))

        reach = int(AIS_MAX_M / 111_320.0 / AIS_CELL_DEG) + 1
        for i, st in zip(idxs, times, strict=True):
            lon, lat = feats[i]["geometry"]["coordinates"][:2]
            cx, cy = int(lon / AIS_CELL_DEG), int(lat / AIS_CELL_DEG)
            best_d, best = None, None
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    for (x, y, tt, mmsi, name, sog) in grid.get(
                            (cx + dx, cy + dy), ()):
                        dt = abs((tt - st).total_seconds())
                        if dt > AIS_WINDOW_S:
                            continue
                        d = haversine_m(lon, lat, x, y)
                        if d > AIS_MAX_M:
                            continue
                        if best_d is None or d < best_d:
                            best_d, best = d, {
                                "distance_m": round(d, 1),
                                "dt_s": int(dt),
                                "mmsi": mmsi,
                                "name": (name or "").strip() or None,
                                "sog_kn": round(sog, 1) if sog is not None
                                else None,
                            }
            if best is not None:
                found[i] = best
        print(f"    {date}: {len(idxs):>4} candidates, "
              f"{sum(1 for i in idxs if i in found):>4} with a report within "
              f"{AIS_MAX_M / 1000:.0f} km")
    return found, missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidates", type=Path,
                    default=EVENTS / "candidates-scored.geojson")
    ap.add_argument("--labels", type=Path, default=EVENTS / "labels.jsonl")
    ap.add_argument("--pvessel", type=Path,
                    default=EVENTS / "candidate-pvessel.json")
    ap.add_argument("--out", type=Path, default=EVENTS / "dossiers.json")
    ap.add_argument("--reception", default="",
                    help="restrict to these reception classes. Default: "
                         "ALL of them. Thin-reception candidates belong in "
                         "the dossier scored at zero with the reason stated; "
                         "omitting them hides the control.")
    ap.add_argument("--no-ais", action="store_true",
                    help="skip the AIS pass (about 35 s over twelve dates)")
    args = ap.parse_args()

    from scripts.boundary_analysis import (ANY_NAME, CZ_NAME, NM_M, TS_NAME,
                                           limit_sets)
    from scripts.sample_candidates import gate_from_bands, stratum

    doc = json.loads(args.candidates.read_text(encoding="utf-8"))
    keep = {s.strip() for s in args.reception.split(",") if s.strip()}
    feats = [f for f in doc.get("features", [])
             if not keep or f["properties"].get("reception", "heard") in keep]
    print(f"\n  {len(feats):,} candidates ({', '.join(sorted(keep))})")

    # The per-scene files carry gate_min_snr / gate_min_pixels as nulls on
    # all 37 scenes -- the gate is applied downstream, not at detection, so
    # it was never written there. The authoritative value is on the candidate
    # file. Showing a dash for something the project knows is worse than
    # showing it with its provenance attached.
    doc_props = doc.get("properties", {})
    fallback_gate = {"gate_min_snr": doc_props.get("gate_min_snr"),
                     "gate_min_pixels": doc_props.get("gate_min_pixels"),
                     "gate_source": args.candidates.name}

    labels = load_labels(args.labels)
    scenes = scene_context()
    evidence = dark_evidence()
    sites = persistence_index()
    gate, _ = gate_from_bands()
    print(f"  {len(labels)} labels, {len(scenes)} scenes, "
          f"{len(evidence):,} dark-evidence records, {len(sites)} known sites")

    pv: dict[tuple[float, float], float] = {}
    if args.pvessel.exists():
        for p in json.loads(args.pvessel.read_text(encoding="utf-8"))["points"]:
            pv[(round(p["lon"], 6), round(p["lat"], 6))] = p["p_vessel"]

    sets = limit_sets()
    any_lines = sets.get(ANY_NAME) or next(iter(sets.values()))
    ts_lines, cz_lines = sets.get(TS_NAME), sets.get(CZ_NAME)

    ais: dict[int, dict] = {}
    no_ais_dates: set[str] = set()
    if not args.no_ais:
        print("\n  nearest AIS report per candidate:")
        ais, no_ais_dates = nearest_ais(feats)
        if no_ais_dates:
            print(f"    {len(no_ais_dates)} date(s) without an AIS file: "
                  f"{', '.join(sorted(no_ais_dates))}")

    records = []
    for i, f in enumerate(feats):
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        rk = (round(lon, 6), round(lat, 6))
        date = p.get("date", "?")

        d_any = any_lines.distance_m(lon, lat)
        band, strength_cls, _rc = stratum(
            p, None if d_any is None else d_any / NM_M, gate)

        # Only a site NEAR the candidate is evidence about it. The first
        # version attached the nearest site whatever its distance, and the
        # page then showed "seen on 2 of 12 passes" beside a site 9.7 km
        # away -- a true statement about somewhere else, rendered as though
        # it were about this detection. The scoring already guarded on
        # distance; the display did not, which is the worse half to miss.
        site = None
        if sites:
            sx, sy, sp = min(sites,
                             key=lambda s: haversine_m(lon, lat, s[0], s[1]))
            d_site = haversine_m(lon, lat, sx, sy)
            if d_site <= SITE_NEAR_M:
                site = {"distance_m": round(d_site, 1),
                        **{k: sp.get(k) for k in (
                            "n_dates_seen", "n_dates_searched", "hit_fraction",
                            "ever_matched", "verdict", "first_seen")}}
            else:
                site = {"distance_m": round(d_site, 1), "near": False,
                        "note": f"no persistent site within {SITE_NEAR_M:.0f} m"}

        ev = evidence.get((round(lon, 5), round(lat, 5)), {})
        radius = ev.get("search_radius_m")
        near = ais.get(i)
        looked = not args.no_ais and date not in no_ais_dates
        isolation = None
        if radius and near:
            isolation = min(1.0, (near["distance_m"] / radius) / ISOLATION_RADII)
        elif radius and not near and looked:
            isolation = 1.0

        p_vessel = pv.get(rk)
        lab = labels.get(rk)
        recep = p.get("reception", "heard")
        # Reception is a gate on belief, not a weight: only `heard` water can
        # support a non-cooperation reading at all.
        recep_ok = 1.0 if recep == "heard" else 0.0
        # A site seen on other passes is furniture until shown otherwise, and
        # the penalty scales with how often it has been seen.
        rep = 0.0
        if site and site.get("n_dates_seen"):
            rep = min(1.0, (site["n_dates_seen"] - 1) / 4.0)

        base = p_vessel if p_vessel is not None else 0.5
        score = base * recep_ok * (1.0 - rep) * (isolation
                                                 if isolation is not None
                                                 else 0.5)

        records.append({
            "key": key_of(date, lat, lon),
            "lon": lon, "lat": lat, "date": date, "t": p.get("t"),
            "scene": p.get("scene"),
            "radar": {k: p.get(k) for k in (
                "snr", "pixels", "length_m_approx", "uncertainty_m",
                "peak_dn", "background_dn", "sigma_dn", "confidence",
                "sensor")},
            "look": _look(scenes.get(p.get("scene"), {}), fallback_gate),
            "matching": {"search_radius_m": radius,
                         "reports_considered": ev.get("reports_considered"),
                         "reports_with_a_fix": ev.get("reports_with_a_fix"),
                         "detection_rate_this_pass":
                             ev.get("detection_rate_this_pass")},
            "ais": {"nearest": near,
                    "cell_vessels": p.get("ais_cell_vessels"),
                    "searched_window_s": AIS_WINDOW_S,
                    "looked": looked,
                    "note": (None if near is not None else
                             "AIS pass skipped" if args.no_ais else
                             f"NO AIS DATA for {date} -- absence here is "
                             f"missing evidence, not evidence of absence"
                             if not looked else
                             f"no report within {AIS_MAX_M / 1000:.0f} km "
                             f"of the look")},
            "reception": {"class": recep},
            "persistence": site,
            "geography": {
                "nm_to_any_limit": None if d_any is None
                else round(d_any / NM_M, 2),
                "nm_to_12nm": None if not ts_lines
                else round((ts_lines.distance_m(lon, lat) or 0) / NM_M, 2),
                "nm_to_24nm": None if not cz_lines
                else round((cz_lines.distance_m(lon, lat) or 0) / NM_M, 2),
                "band": band, "detector_strength": strength_cls},
            "assessment": {
                "p_vessel": p_vessel,
                "label": None if not lab else {
                    "verdict": lab.get("verdict"), "reader": lab.get("reader"),
                    "note": lab.get("note"), "at": lab.get("at")},
                "ais_isolation": None if isolation is None
                else round(isolation, 3),
                "strength": round(score, 4),
                "why": _why(p_vessel, recep, rep, isolation, near, radius,
                        looked),
            },
            "chip": None,
            "caveats": CAVEATS,
        })

    # Chips exist only for the sampled candidates; say so rather than 404.
    have = {p.stem: f"{p.parent.name}/{p.name}"
            for p in CHIPS.glob("*/*.png")}
    n_chip = 0
    for r in records:
        hit = have.get(r["key"])
        if hit:
            r["chip"] = hit
            n_chip += 1

    records.sort(key=lambda r: -r["assessment"]["strength"])

    # The ranking uses no distance term, and the top of it is still not
    # band-neutral -- because p_vessel is higher near the lines, where the
    # detections are genuinely cleaner (0.84 vessel rate within 2 nm against
    # 0.60 beyond 10 nm). A reader browsing the top of this list would see a
    # near-line concentration that is a property of data quality, not of
    # behaviour. Measure it here and put it on the record, rather than
    # letting the page imply a finding the study did not make.
    bias = _ranking_bias(records)
    args.out.write_text(json.dumps({
        "what": "one record per unexplained detection; the discrepancy and "
                "everything that argues against it",
        "ranking": "strength = p_vessel x reception x (1 - persistence) x "
                   "ais_isolation. Distance to any limit is NOT a term.",
        "n": len(records),
        "ranking_bias": bias,
        "with_chip": n_chip,
        "with_label": sum(1 for r in records if r["assessment"]["label"]),
        "with_ais": sum(1 for r in records if r["ais"]["nearest"]),
        "caveats": CAVEATS,
        "records": records,
    }, indent=1), encoding="utf-8")

    print(f"\n  wrote {len(records):,} dossiers -> {args.out.name}")
    print(f"    {n_chip} with a chip, "
          f"{sum(1 for r in records if r['assessment']['label'])} with a label, "
          f"{sum(1 for r in records if r['ais']['nearest'])} with a nearby AIS "
          f"report")
    top = records[0]["assessment"]["strength"]
    med = records[len(records) // 2]["assessment"]["strength"]
    print(f"    strength: top {top:.3f}, median {med:.3f}, "
          f"bottom {records[-1]['assessment']['strength']:.3f}\n")
    return 0


def _look(scene: dict, fallback: dict) -> dict:
    out = dict(scene)
    for k in ("gate_min_snr", "gate_min_pixels"):
        if out.get(k) is None and fallback.get(k) is not None:
            out[k] = fallback[k]
            out["gate_source"] = fallback["gate_source"]
    return out


def _ranking_bias(records: list[dict], top: int = 100) -> dict:
    """How far from band-neutral the top of the ranking is, and why."""
    import statistics
    head, tail = records[:top], records[top:]

    def mix(rs):
        n = len(rs) or 1
        return {b: round(sum(1 for r in rs
                             if r["geography"]["band"] == b) / n, 3)
                for b in ("<=2nm", "2-10nm", ">10nm")}

    xs = [r["assessment"]["strength"] for r in records
          if r["geography"]["nm_to_any_limit"] is not None]
    ys = [r["geography"]["nm_to_any_limit"] for r in records
          if r["geography"]["nm_to_any_limit"] is not None]
    r_val = None
    if len(xs) > 2:
        mx, my = statistics.mean(xs), statistics.mean(ys)
        cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
        den = math.sqrt(sum((a - mx) ** 2 for a in xs)
                        * sum((b - my) ** 2 for b in ys))
        r_val = round(cov / den, 3) if den else None
    return {
        "top_n": top,
        "band_mix_top": mix(head),
        "band_mix_rest": mix(tail),
        "pearson_strength_vs_nm": r_val,
        "warning": (
            "The ranking contains no distance term, and is still not "
            "band-neutral: near-line detections are cleaner (0.84 vessel "
            "rate within 2 nm against 0.60 beyond 10 nm), so p_vessel lifts "
            "them. The over-representation at the top of this list is a "
            "property of DATA QUALITY, not of behaviour, and it is not the "
            "study's finding -- which is that unexplained returns are "
            "DEPLETED near maritime limits. Do not read the top of a "
            "dossier list as a spatial result."),
    }


def _why(p_vessel, recep, rep, isolation, near, radius, looked) -> list[str]:
    """The score's reasons, in words, because a number alone is not evidence."""
    out = []
    if p_vessel is None:
        out.append("no P(vessel): not scored by the classifier")
    elif p_vessel >= 0.8:
        out.append(f"looks like a vessel (P={p_vessel:.2f})")
    elif p_vessel <= 0.4:
        out.append(f"may well be clutter (P={p_vessel:.2f})")
    else:
        out.append(f"uncertain that it is a vessel (P={p_vessel:.2f})")
    if recep != "heard":
        out.append(f"reception is '{recep}': silence here is not evidence of "
                   f"silence")
    if rep > 0:
        out.append("something has been detected at this spot on other passes")
    if near and radius:
        out.append(f"nearest AIS report {near['distance_m']:.0f} m away, "
                   f"{near['dt_s']} s off, against a {radius:.0f} m match "
                   f"radius")
    elif near:
        out.append(f"nearest AIS report {near['distance_m']:.0f} m away")
    elif not looked:
        out.append("AIS was not searched for this date -- isolation unknown, "
                   "and scored as unknown rather than as silence")
    elif isolation == 1.0:
        out.append("no AIS report anywhere near it, in space or time")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
