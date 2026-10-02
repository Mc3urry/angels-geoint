# Phase F pre-registration: non-cooperative aviation at the ADS-B obligation boundary

**Written 2026-10-02, BEFORE any masked rate, any null, or any statistic is
computed.** Committed first; the analysis that follows cites this commit.

> **AMENDED 2026-10-02 — see AMENDMENT 1 at the foot of this file.** The
> independent channel is now MLAT and Mode S only; TIS-B is reported
> separately and never pooled. The original definition below is left as
> written.

An analysis plan produced after the numbers is not an analysis plan. This
project has held that line for the maritime domain — `review-session.json`
was committed before the first chip was looked at — and holds it here.

---

## DECLARED PRIOR EXPOSURE

**This is not a clean pre-registration and must not be described as one.**

Before writing this, the following were computed and seen:

- the independent/cooperative ratio by altitude band (0.0432 below 2,500 ft
  falling monotonically to 0.0027 above 18,000);
- a ratio table by altitude band against **distance from DCA alone**, in
  which the two bands below 10,000 ft appeared flat inside 30 nm and 3 to 4
  times higher outside, while the bands above 10,000 ft showed only a gentle
  drift.

**That table is superseded and its x-axis was wrong.** Appendix D section 1
names four airports in or beside the collection box — Reagan National,
Dulles, Baltimore/Washington, and Joint Base Andrews — so the veil is the
**union of four 30 nm circles**, not a circle around DCA. Rings at 40 to 55
nm from DCA lie partly inside that union. The apparent step was therefore
not cleanly a boundary crossing, and no part of the design below is derived
from it.

The honest position: a suggestive pattern was seen on a wrong axis. The
design below is specified on the correct axis, and the prior exposure is
declared so a reader can discount it.

---

## THE HYPOTHESIS

14 CFR 91.225(d) requires ADS-B Out:

- in Class A (at and above 18,000 ft MSL);
- in Class B and Class C airspace;
- within 30 nm of an appendix D section 1 airport, **surface to 10,000 ft
  MSL**;
- above a Class B or C ceiling within its lateral limits, to 10,000 ft MSL;
- in Class E at and above 10,000 ft MSL in the 48 states and DC, excluding
  airspace at and below 2,500 ft AGL.

So **below 10,000 ft the obligation switches off at the veil boundary**, and
**at and above 10,000 ft it applies everywhere in this box**.

**H1.** The share of aircraft fixes that are non-cooperative is higher in
airspace where ADS-B Out is not required than where it is, at the same
distance from the receiver network and on the same days.

**H0.** The share does not differ once coverage, altitude and distance are
accounted for.

**The claim, whatever the result, is "did not report" and never "broke the
rule".** 91.225(e) exempts aircraft without engine-driven electrical
systems; 91.225(g) permits ATC-authorised deviation; state and military
aircraft are a separate matter; equipment failure is indistinguishable from
choice. The data cannot tell compliance from exemption and will not be said
to.

---

## UNIT, OUTCOME, GROUPS

**Unit.** A (0.05 degree cell, altitude band, day) with at least 200
cooperative fixes. The floor is set here, in advance, and is not tuned.

**Outcome.** Non-cooperative share, `ind / (ind + coop)`, where `ind` is a
fix whose `type` is one of `mlat`, `mode_s`, `tisb_icao`, `tisb_trackfile`,
`tisb_other`, and `coop` is one of `adsb_icao`, `adsb_other`, `adsr_icao`,
`adsr_other`. A share rather than a ratio: bounded, and its variance is
known.

**Running variable.** Signed nautical miles to the nearest point of the
**union of 30 nm circles** around every appendix D section 1 airport whose
circle intersects the collection box. Negative inside, positive outside —
the same signed construction as the maritime landward/seaward split.

Airport coordinates are **fetched from an FAA source and recorded in the
artefact**, never hardcoded from memory, and the resulting union is written
out as a geometry alongside the result.

**Treatment.** Fixes below 10,000 ft MSL. The obligation changes sign at the
boundary.

**Control.** Fixes at and above 10,000 ft MSL. The obligation applies on
both sides, so any gradient across the boundary there is confound —
receiver geometry, traffic composition, anything but the rule. **The control
is the point of the design**, exactly as the AIS-explained control was what
turned the maritime 3 nm null from "no effect" into "measured absence".

**The statistic is the difference in differences**: the step across the
boundary in the treatment, minus the step across the boundary in the
control. H1 predicts it is positive. A step that is equal in both is a
confound and refutes H1 regardless of its size.

---

## THE COVERAGE MASK

Measured and fixed before the statistic is computed.

1. A cell enters the denominator only if it holds at least one
   **independent** fix across the whole window, or is shown to have coverage
   by track continuity (an aircraft fixed independently in cells on both
   sides of it in one pass).
2. Cells with cooperative traffic and no evidence of independent coverage
   are **excluded and counted**. On the unmasked data that is 390 of 2,296
   cells and 6.6% of fixes. The excluded fraction is published with the
   result.
3. No absence is read outside the mask. This is the aviation unheard water
   and it is stated, not estimated.

---

## THE NULLS

The same two as the maritime domain, with the same seeds and trial counts:
**2,000 scattered, 400 shift**.

