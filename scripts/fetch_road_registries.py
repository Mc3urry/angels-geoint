"""Which road feeds this project will poll, decided in code and recorded.

    python scripts/fetch_road_registries.py
    python scripts/fetch_road_registries.py --dry-run     # parse, select, print
    python scripts/fetch_road_registries.py --from-fixture tests/fixtures/road

Writes two tracked artefacts into data/reference/roads/:

    wzdx-registry.json      the declared layer, G1
    transit-registry.json   the independent layer, G2

WHY THE SELECTION LIVES IN THIS FILE

Mechanism O in `docs/reporting-defects.md`: a pooled quantity whose
membership is a directory listing. `limit_sets` once decided what counted as
"any limit" by reading a folder, and 483 NOAA polygons walked in and became a
legal boundary line, raising the headline ratio fifteen-fold in the direction
of the hypothesis. The pool that a result is computed over has to be defined
by a rule that is written down, not by what happened to be convenient.

So the rules below are constants in this module and are copied into the
artefact. Changing which feeds are polled means changing this file, which
means a commit, which means a diff somebody can read.

WHAT THIS REFUSES, AND WHY EACH REFUSAL EXISTS

    an error body with HTTP 200     ArcGIS and Socrata both answer a bad
                                    query that way. `describe_sla` was
                                    written for exactly this and was defeated
                                    the next day -- mechanism N.
    HTML where CSV was expected     Three chart.maryland.gov endpoints answer
                                    200 with a web page. A registry that
                                    parsed a login page into zero rows would
                                    report an outage as an empty world.
    a truncated read                A capped read of this same Mobility CSV
                                    once reported 4,930 rows where the true
                                    figure was 6,479. A registry is a
                                    denominator; a short one is worse than
                                    none.
    an unknown boolean word         `needAPIKey` and `active` are STRINGS.
                                    `bool("false")` is True. A value outside
                                    the known vocabulary is refused rather
                                    than guessed.
    an unknown spec version         The version column holds '', '3.1', '4',
                                    '4.1', '4.2' and 'CWZ 1.0'. Two of those
                                    six are not numbers, so nothing compares
                                    it numerically, and a version this
                                    project has not read the spec for is
                                    excluded by name.

EVERY EXCLUSION IS NAMED

Mechanism G: a report that names its exclusions from a list written in
advance will omit a category added later. So nothing here is dropped. A feed
that is not selected appears in `excluded`, with the rule that excluded it,
and the counts are asserted to add up -- selected plus excluded equals rows
read. A test checks that sum, because a feed vanishing silently between the
two is how a denominator goes wrong in a direction nobody can see.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import REFERENCE

# Imported rather than re-implemented: one list of credential hints and one
# set of body patterns, in one place, used by the probe and by this. Two
# copies drift, and a credential check that has drifted is worse than none,
# because it still reads as covered.
from scripts.probe_road_feeds import body_credential, looks_credentialed

# -- the sources ------------------------------------------------------------

WZDX_URL = ("https://data.transportation.gov/api/views/69qe-yiui/rows.csv"
            "?accessType=DOWNLOAD")
TRANSIT_URL = "https://files.mobilitydatabase.org/feeds_v2.csv"

# Read in full. The Mobility CSV was 2,639,920 bytes on 2026-10-05 and a cap
# below it produced a lower bound that was short by 1,549 rows. 32 MB is
# comfortably above any plausible growth and still refuses a runaway.
MAX_BYTES = 32 * 1024 * 1024
TIMEOUT_S = 60.0
UA = "ANGELS-capstone/1 (+github.com/Mc3urry) python-urllib"

# -- the vocabularies, which are the selection rules ------------------------

# `needAPIKey` and `active` arrive as text. Spelled out because bool() of a
# non-empty string is True, so "false" would select every keyed feed.
TRUE_WORDS = ("true", "t", "yes", "y", "1")
FALSE_WORDS = ("false", "f", "no", "n", "0")

# WZDx versions this project has read the specification for. 'CWZ 1.0' is a
# different specification entirely -- Connected Work Zones -- and is excluded
# by name rather than by failing a float() call.
KNOWN_WZDX_VERSIONS = ("4", "4.1", "4.2")
EXCLUDED_WZDX_VERSIONS = {
    "": "the registry row states no version",
    "3.1": "WZDx 3.1 names the feed metadata object road_event_feed_info "
           "rather than feed_info; not read for this project",
    "CWZ 1.0": "Connected Work Zones is a different specification",
}

# GTFS-Realtime feeds worth polling for the independent layer: vehicle
# positions, in the United States, needing no credential.
TRANSIT_DATA_TYPE = "gtfs_rt"
TRANSIT_ENTITY = "vp"              # vehicle positions
TRANSIT_COUNTRY = "US"
# authentication_type 0 means none required, per the Mobility Database
# schema. Blank is treated as 0 because the published CSV leaves it blank for
# feeds that need nothing, and that reading is asserted in the tests against
# a recorded body.
NO_AUTH_VALUES = ("", "0")
# A feed the catalogue has marked as gone is excluded by name, not by hoping
# the request fails.
BAD_STATUS = ("deprecated", "inactive", "development")


class RegistryRefused(RuntimeError):
    """The body is not a registry. Raised rather than parsed into zero rows."""


def safe_url(url: str) -> str:
    """A url fit to write into a tracked artefact.

    THIS FUNCTION EXISTS BECAUSE THE DRAFT OF THIS FILE DID NOT HAVE IT

    Written on 2026-10-05, hours before GitHub secret scanning found a
    third party's Google key in a committed fixture -- mechanism X in
    `docs/reporting-defects.md`, a guard pointed at the surface its author
    was thinking about.

    The draft recorded every excluded feed with its url, so that a gap in
    coverage would be visible rather than silent. That was the right
    instinct. But the Mobility Database catalogue publishes feed urls with
    tokens embedded in them -- the probe's own manifest records one, 1.8 MB
    into that CSV -- and the feeds this selection EXCLUDES for needing a
    credential are exactly the ones whose urls are most likely to carry one.

    So the artefact would have published other people's tokens, in a file
    written to be committed, as a direct consequence of a rule about
    honesty. The same mistake, one working day later, in code written by
    somebody who had just spent the afternoon writing it up.

    A url that looks credentialed keeps its scheme, host and path and loses
    its query, and the loss is stated at the call site rather than implied.
    """
    if not looks_credentialed(url):
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) \
        + "  [query redacted: it matched a credential pattern]"


# -- parsing ----------------------------------------------------------------

def parse_registry_csv(raw: bytes, *, truncated: bool = False) -> list[dict]:
    """Rows of a registry CSV, or a refusal that says what arrived instead.

    Pure: no network, no clock, no filesystem.
    """
    if truncated:
        raise RegistryRefused(
            "the read was truncated, so the row count would be a lower bound "
            "presented as a total; a registry is a denominator")
    head = raw.lstrip()[:64].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise RegistryRefused(
            "the endpoint answered with an HTML page, not a registry. Three "
            "chart.maryland.gov endpoints do this with HTTP 200")
    if head[:1] in (b"{", b"["):
        raise RegistryRefused(
            "the endpoint answered with JSON where CSV was expected, which "
            "is what an ArcGIS or Socrata error body looks like")
    if head[:1] == b"<":
        raise RegistryRefused(
            "the endpoint answered with XML or HTML, not a registry")
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise RegistryRefused(
            "the body parsed as CSV and held no data rows. An exact zero is "
            "instrumented before it is believed, and an empty registry is "
            "not a fact about the world")
    if None in rows[0]:
        raise RegistryRefused(
            "a row has more fields than the header, so the quoting is not "
            "what this parser assumes")
    return rows


def word_to_bool(value: str, field: str) -> bool:
    """A text boolean, or a refusal. Never truthiness of the string.

    `bool("false")` is True, and `needAPIKey` is text. This is the whole of
    the reason this function exists instead of an `if row[field]`.
    """
    v = (value or "").strip().lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    raise RegistryRefused(
        f"{field} holds {value!r}, which is in neither vocabulary. Guessing "
        f"here is how a keyed feed gets polled without a key")


# -- selection --------------------------------------------------------------

def wzdx_selection(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(selected, excluded). Every row appears in exactly one of them."""
    selected: list[dict] = []
    excluded: list[dict] = []
    for r in rows:
        name = (r.get("feedName") or "").strip()
        org = (r.get("issuingOrganization") or "").strip()
        state = (r.get("state") or "").strip()
        version = (r.get("version") or "").strip()
        raw_url = (r.get("url") or "").strip()
        ident = {"feedName": name, "issuingOrganization": org,
                 "state": state, "version": version,
                 "url": safe_url(raw_url)}

        if not word_to_bool(r.get("active", ""), "active"):
            excluded.append(dict(ident, rule="inactive",
                                 why="the registry marks this feed inactive"))
            continue
        if word_to_bool(r.get("needAPIKey", ""), "needAPIKey"):
            excluded.append(dict(
                ident, rule="needs a key",
                why="a credential is required and this project holds none; "
                    "named here rather than dropped, so the gap in coverage "
                    "is visible",
                apiKeyURL=(r.get("apiKeyURL") or "").strip()))
            continue
        if version not in KNOWN_WZDX_VERSIONS:
            excluded.append(dict(
                ident, rule="unknown spec version",
                why=EXCLUDED_WZDX_VERSIONS.get(
                    version, f"version {version!r} has not been read for "
                             f"this project")))
            continue
        if not raw_url:
            excluded.append(dict(ident, rule="no url",
                                 why="the registry row carries no feed url"))
            continue
        if looks_credentialed(raw_url):
            # Two records of one fact disagreeing: the registry says no key
            # is needed and the url carries one. Rule 20's shape. Excluded
            # rather than reconciled, because whichever is wrong, polling it
            # would mean sending somebody else's credential.
            excluded.append(dict(
                ident, rule="url and registry disagree",
                why="needAPIKey is false and yet the url matches a "
                    "credential pattern; the query is redacted above and "
                    "the feed is not polled"))
            continue
        selected.append(ident)
    return selected, excluded


