# The defect that kept coming back

*A methods note. Written 2026-09-26, after the fifteenth recorded sighting.*

Over the course of this project, fifteen separate faults were found that share
a single shape. None of them was an arithmetic error. Every quantity this
pipeline computes, it computed correctly every time. What failed, fifteen
times, was **what a program said about what it had done.**

That is not a curiosity. It is the most reusable thing the project produced,
and it is the reason the headline result can be trusted more than it could
have been if none of these had been found.

## 1. The class, stated precisely

> **A routine reports an outcome that its own execution does not entitle it
> to report, and nothing in the output distinguishes that from a real one.**

Three properties follow, and together they make this class unusually
dangerous.

**It produces well-formed output.** A crash is a gift: it names a file and a
line. These faults return the right type, in the right range, through the
normal path. `pixel_of` returned a plausible pixel. `_process_alive` returned
a Boolean. `check_tisb.py` returned a count. Nothing is malformed.

**Its output is often the output you expected.** `check_tisb.py` reported zero
TIS-B aircraft for its entire operational life, which is roughly what one
expects over open water. A silent inversion failure produces a chip of empty
sea, which is what most chips look like. The failure mode is camouflaged by
the base rate of the thing being measured.

**It is invisible to the test that would catch anything else.** These are not
edge cases in the computation. They are the computation working perfectly on
an input, or under an assumption, that the report does not mention.

## 2. Why ordinary care does not prevent it

The fifteenth sighting settles this. `correct_clutter.py` printed an
explanation of the `no-data` verdict on a run where no chip carried it — and
explained it using a claim about the detector that had been retracted earlier
the same day. That code was written **two commits earlier, in the change whose
entire purpose was to stop reports describing something other than what
happened**, by someone who had by then catalogued fourteen instances of
exactly this.

Vigilance is not the remedy, because vigilance is what produced that one.

The remedy is structural: **every statement a program makes must be
conditional, by construction, on the thing it describes.** Not "remember to
check whether the category is present" but "the sentence cannot be emitted
unless the category is present."

## 3. The catalogue, by mechanism

The running count in the worklog reached fifteen. A few logged entries
contained more than one instance, so the grouping below has more rows than
fifteen; the count is of sightings, not of lines of code.

### A. "No" and "cannot tell" share a return value

| what | what it reported | what it meant |
|---|---|---|
| `_process_alive` | four collectors dead | `OpenProcess` returned NULL, which covers both *no such process* and *running, and you may not touch it* |
| `git check-ignore -v` | `labels.jsonl` is ignored | exit 0 means *a pattern matched*; the pattern that matched was the negation `!data/events/labels.jsonl` |
| `check_tisb.py` | zero TIS-B aircraft | it read `d.get("ac")`; adsb.fi returns `aircraft`. It had reported a confident zero for its entire life |
| the extras check | `FAILED` | an optional dependency was absent, which is not a failure |

The `_process_alive` case is the one to show an advisor. It did not merely
misreport: `status` offered to clear the lock files of four collectors that
were at that moment writing to disk. **A check that cannot distinguish "no"
from "don't know" will eventually be asked to act on the difference.**

### B. The check verifies the checker's environment, not the thing

| what | why it passed |
|---|---|
| `reproduce.py` | it had only ever run on the machine that wrote it, with `PYTHONPATH` set by hand. On a fresh checkout, three of four stages failed immediately |
| the bootstrap guard | the test had a hole; on its first honest run it caught a pre-existing offender |
| the headless render harness | it screenshotted the card's HTML against the stylesheet. The 240 px clip is applied inline by MapLibre, not in the stylesheet, so the harness rendered every card at a width the browser never uses |
| `pip install -e ".[ml]"` | it resolved to ArcGIS Pro's Python 3.14t, which has no wheels |

A reproduction script that has only been run by its author is not a
reproduction script. It is a record of one person's environment.

### C. The verifier only checks what it generated

`reproduce.py` rebuilt the boundary artefact and compared it against the
committed file honestly — and never asked whether it had rebuilt *the same
thing*. It regenerated six of eight limits, because the command needed
`--near-nm 10` and the artefact did not record which flags had produced it.
The comparison was sound; the premise was not.

The fix was to make the artefact record the parameters, and derive the
rebuild command from the artefact rather than from a line in a script. The
same run also counted a free-text note as a difference and announced a
purely-local stage as network-dependent.

