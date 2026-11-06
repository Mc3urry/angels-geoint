# Phase G pre-registration: the land domain at state lines

**REGISTERED 2026-11-06.** Every section below was written before any land statistic existed. The analysis (G6, `scripts/road_discrepancy.py`) cites this commit. Nothing below changes except by a dated amendment, committed as one.

---

## DECLARED PRIOR EXPOSURE

Seen before writing this:

- which states publish keyless WZDx, which need a key, and which publish
  nothing (G0, 2026-10-05);
- the 38 border transit feeds and the states each touches (G2b, 2026-10-24);
- three days of WZDx ledger: storage, cadence, decorative `update_date`,
  blind feeds;
- about a day of vehicle positions: counts, storage and blind feeds;
- **added 2026-10-27, G4's plausibility check:** per-FEED speed medians and
  90th percentiles, and the exclusion counts, over one day (2026-10-27).
  Nothing per cell, nothing per hour, and nothing by distance to any line.

- **added 2026-11-06:** the live Land layer in both viewers (vehicle
  positions, declared work zones and state lines drawn on a map), and
  feed-level counts from the API checks: vehicles and feeds answering,
  never speeds. The work-zone change measurement for `mndot` (which fields
  change between stored bodies; decision C below). The share of feeds
  with real projects longer than three years, by feed.

**Not seen:** any cell's speed, any slowdown, any anomaly count, any
comparison of the declared layer with the observed one, and anything about
either side of any border.
Nothing in the framing below was chosen after looking at a land result,
because no land result exists.

---

## THE QUESTION, ON LAND

The research question is whether non-cooperative behaviour concentrates at
governance discontinuities. At sea and in the air, the discontinuity is an
obligation that binds the party that reports: a vessel's AIS duty, an
aircraft's ADS-B duty. Non-cooperation there means not reporting where
reporting is owed.

On land, the discontinuity this project can measure is a **publication
regime**. Maryland publishes its work zones openly. Virginia publishes behind
a key. The District publishes nothing. Nationally, states differ in the same
three ways. The party that cooperates or does not is the **agency**, and what
it does or does not publish is its **declaration** of where the road is
disrupted. Vehicles are not the subject. They are the instrument: transit
fleets report their own positions, and those positions show where traffic
actually slowed.

## THE FRAMING DECISION (2026-10-27)

**Obligation avoidance counts as non-cooperative behaviour, with its context
stated every time it is reported.** In full:

1. **What is measured is a gap, not an intention.** A disruption the vehicles
   observe and no agency declares is an undeclared disruption. That is the
   unit of non-cooperation on land. Why it went undeclared is never inferred
   from the gap alone (rule 9, mechanism M).

2. **"Avoidance" is used only in the sense the evidence allows.** Where an
   agency declines to publish, or publishes behind a barrier, its
   disruptions are not available to the public as declarations. This
   document does not claim that any agency is legally required to publish
   WZDx. Where no requirement is shown, a non-publishing agency is described
   as not taking part in a voluntary standard, in those words, and not as
   evading a duty. If a requirement is shown with a citation, the wording may
   say so.

3. **The comparison is across the line, not between agencies' virtues.** The
   claim tested is that undeclared disruption, as a share of observed
   disruption, steps up where the publication regime steps down. It is not a
   ranking of agencies. A step must survive a displaced-boundary null, the
   same test that refuted the maritime and aviation hypotheses, before it is
   attributed to the regime at all.

4. **Three tiers of statement, and the write-up keeps them apart:**
   - *observed*: vehicles slowed here, and no declaration covered it;
   - *associated*: the share of such gaps changes at the state line, beyond
     the displaced-boundary null;
   - *attributed*: the change is due to the publication regime. This is
     never asserted from this design alone. At most it is "consistent with".

5. **Why it still belongs in this project.** The thesis is that cooperative
   reporting is unauthenticated self-declaration, and that independent
   observation is what tests it. On land the self-declaration is the
   agency's. A declaration that is missing, keyed or wrong is the same kind of
   fact as a transponder that is off. The context differs: a voluntary
   standard rather than a federal mandate. Item 2 exists so that difference
   is never lost in a summary.

---

## KNOWN EXCLUSIONS AND ABSENCES (as of 2026-10-27)

Named here so that none of them can be decided by what the archive happens
to look like:

- **Transit feeds blind for the whole first day:** TANK (`mdb-1695`, timed
  out), Arlington (`mdb-1790`, certificate verification failed) and Suffolk
  County (`mdb-1530`, HTTP 403). C-TRAN was blind because of a catalogue
  misspelling and has been corrected (`URL_CORRECTIONS`).
