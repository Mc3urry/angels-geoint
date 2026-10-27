# Phase G pre-registration: the land domain at state lines

> **DRAFT. NOT YET THE REGISTRATION.** The framing below was decided by Joshua
> on 2026-10-27 and is written down before any land statistic exists. The
> unit, the anomaly definition, the statistic and the decision rule are
> marked **TO BE WRITTEN**. They depend on G3 (the mask) and G4 (segment
> speed). This file becomes the registration only when every section is
> filled and the heading above is replaced by **REGISTERED <date>** in the
> same commit. No land statistic may be computed before that commit, and the
> analysis cites it.

---

## DECLARED PRIOR EXPOSURE

Seen before writing this:

- which states publish keyless WZDx, which need a key, and which publish
  nothing (G0, 2026-10-05);
- the 38 border transit feeds and the states each touches (G2b, 2026-10-24);
- three days of WZDx ledger: storage, cadence, decorative `update_date`,
  blind feeds;
- about a day of vehicle positions: counts, storage and blind feeds, with no
  speed computed.

**Not seen:** any segment speed, any slowdown, any comparison of the declared
layer with the observed one, and anything about either side of any border.
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
- **Pioneer Valley's Swiftly feed** cannot be polled without a vendor key, so
  only the agency's own feed is used. This is a choice of source, not a gap.
- **Time of day.** The collector is a laptop that commutes, so its gaps fall
  at commute hours, which on the road are rush hours. Comparisons are made
  like hour against like hour, or the registration says why not.

---

## TO BE WRITTEN, after G3 and G4

- **The mask (G3):** where independent observation exists at all. CHART
  cameras and the traffic-count segments; a segment outside it is excluded
  and counted, not read as quiet.
- **The anomaly (G4):** what counts as "vehicles slowed here", in units and
  thresholds fixed before G6 runs. G4 de-duplicates fixes on (feed_id,
  vehicle_key, vehicle_ts).
- **The unit:** segment, hour of day and day; and the border distance bands.
  Any border threshold is at most 2,000 m, the buffer G2b selected on.
- **The statistic, the nulls and the decision rule:** the step in the
  undeclared share across the line, the displaced-boundary null, and what
  supports, what refutes, and what is too thin to say.
- **The window:** dates fixed before G6 runs.
