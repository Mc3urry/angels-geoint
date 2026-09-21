"""Tests for the live AIS stream adapter.

Nothing here opens a socket. Everything interesting in this adapter is the
merge of two message types arriving at different rates and the honesty of the
snapshot that comes out; a test that needed a live WebSocket to exercise a
dictionary merge is a test nobody runs.
"""

from __future__ import annotations

import time

import pytest

from angels.adapters.maritime.aisstream import (
    DROP_S,
    SETTLED_FRACTION,
    STALE_S,
    WARMUP_S,
    MissingCredentials,
    Stream,
    subtype,
)

BOX = (-77.2, 36.0, -71.0, 39.6)


def stream() -> Stream:
    """A stream that has run long enough AND stopped finding new vessels.

    Both conditions are needed now. The warm-up used to be a stopwatch;
    measuring the real box showed the stopwatch was wrong by a factor of
    several, so the decision is made by the discovery rate instead. A peak is
    seeded here with no recent arrivals, which is what "settled" means.
    """
    s = Stream(BOX, api_key="test-key")
    s._started_at = time.time() - 600.0      # past the floor, no socket
    s._connected = True
    s._peak_rate = 120.0                     # it was busy once
    s._discovered = []                       # and is not any more
    return s


def position(mmsi="366999000", lat=37.0, lon=-76.0, sog=12.5, cog=91.0,
             kind="PositionReport", **extra):
    body = {"Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": cog}
    body.update(extra)
    return {"MessageType": kind,
            "MetaData": {"MMSI": mmsi},
            "Message": {kind: body}}


def static(mmsi="366999000", name="EVER GIVEN", ship_type=70, a=200, b=200,
           c=20, d=20):
    return {"MessageType": "ShipStaticData",
            "MetaData": {"MMSI": mmsi},
            "Message": {"ShipStaticData": {
                "Name": name, "Type": ship_type,
                "Dimension": {"A": a, "B": b, "C": c, "D": d},
                "MaximumStaticDraught": 14.5, "Destination": "USNFK"}}}


# -- subtype classification ------------------------------------------------

@pytest.mark.parametrize("code,expect", [
    (70, "cargo"), (79, "cargo"), (80, "tanker"), (89, "tanker"),
    (60, "passenger"), (30, "fishing"), (52, "tug"), (31, "tug"),
    (36, "pleasure"), (37, "pleasure"), (40, "highspeed"), (50, "service"),
    (35, "government"), (55, "government"), (90, "other"), (99, "other"),
])
def test_ship_types_map_to_filter_categories(code, expect) -> None:
    assert subtype(code) == expect


def test_military_and_law_enforcement_share_one_visible_category() -> None:
    """Grouped deliberately. Both are state platforms broadcasting
    irregularly by design, and a meaningful share of any dark-vessel count off
    Norfolk will be the Navy going about normal business. A visible category
    is an acknowledged confounder; no category is an invisible one."""
    assert subtype(35) == subtype(55) == "government"


def test_undeclared_and_other_are_separate_categories() -> None:
    """THE FIRST VERSION MERGED THEM, AND THE REAL BOX SHOWED WHY THAT WAS
    WRONG. A ten-minute run put 419 of 511 vessels in 'other' -- one chip
    dwarfing every real category, presented as though four hundred vessels had
    declared an unusual type. Almost all had declared nothing."""
    s = stream()
    s.ingest_message(position())
    v = s._vessels["366999000"]
    assert v.subtype == "unknown" and v.ship_type is None
    s.ingest_message(static(ship_type=99))
    assert v.subtype == "other" and v.ship_type == 99


def test_type_zero_is_not_available_not_a_type() -> None:
    """ITU-R M.1371 defines ship type 0 as "not available or no ship". It is
    the factory default on a Class B set nobody configured, which is to say
    the commonest way of not answering."""
    assert subtype(0) == "unknown"
    s = stream()
    s.ingest_message(position())
    s.ingest_message(static(ship_type=0))
    v = s._vessels["366999000"]
    assert v.typed is False
    assert v.heard_static is True, "it did send something -- just not a type"


# -- the merge -------------------------------------------------------------

def test_position_and_identity_arrive_separately_and_merge() -> None:
    """A position report comes every few seconds, static data every six
    minutes. A vessel is therefore known by position long before it is known
    by name, and the name must not be waited for."""
    s = stream()
    s.ingest_message(position())
    v = s._vessels["366999000"]
    assert v.lat == 37.0 and v.name is None

    s.ingest_message(static())
    assert v.name == "EVER GIVEN"
    assert v.length_m == 400.0 and v.width_m == 40.0
    assert v.subtype == "cargo"
    assert v.lat == 37.0, "static data must not disturb the position"