def transit_selection(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(selected, excluded) for GTFS-Realtime vehicle positions.

    The first matching rule wins and is recorded, so a row excluded for three
    reasons is reported under one and the counts still add up.
    """
    selected: list[dict] = []
    excluded: list[dict] = []
    for r in rows:
        raw_url = (r.get("urls.direct_download") or "").strip()
        ident = {
            "id": (r.get("id") or "").strip(),
            "provider": (r.get("provider") or "").strip(),
            "subdivision": (r.get("location.subdivision_name") or "").strip(),
            "municipality": (r.get("location.municipality") or "").strip(),
            "url": safe_url(raw_url),
        }
        data_type = (r.get("data_type") or "").strip().lower()
        entity = (r.get("entity_type") or "").strip().lower()
        country = (r.get("location.country_code") or "").strip().upper()
        auth = (r.get("urls.authentication_type") or "").strip()
        status = (r.get("status") or "").strip().lower()

        if data_type != TRANSIT_DATA_TYPE:
            excluded.append(dict(ident, rule="not realtime",
                                 why=f"data_type is {data_type!r}, not "
                                     f"{TRANSIT_DATA_TYPE!r}"))
            continue
        # entity_type is a list in one cell: 'vp', 'tu', 'sa', or several.
        if TRANSIT_ENTITY not in [e.strip() for e in entity.replace(
                ",", " ").split()]:
            excluded.append(dict(ident, rule="no vehicle positions",
                                 why=f"entity_type is {entity!r} and does "
                                     f"not include {TRANSIT_ENTITY!r}; trip "
                                     f"updates and alerts are not positions"))
            continue
        if country != TRANSIT_COUNTRY:
            excluded.append(dict(ident, rule="outside the study area",
                                 why=f"country_code is {country!r}"))
            continue
        if auth not in NO_AUTH_VALUES:
            excluded.append(dict(
                ident, rule="needs a key",
                why=f"authentication_type is {auth!r}; named rather than "
                    f"dropped so the gap is visible",
                authentication_info=(r.get("urls.authentication_info")
                                     or "").strip()))
            continue
        if status in BAD_STATUS:
            excluded.append(dict(ident, rule="not current",
                                 why=f"the catalogue marks this feed "
                                     f"{status!r}"))
            continue
        if not raw_url:
            excluded.append(dict(ident, rule="no url",
                                 why="no direct_download url"))
            continue
        if looks_credentialed(raw_url):
            excluded.append(dict(
                ident, rule="url and catalogue disagree",
                why="authentication_type says none is needed and yet the "
                    "url matches a credential pattern. The Mobility "
                    "catalogue does publish feed urls with tokens in them, "
                    "so this is expected to fire; the query is redacted "
                    "above and the feed is not polled"))
            continue
        selected.append(ident)
    return selected, excluded


def artefact(kind: str, url: str, raw: bytes, rows: list[dict],
             selected: list[dict], excluded: list[dict],
             *, when: datetime, rules: dict) -> dict:
    """The record, with the inputs that define it in the same file.

    Rule 12. A stored measurement records what it is a measurement of: the
    endpoint, the fetch time, the digest of the bytes it was computed from,
    the rule set, and the arithmetic that has to hold.
    """
    by_rule: dict[str, int] = {}
    for e in excluded:
        by_rule[e["rule"]] = by_rule.get(e["rule"], 0) + 1
    return {
        "_what": f"{kind}: which feeds this project polls, and which it does "
                 f"not, with the reason for every exclusion",
        "_endpoint": url,
        "_fetched_at_utc": when.isoformat(timespec="seconds"),
        "_bytes": len(raw),
        "_sha256": hashlib.sha256(raw).hexdigest(),
        "_selection_rules": rules,
        "_note": "The rules above are constants in "
                 "scripts/fetch_road_registries.py. Changing which feeds are "
                 "polled is a commit, not a configuration change -- see "
                 "mechanism O in docs/reporting-defects.md.",
        "rows_read": len(rows),
        "n_selected": len(selected),
        "n_excluded": len(excluded),
        "excluded_by_rule": by_rule,
        "selected": selected,
        "excluded": excluded,
    }


def check_arithmetic(art: dict) -> None:
    """Selected plus excluded equals rows read, or the artefact is wrong.

    A feed that falls between the two lists leaves a denominator short in a
    direction nobody can see, which is the shape of most of this project's
    worst afternoons.
    """
    total = art["n_selected"] + art["n_excluded"]
    if total != art["rows_read"]:
        raise RegistryRefused(
            f"{art['n_selected']} selected plus {art['n_excluded']} excluded "
            f"is {total}, against {art['rows_read']} rows read. A row is "
            f"being lost between the two lists")
    if sum(art["excluded_by_rule"].values()) != art["n_excluded"]:
        raise RegistryRefused(
            "the per-rule exclusion counts do not sum to the number excluded")


def check_no_credentials(art: dict) -> None:
    """Nothing credential-shaped reaches a file written to be committed.

    `safe_url` redacts the urls, which is the rule. This reads the finished
    artefact and fails on CONTENT, which is the backstop, and the two are
    not the same kind of check: the first trusts a pattern list applied at
    one call site, the second trusts nothing and looks at what is about to
    be written.

    Mechanism X cost a credential because one guard existed and passed. A
    guard and a backstop that disagree is a finding; a guard alone is a
    feeling.
    """
    hit = body_credential(json.dumps(art).encode("utf-8"))
    if hit:
        raise RegistryRefused(
            f"the artefact contains a {hit} and will not be written. A "
            f"redaction rule has been outflanked somewhere upstream")


# -- fetching, which is the only part that touches the network --------------

def fetch(url: str, *, cap: int = MAX_BYTES,
          timeout: float = TIMEOUT_S) -> tuple[bytes, bool]:
    """(body, truncated). The caller decides what a truncated body means."""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/csv, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(cap + 1)
    return body[:cap], len(body) > cap


def from_fixture(fix_dir: Path, name: str) -> tuple[bytes, bool]:
    """A recorded body and whether the recording was clipped.

    The clip comes from the manifest rather than from the file's length,
    because a clipped file's length tells you the clip and not the source.
    A clipped registry is refused downstream, which is the correct answer and
    the reason `--from-fixture` cannot stand in for a fetch of the transit
    catalogue.
    """
    man = json.loads((fix_dir / "manifest.json").read_text(encoding="utf-8"))
    for b in man["bodies"]:
        if b["name"] == name:
            return (fix_dir / b["file"]).read_bytes(), bool(b.get("clipped"))
    raise RegistryRefused(f"no recorded body called {name!r} in {fix_dir}")


WZDX_RULES = {
    "active": f"one of {list(TRUE_WORDS)}, read as text and refused if "
              f"outside the vocabulary",
    "needAPIKey": f"must be one of {list(FALSE_WORDS)}",
    "version": f"must be one of {list(KNOWN_WZDX_VERSIONS)}",
    "url": "must be present",
}
TRANSIT_RULES = {
    "data_type": TRANSIT_DATA_TYPE,
    "entity_type": f"must include {TRANSIT_ENTITY!r}",
    "country_code": TRANSIT_COUNTRY,
    "authentication_type": f"must be one of {list(NO_AUTH_VALUES)}",
    "status": f"must not be one of {list(BAD_STATUS)}",
    "url": "urls.direct_download must be present",
}

REGISTRIES = (
    {"kind": "WZDx work zone feeds, the declared layer (G1)",
     "fixture": "wzdx-registry", "url": WZDX_URL,
     "out": "wzdx-registry.json", "select": wzdx_selection,
     "rules": WZDX_RULES},
    {"kind": "GTFS-Realtime vehicle positions, the independent layer (G2)",
     "fixture": "mobility-registry", "url": TRANSIT_URL,
     "out": "transit-registry.json", "select": transit_selection,
     "rules": TRANSIT_RULES},
)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=REFERENCE / "roads")
    ap.add_argument("--from-fixture", type=Path, default=None, metavar="DIR",
                    help="parse recorded bodies instead of fetching. The "
                         "transit catalogue's recording is clipped and will "
                         "be refused, which is the correct answer")
    ap.add_argument("--dry-run", action="store_true",
                    help="select and print; write nothing")
    args = ap.parse_args(argv)

    when = datetime.now(timezone.utc)
    failures = 0
    for reg in REGISTRIES:
        print(f"\n  {reg['kind']}")
        try:
            if args.from_fixture:
                raw, trunc = from_fixture(args.from_fixture, reg["fixture"])
                print(f"    from recording: {len(raw):,} bytes"
                      f"{'  (CLIPPED)' if trunc else ''}")
            else:
                raw, trunc = fetch(reg["url"])
                print(f"    fetched {len(raw):,} bytes"
                      f"{'  (TRUNCATED)' if trunc else ''}")
            rows = parse_registry_csv(raw, truncated=trunc)
            selected, excluded = reg["select"](rows)
            art = artefact(reg["kind"], reg["url"], raw, rows, selected,
                           excluded, when=when, rules=reg["rules"])
            check_arithmetic(art)
            check_no_credentials(art)
        except (RegistryRefused, urllib.error.URLError, OSError) as exc:
            failures += 1
            print(f"    REFUSED: {exc}")
            print(f"    No artefact written. A refusal that quantifies the "
                  f"miss is the point; a silent fallback is not.")
            continue

        print(f"    {art['rows_read']} rows, {art['n_selected']} selected, "
              f"{art['n_excluded']} excluded")
        for rule, n in sorted(art["excluded_by_rule"].items(),
                              key=lambda kv: -kv[1]):
            print(f"      {n:>4}  {rule}")
        if args.dry_run:
            print("    --dry-run: nothing written")
            continue
        out = args.out_dir / reg["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(art, indent=1) + "\n", encoding="utf-8")
        print(f"    wrote {out}")

    if failures:
        print(f"\n  {failures} of {len(REGISTRIES)} registries refused.\n")
        return 1
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
