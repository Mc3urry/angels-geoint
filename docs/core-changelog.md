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

### Phase 3 — maritime adapter
_(fill in as you go)_
