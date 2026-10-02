# The aviation asymmetry, and its retraction

*Finding established 2026-09-18 in the workspace notes. **Retracted in part
2026-10-02**, with the measurement that overturned it. The original claim is
kept in full below rather than deleted, because a project whose whole subject
is misreported absence does not get to quietly delete a misreported absence
of its own.*

---

## What this document is about

The thesis is that cooperative reporting is unauthenticated self-reporting,
that independent sensing observes without consent, and that **the discrepancy
is the intelligence product**. That framing only works where both halves
exist. The three domains were never equally equipped, and the asymmetry
between them is a finding in its own right — it determines what kind of claim
each domain can support.

    domain   cooperative, real time        independent observation
    air      ADS-B via OpenSky             see the retraction below
    sea      AIS via aisstream.io          Sentinel-1 SAR
    land     GTFS-Realtime transit         public DOT cameras (unbuilt)

**Sea** has a discrepancy layer and it is retrospective by construction:
Sentinel-1 lands hours to days late and revisits every 12 days. A vessel
missing from the live AIS map is *not* a dark vessel, it is a vessel that has
not transmitted in a few minutes, which is the normal condition of an
anchored ship. Blending the live view and the dark-vessel product into one
"radar" layer would be the single most damaging thing the front end could do.

**Land** is scoped to institutions deliberately. There is no civilian
cooperative broadcast for private road vehicles; the nearest things are
commercial telematics and camera ANPR, and both cross this project's standing
boundary — **platforms and institutions, never persons**. A container ship is
an institution; a car is a person with a roof.

---

## THE ORIGINAL CLAIM, 2026-09-18 — SUPERSEDED

> **Air can never have a discrepancy layer.** Six sources were tested and
> none returned MLAT or any other non-cooperative position. That is an
> established finding of this project. Anomalies in the air domain are
> therefore found *inside* the cooperative record — transponder gaps,
> identity changes, orbits, implausible kinematics — which is a weaker and
> differently-shaped claim than "radar saw it and ADS-B did not".

That was written as an established finding and acted on for a week: the
aviation detectors were built around in-record anomalies, and the air domain
was described in the plan as structurally incapable of the comparison the
maritime half performs.

**It was wrong, and the reason is recorded in `scripts/check_tisb.py`.** The
one probe whose job was to say whether an independent aviation channel
existed read the aircraft list from `d.get("ac")`. adsb.fi returns it under
`aircraft`. So the probe printed

    0 aircraft within 70 nm of DC

every time it was ever run, with an empty table under it — indistinguishable
from a sky containing no independent traffic. A confident negative, produced
by looking in the wrong place, and it is very likely why nobody noticed that
224 hours of collected ADS-B contained not one MLAT row.

That is this project's recurring defect class in its purest form: **a check
that describes something other than what happened, and cannot tell you so.**
The probe was fixed on 25 September to accept either key and to exit non-zero
when neither is present, because a probe that cannot distinguish "nothing
there" from "I looked in the wrong place" is worse than no probe at all.

---

## THE MEASUREMENT, 2026-10-02

`data/raw/aviation-adsbfi/` now holds **8 days, 1,635 polls, 1,545,237
position rows**. Counting the `type` field, which names the position source:

    position source     rows        share    kind
    adsb_icao      1,493,033       96.62%    cooperative
    adsr_icao         32,104        2.08%    rebroadcast of cooperative content
    mlat               7,309        0.47%    INDEPENDENT
    tisb_icao          6,553        0.42%    INDEPENDENT
    tisb_trackfile     4,095        0.27%    INDEPENDENT
    tisb_other         1,512        0.10%    INDEPENDENT
    adsb_other           452        0.03%    cooperative
    mode_s               135        0.01%    independent, no position
    adsr_other            44        0.00%    rebroadcast

**19,604 rows — 1.27% — carry a position the aircraft did not report.** MLAT
is solved from the arrival-time geometry of a signal across four or more
ground receivers. TIS-B is uplinked from ground radar. Neither is something
an aircraft chooses to emit, which is the entire definition the thesis uses.

A separate and weaker quantity: **74,222 rows (4.80%) have a non-empty
`tisb_fields` array** — an otherwise-cooperative aircraft on which *some
fields* arrived via TIS-B. That is not an independently observed target and
must not be counted as one.

### What the retraction does and does not license

**Does:** the air domain has an independent observation channel. The claim
that it "can never have a discrepancy layer" is withdrawn. A dark-aircraft
analogue — fixed by MLAT or TIS-B, absent from the cooperative feed — is
measurable in data this project is already collecting.

**Does not:** 1.27% is small, and more importantly its coverage is severely
non-uniform. MLAT requires four or more receivers with overlapping reception
of the same aircraft; TIS-B is uplinked only where ground radar covers and
only under particular equipage regimes. **The independent channel's own
coverage is the aviation analogue of searched water, and nothing can be read
into an aviation absence until it is measured**, exactly as
`ais_coverage.py` had to exist before any dark-vessel count did. That is the
step that will make or break an aviation study, and it has not been taken.

### Why it may be the stronger domain, once that is done

Maritime limits carry no change in reporting obligation at 3, 12 or 24 nm
for most vessels, which is one honest explanation for the null this project
measured there. The **ADIZ does**: entry legally requires a filed flight
plan, two-way radio and an operating transponder. The research question —
does non-cooperative behaviour concentrate at governance discontinuities —
has never yet been asked at a boundary where the governance actually attaches
to reporting.

The same scoping boundary applies and bites harder here than anywhere else.
An airliner or a state aircraft is an institution. A private light aircraft
with its transponder off is a person with wings, and the filter for that
belongs in the code, not in a sentence in a README.

---

*Reproduce the table above with `scripts/check_tisb.py`, or directly from
`data/raw/aviation-adsbfi/` by counting the `type` column.*