### D. A value copied out of its source, left to drift

| what | drifted to |
|---|---|
| `JUSTIFIED` in `tune_gate.py` | the measured keep-fraction beyond 10 nm was 0.59 on the day the drift was found, and 0.5975 after the next label revision; the constant still said 0.62. It is the denominator of the rule deciding whether a gate may be applied upstream of the boundary test |
| four footprint lists in `collector.ps1` | a fifth collector was added and four separate hardcoded lists had to agree; a regex was blind to the new filename |
| three `*-report.txt` files | hand-captured. The artefacts beside them were regenerated; the reports were not, and carried no timestamp or command by which a reader could tell |

The `JUSTIFIED` case is the most instructive because **the stale value was
stricter than the truth**, so the disqualifier erred conservatively. That was
luck. A transcribed constant has no mechanism that keeps it conservative.

### E. A fix lands on one side of a pair

`_process_alive` had a correct POSIX branch and a wrong Windows branch. The
animation loop was throttled in `live.js` and not in `vessels.js` — which
opens by describing itself as "structurally the twin of live.js". The popup
width option was given to `live.js` the day the card was widened, and to none
of the other three. The aviation collector wrote a heartbeat; the maritime one
did not.

Four times, a principle was established on one member of a pair and never
carried across. The pairs were known to be pairs — one of them says so in its
own first comment.

### F. A routine returns an answer without saying whether it found one

`pixel_of` ran thirty Newton steps against the geolocator and returned
whatever it was holding. Measured afterwards against the pixel the detector
had already recorded, over all 1,150 candidates:

    residual, pixels:  median 0.040   p90 0.060   p99 510   max 998
    off by more than 300 m:  17  (1.5%)

**Bimodal, with nothing in between.** A candidate was either located to four
hundredths of a pixel or it was kilometres away. That is precisely why it
survived: there was never a slightly-wrong chip to notice, only 98.5 % perfect
ones and a handful of open water somewhere else — which look exactly like
ordinary chips.

Four of the 147 hand-read chips were of the wrong ground. Three came back
99.8 % zero and **were written up as a detector defect, committed, and
pushed**: "2.0 % of candidates are detections in the no-data border ramp."
That finding was entirely false. The detector's own position is right to half
a metre; the three are bright vessels of 45 to 117 pixels at SNR 23 to 141.

This is the only sighting that produced a published false finding, and it is
worth understanding why. The evidence for the false claim was three black
chips — and three black chips are exactly what this defect manufactures. **The
fault generated its own corroboration.**

### G. A report names its exclusions from a list written in advance

`classify_candidates.py` printed "N ambiguous held out" and `tune_gate.py`
"N ambiguous excluded". Both spelled the excluded verdict as a literal, so
when a fifth verdict appeared, its rows would have left the fit and the gate
estimate **without appearing in either sentence**. `correct_clutter.py`
computed a denominator as `sum(c.values())`, which was correct until a fifth
verdict existed and would then have lowered every rate silently.

And, at the display layer, the dossier showed the nearest persistent site
whatever its distance — "seen on 2 of 12 passes" printed beside a site 9.7 km
away. The *scoring* had guarded on distance. The *display* had not, which is
the worse half to miss, because it is the half a reader believes.

### H. Reading at a scale that cannot show the thing

Of 147 chips, 70 were re-read at magnification and 77 were judged only at
contact-sheet scale — and **all 77 were called `vessel`**. A verdict class
with no disagreements in it is not a clean result; it is an unexamined one.

The re-read then repeated the mistake one level down. The new sheets cropped
120 pixels of a 600-pixel chip, so each panel showed 600 m of sea rather than
the 3 km the chip holds, and nothing on the panel said so. At that scale a
target 100 m from the mark reads as a displaced ghost, and a 700 m azimuth
smear reads as an edge-to-edge scene artefact. Both readings were made. The
azimuth displacement of a moving vessel is 400–470 m — larger than the offsets
being treated as disqualifying.

Re-cut with a 3 km context view beside a 5× centre view, **eight of fifteen
proposed downgrades reversed.**

## 4. What actually caught them

No single technique found more than a few. The useful list is short:

**A number that cannot be true.** SNR 141 across 117 pixels, in a chip that
is 99.8 % zero. A detection that bright cannot come from nothing. This broke
the false finding, and the contradiction had been sitting in the same table as
the claim.