- **WZDx feeds down or unreliable:** `nmdot` and `ncdot` were down for the
  whole first overnight run, and `mndot` was unreliable. Their status at
  registration will be measured over the full window and stated here.
- **Pioneer Valley's Swiftly feed** cannot be polled: a Swiftly key was
  issued on 2026-11-03, and the feed refuses it (HTTP 401). Only the
  agency's own feed is used, so the G2b duplicate pair stays provisional.
  This is a choice of source, not a gap.
- **Time of day.** The collector is a laptop that commutes, so its gaps fall
  at commute hours, which on the road are rush hours. Comparisons are made
  like hour against like hour, or the registration says why not.

---

## THE HYPOTHESIS, AND WHY IT IS NOT TRUE BY CONSTRUCTION

The obvious design is false by construction. In a state that publishes
nothing, or only behind a key, this project can see no declaration at all,
so every disruption there is "undeclared" by definition. A step in the
undeclared share at a line where publication stops would be the archive's
own boundary, not anybody's behaviour.

So the undeclared share is measured **only where declarations are
visible**: in states whose work zones are published keylessly (the *open*
regime). The question becomes whether an agency that does publish declares
less of its disruption near a line where its neighbour stops publishing
than near a line where its neighbour publishes too.

> **H1.** In open-regime states, the undeclared share of observed
> disruption rises toward state lines where the neighbouring regime is
> keyed or none (treatment), by more than it rises toward state lines
> where the neighbour is also open (control).

**H0.** No such difference beyond what a displaced boundary produces.

On the closed side of a treatment line, only the observed disruption rate
(anomalies per cell-hour in the mask) is reported, as context. No
undeclared share is ever computed for a keyed or none state.

---

## UNIT, OUTCOME, GROUPS

**Cell.** G4's cell: `CELL_DEG = 0.00225`, about 250 m north-south, by
local date and local hour (`scripts/road_speeds.py`, `cell_of`). Local
means the cell's own time zone.

**Anomaly.** As proposed on 2026-10-27, now fixed: a cell-hour whose median
pair speed is under **0.5 x** the same cell's median at the same local hour
on the same kind of day (weekday or weekend), counted only with **5 pairs
from 2 vehicles**, against a baseline built from **at least 5 days** in the
window. The pair rules and the exclusions are G4's, unchanged (speed from two
consecutive fixes of one vehicle on its own clock, 15 to 120 s apart, at most
60 m/s; excluded and counted: no vehicle timestamp, entity id for vehicle
id, duplicate, out of order, too close, gap, impossible, parked).

**Declared.** An anomaly cell-hour is *declared* when a WZDx event satisfies
all three conditions:

1. **Published at the time.** It is in the latest body that feed stored at or
   before the end of that hour, as the agency declared it then, not as it
   was later revised.
2. **Active in that hour.** `start_date` is at or before the end of the hour,
   and either `end_date` is at or after its start, or the end is missing or
   open-ended. An event whose `event_status` is `completed` or `cancelled`
   is not active.
3. **Close enough.** Its geometry passes within **300 m** of the cell's
   centre.

Any feed's event counts, whichever state published it. Otherwise the
anomaly is *undeclared*.

**Outcome.** The undeclared share, `undeclared / anomalies`, over anomaly
cell-hours in the mask.

**Regime.** Each state is *open*, *keyed* or *none*, read from the committed
`data/reference/roads/wzdx-registry.json` (G0) at the registration commit:

- *open*: a keyless feed in the registry;
- *keyed*: a feed exists only behind a key;
- *none*: no feed.

The table is written into the result artefact. A state whose every keyless
feed was blind for more than half of the window is reported as *open,
unreliable*; it stays open in the primary test and is dropped in one
sensitivity.

**Lines.** State boundaries from G2b's TIGERweb source. A *treatment line* is
a stretch of boundary between an open state and a keyed or none state. A
*control line* is a stretch between two open states.

**Distance bands**, measured from the nearest line of each kind, on the
open side only:

- *near*: 0 to 2,000 m, the buffer G2b selected the border feeds on;
- *far*: 2,000 to 10,000 m.

A cell nearer to a line of the other kind than to its own is assigned to
the nearer line. A cell beyond 10,000 m from both kinds is not used.

**The statistic is the difference in differences**:

> (near minus far, at treatment lines) minus (near minus far, at control lines)

each term being an undeclared share. H1 predicts it is positive.

---

## THE COVERAGE MASK