- **Scattered** re-throws fixes independently within the mask. Expected to
  reject for both treatment and control, as it did for every maritime limit:
  it measures where aircraft are.
- **Shift** translates the whole spatial pattern within the mask, preserving
  clustering. **This is the one that decides.** A difference in differences
  that does not survive the shift is clustering, not a boundary.

---

## DECISION RULE, SET NOW

**Support for H1** requires all of:

- the difference in differences is positive;
- it survives the shift null at p <= 0.05 against 400 shifts, stated as k of
  401 rather than as a bare p;
- it survives with the coverage mask applied and with the 200-fix floor;
- it does not reverse when the altitude split is moved to 8,000 or 12,000 ft.

**Refutation** is any of: a non-positive difference; failure of the shift
null; or a control step statistically indistinguishable from the treatment
step.

**Multiplicity.** Four altitude bands, two groups, two split-point
sensitivities. The primary test is the single difference in differences at
the 10,000 ft split. Everything else is sensitivity and is labelled as such
in the output, not promoted if it happens to be larger.

**The result is reported whichever way it falls.** The maritime domain
published a null and is stronger for it.

---

## STOP CONDITIONS

The study stops and is written up as a negative methods result if:

- the coverage mask leaves too little area outside the veil union to compare
  — to be measured next, before any outcome is computed;
- track continuity shows independent coverage is patchy *within* the mask in
  a way the mask cannot model;
- the testable n after masking and the 200-fix floor falls below what could
  detect a doubling of the share. **A small n is a finding, reported as one,
  and never a reason to loosen the mask.**

---

## SCOPING, WHICH IS A CONSTRAINT AND NOT A CAVEAT

**Platforms and institutions, never persons.** Of the 759 never-cooperative
aircraft in the window, the 52 that resolve to a registration are mostly
light general aviation and rotorcraft. An ICAO hex in a 70 nm circle
resolves to a named owner at a home airport.

1. **Aggregate only.** Rates per cell, band and side of the boundary. No
   hex, no tail number, no individual track in any artefact, figure or
   commit.
2. **No registration enrichment.** The `r` and `t` fields arrive in the feed
   and are not queried against any owner database.
3. **If it cannot be analysed without naming it, it is not analysed.**

---

## WHAT IS NOT BEING TESTED

- The Washington DC SFRA and Flight Restricted Zone impose their own
  reporting duties inside 30 nm of DCA. They overlap the veil and are not
  separable with this data. Not claimed.
- Compliance. See the hypothesis.
- Any domain other than the air, and any period other than the collected
  window.

---

*Window at the time of writing: 8 days, 1,635 polls, 1,545,237 position rows,
`data/raw/aviation-adsbfi/`. The window will be longer when the analysis
runs; the plan does not change because of that.*


---

# AMENDMENT 1 — 2026-10-02: TIS-B is not an independent observer

**Changed.** The outcome above defines `ind` as `mlat`, `mode_s`,
`tisb_icao`, `tisb_trackfile` and `tisb_other` pooled. **The primary measure
is now `mlat` and `mode_s` only.** TIS-B is still computed and reported, in
its own column, and is never pooled into the independent channel.

**Why — the mechanism.** TIS-B is uplinked from ground radar *for the benefit
of ADS-B-equipped aircraft in a service volume*. If its presence depends on a
nearby cooperative client, TIS-B detections are correlated with the
cooperative denominator they are divided by, and the ratio is partly a
measure of itself.

**Why — the test.** Cells binned by cooperative fix density:

    coop fixes in cell   cells   with mlat   with tisb
                  1-99     886       33.1%       28.4%
               100-499   1,129       59.6%       51.0%
             500-1,999     245       68.6%       72.2%
           2,000-9,999      28       78.6%       89.3%

TIS-B is the less likely of the two in sparse cells and the more likely by
ten points in dense ones. A sensor that watches the sky does not behave that
way; a service provisioned toward traffic does. MLAT has no such dependency —
it is solved from arrival-time geometry across four or more receivers and is
not provisioned toward anything.

**Which way it cuts, which is the part that matters.** Against the
hypothesis. The pooled series carried the large values and all of the
apparent structure, including 0.0490 at 10 to 20 nm outside the boundary.
MLAT alone is smooth, 2.4x across the whole span, and shows **no step at the
boundary at all** — 0.0112 inside against 0.0100 outside. This amendment
makes a positive result less likely, not more.

**Timing.** Made before the difference in differences was computed, on a
mechanism argument and a correlation test, never on an outcome.

---

# NOT AMENDED — 2026-10-02: the unit floor, and why it stays

The unit above is (cell, altitude band, day) with at least 200 cooperative
fixes. Measured: that keeps **607 of 34,312 units, holding 18.0% of the
data**. Collapsing the day dimension keeps 78.6%. The floor was specified
against a unit that had not been sized, and that was a mistake.

**It is not changed.** Binned shares have already been seen, so any change to
the unit is outcome-adjacent in a way the TIS-B amendment is not. Both are
run instead: **the pre-registered unit as primary, the day-collapsed unit as
a declared sensitivity.** If they disagree, that is reported — not resolved
in favour of whichever looks better.