**Running it as a stranger would.** A fresh `git archive HEAD` checkout, and
the author's own machine rather than the development VM. Three of four
reproduction stages failed on first contact with a second environment.

**An adversarial test written to fail.** The leakage test trains the
classifier's own features to predict distance-to-limit. It returned AUC 0.738
and identified the culprit: the range-pixel column, present as an
incidence-angle proxy, predicting the band at 0.732 alone. A coordinate in
disguise.

**Two independent routes to one number.** The detector's recorded pixel
against the chipper's reconstruction. Searched area by two different
computations over the same mask. Agreement is evidence; the point is to have
two routes at all.

**Mutation testing.** Breaking the code deliberately to see whether the test
notices. The `collector.ps1` parity tests were checked three ways this way.

**Looking at the artefact at the right magnification** — and measuring, rather
than assuming, what the display is showing.

## 5. The rules adopted

Each of these exists because something went wrong that it would have stopped.

1. **Name every excluded category from the data, never from a list.** A
   verdict added later must not be able to leave a count without appearing in
   the sentence that reports the count.
2. **A stored value is never reconstructed.** If the pipeline recorded it,
   downstream reads it. Re-deriving is not a cross-check unless the two are
   compared and the disagreement is reported.
3. **A derivation that must happen verifies itself and refuses.** `pixel_of`
   raises rather than returning a number of unknown quality, and the refusal
   states how far off it was.
4. **A published measurement carries a digest of its input; the consumer
   refuses a stale one.** `keep-fractions.json` carries the SHA-256 of the
   labels it was measured from, and `tune_gate.py` stops rather than falling
   back to a remembered value.
5. **Reports are written by the run that produced them**, stamped with the
   command and the time, on failure as well as success.
6. **A refusal quantifies the miss.** A refusal that does not say what failed
   is the same defect one level up.
7. **Retractions stay in place.** A false finding that was published is marked
   RETRACTED with a pointer to what replaced it, not deleted.

## 6. What it cost, and what it bought

The chip labels were revised three times in two days. Each revision forced a
re-run of four downstream consumers. Four commits in one day were corrections
rather than progress, and one of them retracted a finding that had already
reached a public repository.

Against that:

    within 2 nm, observed / expected
    label set 1:   0.396 published -> 0.512 corrected
    label set 2:   0.396 published -> 0.535 corrected
    label set 3:   0.396 published -> 0.544 corrected

**The headline result survived all three revisions**, and the corrected figure
walked steadily towards the hypothesis without ever threatening to cross the
1.0 that would support it. A number that moves under correction and still
cannot reach is stronger evidence than one that sits still, because it
demonstrates that the correction has real purchase.

The classifier tells the same story from the other side. Removing seven labels
that turned out to be sidelobes and soft returns raised cross-validated AUC
from 0.875 ± 0.064 to 0.923 ± 0.051 — **better, on less data, with a tighter
interval.** Those labels were not information the model lost; they were noise
it had been asked to fit.

And one measurement that had been written off recovered. `near_peak_x_bg` is
0.305 × SNR (IQR 0.288–0.332), so its *level* is the detector's own opinion
restated and cannot validate a label. But its *residual*, once SNR is divided
out, ranks the three chips independently read as sidelobes of a brighter
neighbour at the top of all 147 — from numbers the reader never saw. That is
the one place in this project where a machine measurement and a human reading
were compared blind and converged.

## 7. The claim this supports

A project of this kind asks to be believed about something invisible: vessels
that are present and not reporting. The natural objection is that the analyst
found what he was looking for.

The defence is not that no mistakes were made. Fifteen were found, one of them
published and withdrawn. The defence is that **the mistakes were found by the
project's own machinery, they were recorded rather than tidied away, and the
result did not depend on any of them.** Every correction moved the headline
number in the direction that would have helped the hypothesis, and it still
did not arrive.

*A note on the numbers in this document.* Every figure above was re-derived
from the artefacts by a script, not copied from the worklog, and one of them
was wrong when it was: the `>10nm` keep-fraction is quoted here as 0.59,
which was true on the day the drift was found and became 0.5975 after the
next label revision. Quoting a measured value without saying which run it
came from is mechanism **D** in this document's own catalogue, committed
inside the document about mechanism D. It is corrected above rather than
silently, because that is rule 7.