1. A cell enters the analysis only when it has a baseline under the
   anomaly rule (at least 5 days with 5 pairs from 2 vehicles, at that hour,
   on that kind of day). No baseline, no anomaly, no absence read there.
2. Cells with vehicle pairs but no baseline are **excluded and counted**, by
   group and band, and the count is published with the result.
3. CHART cameras and the traffic-count stations are context: where the state
   can see, and how busy the road is. They are not the denominator.

---

## THE NULLS

- **Scattered:** the undeclared label is re-thrown across anomaly
  cell-hours within the mask, 2,000 times, seed 20240621. As at sea and in
  the air, this is expected to reject for reasons that are not the
  hypothesis, and is reported but does not decide.
- **Displaced boundary (shift): this is the one that decides.** Both line
  sets are translated together by a random vector of 3 to 20 km in a random
  direction, 400 times, seed 20240621. Bands and groups are recomputed for
  each displacement, and the statistic is recalculated. The result is
  reported as k of 401.

---

## DECISION RULE, SET NOW

**Support for H1** requires all of:

- the difference in differences is positive;
- it survives the displaced-boundary null at p <= 0.05, reported as k of
  401;
- it holds with the mask and the anomaly floors as written;
- it does not reverse sign when the covering distance is 150 m or 600 m,
  or when a declaration one hour either side counts as covering.

**Refutation** is any of:

- a non-positive difference;
- failure of the displaced-boundary null;
- a treatment step indistinguishable from the control step.

**Multiplicity.** The primary test is the single difference in differences
at 300 m, exact hour, with all open states included. Everything else is
sensitivity, labelled as such and never promoted:

- the covering distance at 150 m and at 600 m;
- a declaration one hour either side counting as covering;
- dropping open but unreliable states;
- weekday only;
- each line type alone.

**Three tiers of statement**, as in the framing above:

- *observed*: an undeclared slowdown;
- *associated*: the difference beyond the null;
- *attributed*: never asserted from this design.

**The result is reported whichever way it falls.**

---

## STOP CONDITIONS

The study stops and is written up as a negative methods result, which is a
finding and not a failure, if any of these holds:

- any of the four groups (treatment near, treatment far, control near,
  control far) holds fewer than **30 anomaly cell-hours** in the mask;
- there are no control lines in the border feeds' footprint, so no
  difference in differences can be formed;
- the anomaly baseline cannot be built for most cells because the
  collector's gaps fall at the same hours every day. This is measured from
  the uptime log first, before any outcome.

**A small n is reported as a small n**, and never answered by loosening the
mask, the floors or the bands.

---

## THE WINDOW

**2026-10-28 00:00 to 2026-11-25 23:59**, in each cell's local time: 29 days.
The window ends before Thanksgiving, so holiday traffic does not have to be
modelled.

Data from 2026-10-28 to the registration commit was already collected and
is used. Only feed-level counts from it were seen (prior exposure above),
and no cell, speed or anomaly was computed.

The `roads-wzdx` and `roads-vehicles` collectors run without changes
through the window, except decision C below. C takes effect at the first
cycle after `roads-wzdx` restarts, and is recorded in the ledger.

---

## DECISIONS TAKEN BEFORE REGISTRATION (Joshua, 2026-11-06)

- **C. mndot's rolling end date.** An `end_date` more than three years
  after the event's own start is read as "no end", for `mndot` only
  (`CONTENT_DIGEST_VERSION = 3`). This changes only which bodies count as a
  changed declaration. For G6, such an event is open-ended, so it is active
  in every hour after its start. Measured while deciding: rolling end dates
  account for 12 of 462 edited `mndot` events over 59 stored bodies. Most of
  `mndot`'s churn is geometry re-snapped by a few metres each cycle. That is
  recorded in FINDINGS and is not excluded here.
- **The 19 redundant WZDx bodies** (sighting 42) stay in the archive. They
  carry no new claim, and no evidence is deleted during the window.
- **Parked fixes** stay stored by `roads-vehicles`. G4 already excludes
  them, and changing the collector during the window would change the
  protocol.
- **CHART's terms.** The viewers run on localhost only. The terms must be
  read before any public deployment. No analysis uses CHART pictures.

---

## WHAT IS NOT BEING TESTED

- Whether any agency is required to publish WZDx (framing, item 2).
- Why an anomaly went undeclared. Causes are never inferred from a gap.
- The undeclared share on the closed side of a line, which is 1 by
  construction.
- Private vehicles individually. Transit fleets are the instrument; no
  vehicle id appears in any artefact, only cells, hours and counts.