def test_an_unnamed_vessel_renders_as_its_mmsi_not_as_unknown() -> None:
    """A vessel heard once but not yet identified is a real vessel. Labelling
    it 'Unknown' invites reading it as suspicious, which is exactly the
    inference this project exists to discipline."""
    s = stream()
    s.ingest_message(position())
    feat = s.snapshot().features[0]
    assert feat["properties"]["label"] == "366999000"
    assert feat["properties"]["name"] is None


def test_static_data_alone_never_puts_a_vessel_on_the_map() -> None:
    """Knowing a ship's name is not knowing where it is."""
    s = stream()
    s.ingest_message(static())
    assert s.snapshot().features == []


def test_class_b_is_recorded_as_class_b() -> None:
    s = stream()
    s.ingest_message(position(kind="StandardClassBPositionReport"))
    assert s._vessels["366999000"].ais_class == "B"


# -- the AIS sentinels -----------------------------------------------------

def test_the_speed_sentinel_is_refused() -> None:
    """102.3 kn means "not available". It is a plausible-looking number that
    would be dead-reckoned into a vessel crossing the Atlantic between two
    polls."""
    s = stream()
    s.ingest_message(position(sog=102.3))
    assert s._vessels["366999000"].sog_kn is None


def test_the_course_and_heading_sentinels_are_refused() -> None:
    s = stream()
    s.ingest_message(position(cog=360.0, TrueHeading=511))
    v = s._vessels["366999000"]
    assert v.cog_deg is None and v.heading_deg is None


def test_the_position_sentinels_are_refused() -> None:
    """91/181 are valid numbers. Trusting them puts vessels at the north pole
    and on the date line, where they are invisible rather than obviously
    wrong."""
    s = stream()
    s.ingest_message(position(lat=91.0, lon=181.0))
    assert s.snapshot().features == []


def test_a_message_with_no_mmsi_is_dropped() -> None:
    s = stream()
    s.ingest_message({"MessageType": "PositionReport", "MetaData": {},
                      "Message": {"PositionReport": {"Latitude": 37.0,
                                                     "Longitude": -76.0}}})
    assert s._vessels == {}


@pytest.mark.parametrize("junk", [None, [], "text", 7, {},
                                  {"MessageType": "Nonsense"},
                                  {"MessageType": "PositionReport"}])
def test_malformed_input_is_dropped_rather_than_raising(junk) -> None:
    """A single bad frame must not take the socket down. The stream is the
    only path to data that cannot be backfilled -- whatever is broadcast while
    it is reconnecting is gone -- so uptime beats strictness here."""
    s = stream()
    assert s.ingest_message(junk) is None
    assert s._vessels == {}


# -- ageing ----------------------------------------------------------------

def test_a_quiet_vessel_goes_stale_rather_than_disappearing() -> None:
    """Silence is ambiguous: left the box, switched off, or simply has not
    transmitted -- a Class A vessel at anchor reports only every 3 minutes.
    Deciding what the silence means is an analysis question, not a transport
    one, so the vessel stays on the map wearing its age."""
    s = stream()
    s.ingest_message(position())
    s._vessels["366999000"].t_position = time.time() - (STALE_S + 60)
    feats = s.snapshot().features
    assert len(feats) == 1
    assert feats[0]["properties"]["stale"] is True
    assert feats[0]["properties"]["age_s"] > STALE_S


def test_a_long_dead_vessel_is_forgotten_to_bound_memory() -> None:
    s = stream()
    s.ingest_message(position())
    s._vessels["366999000"].t_position = time.time() - (DROP_S + 60)
    assert s.snapshot().features == []
    assert s._vessels == {}


# -- the cold-start guard --------------------------------------------------

def test_class_b_static_arrives_in_two_parts_and_both_are_read() -> None:
    """Message 24 is split: part A carries the name, part B the type, call
    sign and dimensions, and they are separate transmissions. A Class B
    vessel is therefore named before it is typed, and the code must not wait
    for one to accept the other."""
    s = stream()
    s.ingest_message(position("367000001",
                              kind="StandardClassBPositionReport"))
    s.ingest_message({"MessageType": "StaticDataReport",
                      "MetaData": {"MMSI": "367000001"},
                      "Message": {"StaticDataReport": {
                          "PartNumber": 0, "ReportA": {"Name": "SEA HAWK"}}}})
    v = s._vessels["367000001"]
    assert v.name == "SEA HAWK" and v.ship_type is None
    assert v.typed is False, "the name half says nothing about what it is"
    assert v.heard_static is True

    s.ingest_message({"MessageType": "StaticDataReport",
                      "MetaData": {"MMSI": "367000001"},
                      "Message": {"StaticDataReport": {
                          "PartNumber": 1,
                          "ReportB": {"ShipType": 37, "CallSign": "WDG1234",
                                      "Dimension": {"A": 6, "B": 4,
                                                    "C": 2, "D": 2}}}}})
    assert v.ship_type == 37 and v.subtype == "pleasure"
    assert v.length_m == 10.0 and v.call_sign == "WDG1234"


