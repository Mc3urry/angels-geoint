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

### Phase 3 — maritime adapter
_(fill in as you go)_
