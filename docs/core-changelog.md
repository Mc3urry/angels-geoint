# Core changelog

Every change forced on `angels/core/` by adding a new domain.

**This file is a deliverable, not bookkeeping.** It is the evidence for the
entire architectural claim: that the detection logic is genuinely
domain-blind, and that a second domain slots in through the adapter seam.

The number of entries below is a result. Report it.

## Format

```
### <date> — <what forced it>
**Change:** what you altered in core
**Why the abstraction did not cover it:** the honest reason
**Alternative considered:** what you could have done in the adapter instead
```

---

### Phase 1 — initial design
Baseline. `Position`, `Report`, `Observation`, `Track`, `DiscrepancyEvent`,
plus the `PlausibilityModel` protocol. Designed against aviation but with
maritime deliberately in mind.

Predictions to check in Phase 3:
- AIS report intervals vary by vessel class and speed, so a single
  `expected_report_interval_s` may prove too blunt.
- SAR observations arrive as whole scenes at one instant, unlike the
  continuous MLAT stream. `matching` should already handle this via
  `Track.position_at`, but it is untested against a real scene.
- MMSI is reused across hulls far more than ICAO24 is across airframes.
  `identity` may need a notion of identifier trustworthiness.

### 2026-09-02 — collector uptime added to core
**Change:** new `angels/core/uptime.py` (`HeartbeatLog`, `sessions`,
`uptime_intervals`, `blind_intervals`, `was_collecting`). Stdlib only, so the
core dependency rule still holds.

**Why the abstraction did not cover it:** it did not exist yet, and the gap is
real. `PlausibilityModel.coverage()` answers "would a report have been
RECEIVED here", which silently assumes we were listening at all. An hour with
no data has two completely different explanations — the sky was quiet, or the
collector was down — and nothing in the model could tell them apart.

**Prompted by:** an actual hole at hour 20 on 2 September 2026. Files exist for
hours 18, 19 and 21. Almost certainly a stopped poller, but there is no way to
prove that from the archive, which is exactly the problem. Retroactive uptime
is unknowable, so the collector now records that it was alive and asked,
separately from what came back.

**Alternative considered:** inferring downtime from the absence of Parquet
files. Rejected — it cannot distinguish a stopped collector from a genuinely
empty region, it breaks whenever the flush interval changes, and it says
nothing about polls that ran and failed. A failed poll is still evidence we
were looking.

**Consequence for Phase 2:** `detectors/gaps.py` must call `blind_intervals`
and exclude those windows before reporting anything. A gap inside our own
downtime is not a finding.

### 2026-09-13 — national collection footprint added, core unchanged
**Change:** none to `angels/core/`. Recorded here precisely because nothing
changed.

Scaling collection from a 2-square-degree metro box to the whole continental
United States — a ~600× increase in area, twenty times the aircraft per poll,
and a second collector running at a twentyfold different cadence — touched
`config.py`, one adapter function signature (`read_archive(..., dataset=)`),
two scripts and a PowerShell wrapper. `models.py`, `plausibility.py` and every
detector were untouched and every one of their tests passed unmodified.

**Why the abstraction covered it:** the detectors take a `Track` and a
`PlausibilityModel` and nothing else. Neither carries a region. Chicago
traffic and Baltimore traffic are the same type, arriving through the same
seam, and a detector has no way to tell — which is the property the design was
built for and the first time it has been tested at a scale change rather than
a domain change.

**What did have to change, and why it is adapter work rather than core work:**

- `read_archive` grew a `dataset` argument. The two footprints write separate
  Parquet trees because their sample rates differ twentyfold, and any rate
  computed over the union — reports per hour, gap length, track continuity —
  would be a weighted average of two incomparable regimes. Wrong without
  looking wrong. This is a storage-layout concern, which is where it lives.
- `max_gap_s` moved from an adapter default to a per-footprint setting. A gap
  threshold is only meaningful relative to the polling interval: 900 s is
  thirty missed polls at 30 s and less than two at 600 s, so the inherited
  default would have fragmented every national track on a single dropped poll,
  and the detectors would have read the fragments as evidence. This is the one
  genuinely subtle finding of the change, and worth stating in the write-up:
  **a parameter that is correct is not therefore portable.**
- `CollectorLock` and `HeartbeatLog` needed no change at all. Both were already
  keyed by collector name, from the 2026-09-02 entry above, which turned out to
  be exactly the right granularity a year before there was a second collector.
  Two names may hold locks simultaneously; two of one name still cannot.

**Alternative considered:** one archive with a `region` column, filtered at
query time. Rejected. It is tidier on disk and worse everywhere else — every
downstream aggregation would silently mix sample rates unless every query
remembered to filter, and "unless every query remembers" is not a design.

**Cost to state honestly in the thesis, not to discover in review:** over a
2° box, receiver coverage is near enough uniform to ignore. Over CONUS it is
not. OpenSky is dense along the Northeast corridor and thin over the Great
Basin; traffic density varies the same way; both correlate with terrain and
population. A naive national map of silences is a map of where nobody is
listening. `core/coverage.py` therefore stops being a nice-to-have and becomes
load-bearing, and it has to be spatially varying rather than a scalar.

**What it buys:** the geographic claim moves from one boundary (the DC SFRA,
a case study, and a committee is right to ask whether the effect belongs to
boundaries or to Washington) to ~37 Class B airspaces plus the prohibited
areas, the MOA network and the ADIZ — enough to test whether the effect is a
property of boundary *type*, controlling for traffic density.

### Phase 3 — maritime adapter
_(fill in as you go)_