def test_a_flattened_static_report_is_read_too() -> None:
    """A decoder that flattens part B into the body is as likely as one that
    nests it, and which one the service uses is not worth a broken type
    histogram to find out."""
    s = stream()
    s.ingest_message(position("367000002"))
    s.ingest_message({"MessageType": "StaticDataReport",
                      "MetaData": {"MMSI": "367000002"},
                      "Message": {"StaticDataReport": {
                          "Name": "FLAT", "ShipType": 70}}})
    assert s._vessels["367000002"].subtype == "cargo"


def test_the_invalid_half_of_message_24_is_ignored() -> None:
    """THE ZERO-LENGTH BUG. The decoder emits both halves on every message 24
    and marks the absent one Valid: false, filled with zeros. Read without the
    flag, a name-only part A carries a length of 0 m that overwrites the real
    length part B supplied, and a 40 m workboat renders as the smallest
    circle on the map."""
    s = stream()
    s.ingest_message(position("367000009"))
    # part B first: the real type and dimensions
    s.ingest_message({"MessageType": "StaticDataReport",
                      "MetaData": {"MMSI": "367000009"},
                      "Message": {"StaticDataReport": {
                          "PartNumber": True,
                          "ReportA": {"Valid": False, "Name": ""},
                          "ReportB": {"Valid": True, "ShipType": 52,
                                      "CallSign": "WTUG9",
                                      "Dimension": {"A": 25, "B": 15,
                                                    "C": 5, "D": 5}}}}})
    v = s._vessels["367000009"]
    assert v.length_m == 40.0 and v.subtype == "tug"

    # then part A: name only, with an invalid all-zero part B alongside it
    s.ingest_message({"MessageType": "StaticDataReport",
                      "MetaData": {"MMSI": "367000009"},
                      "Message": {"StaticDataReport": {
                          "PartNumber": False,
                          "ReportA": {"Valid": True, "Name": "BIG TOOT"},
                          "ReportB": {"Valid": False, "ShipType": 0,
                                      "CallSign": "",
                                      "Dimension": {"A": 0, "B": 0,
                                                    "C": 0, "D": 0}}}}})
    assert v.name == "BIG TOOT"
    assert v.length_m == 40.0, "the invalid half must not zero the length"
    assert v.ship_type == 52, "nor the type"
    assert v.call_sign == "WTUG9"


def test_all_zero_dimensions_mean_not_available() -> None:
    """Zero is the AIS encoding for "not available" in every dimension field,
    Class A included. It is never a length of zero metres."""
    s = stream()
    s.ingest_message(position())
    s.ingest_message(static(a=0, b=0, c=0, d=0))
    v = s._vessels["366999000"]
    assert v.length_m is None and v.width_m is None


def test_static_parts_are_counted_so_unknown_can_be_diagnosed() -> None:
    """A pile of 'unknown' is either vessels that never identified themselves
    or a parser dropping what they sent. From outside, these counts are the
    only way to tell those apart."""
    s = stream()
    s.ingest_message(position("1"))
    s.ingest_message(static("1", 70))
    s.ingest_message({"MessageType": "StaticDataReport",
                      "MetaData": {"MMSI": "2"},
                      "Message": {"StaticDataReport": {
                          "ReportA": {"Valid": True, "Name": "X"},
                          "ReportB": {"Valid": False}}}})
    s.ingest_message({"MessageType": "StaticDataReport",
                      "MetaData": {"MMSI": "2"},
                      "Message": {"StaticDataReport": {
                          "ReportA": {"Valid": False},
                          "ReportB": {"Valid": True, "ShipType": 37}}}})
    parts = s.snapshot().static_parts
    assert parts == {"msg5": 1, "A": 1, "B": 1, "neither": 0}


# -- a name is not evidence of static data --------------------------------

def test_a_name_from_metadata_does_not_count_as_static_data() -> None:
    """THE PROBE'S OWN BUG, AND A GOOD ONE.

    aisstream enriches every message's MetaData with a ShipName from its own
    database, so a name arrives free with the first position report. The
    first probe run counted names and reported "215 of 225 had sent static
    data" for a window in which almost none had. A number labelled as one
    thing and measuring another is the failure mode this whole project is
    built around, and it turned up in the tool written to guard against it.
    """
    s = stream()
    s.ingest_message({"MessageType": "PositionReport",
                      "MetaData": {"MMSI": "366000003",
                                   "ShipName": "ENRICHED"},
                      "Message": {"PositionReport": {
                          "Latitude": 37.0, "Longitude": -76.0}}})
    v = s._vessels["366000003"]
    assert v.name == "ENRICHED"
    assert v.typed is False, "a name is not a type"
    assert s.snapshot().n_typed == 0


def test_n_typed_counts_only_vessels_that_identified_themselves() -> None:
    s = stream()
    s.ingest_message(position("1"))
    s.ingest_message(position("2"))
    s.ingest_message(static("2", 70))
    assert s.snapshot().n_typed == 1


# -- the cold-start guard --------------------------------------------------

def test_a_young_stream_declares_itself_incomplete() -> None:
    """THE POINT OF THE FILE. Four seconds after connecting the table holds
    the handful of vessels that happened to transmit in those four seconds. A
    map drawn from it looks like an empty sea, and reporting that as an
    absence is the error this project has a standing rule against."""
    s = Stream(BOX, api_key="k")
    s._started_at = time.time() - 4.0
    s._connected = True
    snap = s.snapshot()
    assert snap.warming is True
    assert snap.listening_s < WARMUP_S


def test_a_disconnected_stream_is_warming_however_long_it_has_run() -> None:
    """A reconnecting stream is just as incomplete as a cold one, and the
    reader should not have to combine two fields to discover that."""
    s = stream()
    s._connected = False
    assert s.snapshot().warming is True


def test_a_settled_connected_stream_is_not_warming() -> None:
    assert stream().snapshot().warming is False


def test_the_snapshot_reports_how_long_it_has_been_listening() -> None:
    s = stream()
    assert s.snapshot().listening_s >= 600.0


def test_a_stream_error_survives_into_the_snapshot() -> None:
    s = stream()
    s._connected = False
    s._error = "ConnectionClosed: 1006"
    assert s.snapshot().error == "ConnectionClosed: 1006"


# -- filtering and counts --------------------------------------------------

def test_subtype_filtering_selects_without_distorting_the_counts() -> None:
    """by_subtype counts everything heard, not everything shown. A filter is
    a view; it must not change what the panel says is out there."""
    s = stream()
    s.ingest_message(position(mmsi="1"))
    s.ingest_message(static(mmsi="1", ship_type=70))
    s.ingest_message(position(mmsi="2"))
    s.ingest_message(static(mmsi="2", ship_type=80))
    s.ingest_message(position(mmsi="3"))
    s.ingest_message(static(mmsi="3", ship_type=30))

    snap = s.snapshot(("cargo", "tanker"))
    assert len(snap.features) == 2
    assert snap.by_subtype == {"cargo": 1, "tanker": 1, "fishing": 1}


def test_an_empty_filter_tuple_shows_everything() -> None:
    s = stream()
    s.ingest_message(position())
    assert len(s.snapshot(()).features) == 1


# -- the subscription wire format ------------------------------------------

def test_the_bounding_box_is_latitude_first() -> None:
    """THE ONE THING THAT FAILS SILENTLY. The service wants [[lat, lon], ...]
    -- the opposite order from GeoJSON and from everything else in this
    project. Reversed, it subscribes to a box in the Indian Ocean and delivers
    an empty stream that looks exactly like a quiet sea."""
    import json
    sub = json.loads(Stream(BOX, api_key="k")._subscription())
    (s_lat, w_lon), (n_lat, e_lon) = sub["BoundingBoxes"][0]
    assert (s_lat, n_lat) == (36.0, 39.6)
    assert (w_lon, e_lon) == (-77.2, -71.0)
    assert s_lat < n_lat and w_lon < e_lon


def test_only_the_message_types_we_use_are_requested() -> None:
    import json
    sub = json.loads(Stream(BOX, api_key="k")._subscription())
    assert set(sub["FilterMessageTypes"]) == {
        "PositionReport", "StandardClassBPositionReport",
        "ExtendedClassBPositionReport", "ShipStaticData",
        "StaticDataReport"}


def test_class_b_static_data_is_subscribed_to() -> None:
    """THE BUG THE FIRST REAL RUN EXPOSED.

    ShipStaticData is AIS message 5, which only Class A transmits. Class B
    identifies itself with message 24, StaticDataReport. On the first run of
    the real box 127 of 225 vessels were Class B, and without message 24 not
    one of them could ever acquire a ship type -- 215 of 225 came out as
    'other', against 4 cargo and 1 tanker in the Chesapeake approaches. A
    type histogram that implausible was the subscription's fault, not the
    sea's.
    """
    import json
    sub = json.loads(Stream(BOX, api_key="k")._subscription())
    assert "StaticDataReport" in sub["FilterMessageTypes"]


# -- credentials -----------------------------------------------------------

def test_a_missing_key_is_its_own_error() -> None:
    """Distinct from a stream that ran and heard nothing -- the fixes are
    unrelated and one of them is not a finding about the sea."""
    import asyncio
    s = Stream(BOX, api_key="")
    s._key = ""
    with pytest.raises(MissingCredentials):
        asyncio.run(s.start())
    assert s._task is None, "a refused start must not leave a task behind"


def test_the_feature_speed_is_in_metres_per_second_like_the_air_layer() -> None:
    """Named and scaled to match the air domain so one dead-reckoner in the
    browser serves both without caring which domain a feature came from."""
    s = stream()
    s.ingest_message(position(sog=10.0))
    p = s.snapshot().features[0]["properties"]
    assert p["speed_mps"] == pytest.approx(5.14444, rel=1e-4)
    assert p["heading"] == 91.0


def test_constants_are_derived_from_ais_reporting_intervals() -> None:
    """Not round numbers picked by eye. A Class A vessel at anchor reports
    every 3 minutes, so a table younger than that has provably not heard the
    stationary traffic, and 'stale' has to be at least two missed reports."""
    assert WARMUP_S >= 180.0, "at least one full anchor reporting interval"
    assert STALE_S >= 2 * 180.0
    assert DROP_S > STALE_S


# -- warming is measured, not timed ---------------------------------------

def test_a_stream_still_finding_vessels_is_warming_however_old() -> None:
    """THE LESSON FROM THE FIRST REAL RUN. At t=120s the box was still
    discovering 123 vessels a minute against a peak of 129 -- a straight
    line, not a curve flattening out. The old stopwatch called that ready.
    Ten minutes of uptime is no evidence at all if the arrivals have not
    slowed."""
    s = stream()
    now = time.time()
    s._peak_rate = 130.0
    s._discovered = [now - i * 0.5 for i in range(120)]   # ~120/min, still hot
    snap = s.snapshot()
    assert snap.warming is True
    assert snap.listening_s > 500, "and it is not young"
    assert snap.discovery_per_min > 100


def test_a_stream_whose_arrivals_have_decayed_is_settled() -> None:
    s = stream()
    now = time.time()
    s._peak_rate = 130.0
    s._discovered = [now - 5.0, now - 30.0]               # ~2/min
    snap = s.snapshot()
    assert snap.warming is False
    assert snap.discovery_per_min < SETTLED_FRACTION * snap.peak_discovery_per_min


def test_the_floor_holds_even_when_nothing_has_arrived() -> None:
    """A socket that has been open for four seconds and heard nothing is not
    settled, it is new. Without the floor, 'no arrivals' would read as
    'arrivals have decayed' and the emptiest possible table would present
    itself as the most trustworthy."""
    s = Stream(BOX, api_key="k")
    s._started_at = time.time() - 4.0
    s._connected = True
    s._peak_rate = 100.0
    assert s.snapshot().warming is True


def test_no_peak_yet_means_not_settled() -> None:
    """Before a full discovery window has elapsed there is nothing to
    compare against, and an unmeasured rate must not pass for a low one."""
    s = Stream(BOX, api_key="k")
    s._started_at = time.time() - 200.0
    s._connected = True
    assert s._peak_rate == 0.0
    assert s.snapshot().warming is True


def test_the_peak_ignores_the_first_partial_window() -> None:
    """Five vessels in the first five seconds is 60/min extrapolated, and if
    that set the peak then almost any later rate would clear the 30%
    threshold and the stream would call itself settled on its second
    sample."""
    s = Stream(BOX, api_key="k")
    s._started_at = time.time() - 5.0
    s._discovered = [time.time()] * 5
    rate, peak = s.discovery_rate()
    assert rate > 0
    assert peak == 0.0, "too early to know what busy looks like"


def test_the_discovery_rate_is_published_for_inspection() -> None:
    """Published so a reader can watch the decay rather than trust a flag --
    the flag is a threshold on these two numbers and nothing more."""
    snap = stream().snapshot()
    assert hasattr(snap, "discovery_per_min")
    assert hasattr(snap, "peak_discovery_per_min")
