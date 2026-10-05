# The defect that kept coming back

*A methods note. Written 2026-09-26 after the fifteenth recorded sighting; a
sixteenth arrived the next morning and is section 3.J. Extended 2026-10-02 at
twenty-seven, with mechanisms L to R — every one of them committed after this
document existed and was being actively watched for. Extended again the same
evening to twenty-nine, with S and T, both of them committed inside the script
written to catch this class, one of them an hour after a comment in that same
file warned against it. Extended again 2026-10-05 to thirty, with U --
found by an instrument built that afternoon, in a number this document had
already helped publish. Extended again 2026-10-05 to thirty-three, with V
and W, and a third instance of F -- all three found by a test suite built
from recorded server responses rather than from expectations.*

Over the course of this project, **thirty-three** separate faults were found
that share a single shape. Almost none was an arithmetic error. Every quantity
this pipeline computes, it computed correctly. What failed, thirty-three
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

The running count in the worklog reached **thirty-three**. A few logged
entries contained more than one instance, so the grouping below has more rows
than that; the count is of sightings, not of lines of code.

Mechanisms A to K came from the maritime domain. L to T were added as the
project moved to aviation and then to roads, and they are the more
instructive half: by then the class was known, named, documented in this
file, and being actively watched for. **Knowing the failure mode did not
prevent it.** Three of the seven L-to-R sightings were committed by someone
who had read this document that week.

S and T go further. Both were committed **inside a script whose entire
purpose was to catch this class before it reached an analysis** — a probe
that reports every field of every feed with the type of its value, written
precisely because a string column had once been filtered as a number. The
probe did its job and caught a real instance on its first run. It also
contained two instances of its own, and one of them was committed an hour
after a comment in the same file warned against that exact failure in
general terms.

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

**A third instance, 2026-10-05.** A probe summarising a WZDx feed set
`out["feed_info"] = record_shape(obj.get("road_event_feed_info"))` and `None`
otherwise. `road_event_feed_info` is what WZDx **v3** calls that object; v4
renamed it `feed_info`, every feed in the registry is v4 or later, and
Maryland's is 4.1. So the lookup found nothing, stored `"feed_info": null`,
and printed nothing at all -- and a reader of that report would conclude the
feed does not declare its own version. It declares 4.1, under a key the code
was not asking for. `null` meant "I looked under one name, which was the
wrong one", and said "there is nothing here".

Both names are now tried, v4 first, and which one answered is recorded. The
console prints a line either way, including when neither is present, because
the silence was the defect and not the absence.

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

### J. "Missing" that means "missing where I looked"

Every run of `build_dossiers.py` printed:

    2024-09-25: NO AIS FILE -- 59 candidates cannot be assessed for isolation

The file existed. `data/raw/maritime/date=2024-09-25/ais.parquet`, 434,668
rows, on disk for five days before the message was first believed. The script
looked only for the *national* day in `data/reference/ais/`, which eleven
dates have because they were fetched as GeoParquet and which this one never
had, because it arrived as a legacy CSV zip instead.

**This was the most expensive of the first sixteen**, and not because of its
size. The others produced a wrong number or a wrong sentence, and a wrong
number invites checking. This one produced a **standing, accepted
limitation**: printed every run, written into the dossier's `CAVEATS` list,
carried on 59 records, and entered in the project backlog as a task whose
stated remedy was to unzip a file that did not need unzipping. It was believed
for a week, by everyone who saw it, because a missing file is an ordinary
thing and the message was specific and confident.

A defect that manufactures a plausible limitation is more durable than one
that manufactures a wrong answer, because nobody audits a limitation. They
work around it.

The fix had to carry a caveat rather than simply switch sources. The two
artefacts are not equivalent: the clipped file stops at `AOI_SEA`, so a
candidate near the AOI edge reads as **more isolated than it is** — and
isolation feeds the strength score, so an unstated substitution would inflate
the exact quantity the project is trying to measure. The fallback is used and
declared, on every affected record, in the sentence the viewer renders.

### K. A p-value that means "no test", printed as one that means "no effect"

`boundary-bands.json` reports eight limit sets. The **200 nm EEZ** row shows
chi-square 0.0 and p 1.0000 — because AOI_SEA stops near 160 nm, so all 1,097
candidates land in the catch-all band and there is a single non-empty cell.
Both numbers are what the arithmetic must produce when there is nothing to
compare.

Beside the 12 and 24 nm rows, it reads as *tested, no effect*. It means
*never tested*. A reader scanning the table cannot tell, and the table is the
part of a study people read.

This is the only sighting so far that sits in the **results** rather than in a
script's output, a log, or a tooltip — which makes it the one most likely to
have reached a reader as a false conclusion. It is now derived from the counts
at report time, printed as NOT TESTED with the reason, and carried into the
artefact as a flag for consumers that never see the printout.

### L. A fix that does not retract the readings the broken instrument took

`check_tisb.py` read the aircraft list from `d.get("ac")`; adsb.fi returns it
under `aircraft`. The probe printed **"0 aircraft within 70 nm of DC"** every
time it was ever run — a confident negative produced by looking in the wrong
place. The bug was found, fixed and written up on 25 September.

**The conclusion it had produced was never revisited.** "Air can never have a
discrepancy layer. Six sources were tested and none returned MLAT" stood in
the plan for a week after the instrument was repaired, and shaped which
detectors were built. Eight days of collection later: 1.27% of position rows
carry a position the aircraft did not report.

Fixing an instrument does not retract the readings it took.

### M. An error message that is true, and an inference from it that is not

`HTTP 500` from BOEM was recorded as "the service is down", written into
FINDINGS, into a commit message, and said twice in conversation as "almost
certainly transient". One request settled it: `returnCountOnly=true` answers
`{"count":5746}` instantly. The server was fine. The request was asking for
every national polyline with geometry in one call, and ArcGIS answered 500
instead of a clean `exceededTransferLimit`.

A status code is a fact about a response. "The service is down" is a claim
about a cause, and it was asserted three times in writing without one cheap
request spent on testing it.

The same shape, one week earlier: a 0-byte `.git/index.lock` was correctly
diagnosed as dead and **confidently attributed to the wrong process**. It had
been created by this project's own "read-only" `git status`, which takes the
lock whatever `GIT_OPTIONAL_LOCKS` says.

### N. A guard written for a class, defeated by the next instance of it

`describe_sla` was written specifically to stop a wrong file being accepted as
the 3 nm limit line. The next day it passed **153 MB of 483 polygons**: it
checked that the JSON parsed, that there was no error object, that features
existed and that nothing was truncated. It printed `BDRY_NAME_TEXT: None` —
the identifying field absent from every feature — and still said OK.

The same week, a rule adopted after two failed fetches read *"when a fetch
fails twice with **4xx**, verify the endpoint exists"*. The next instance
arrived as a **5xx** and the rule did not fire. It had been specified by the
symptom in front of it rather than by the principle.

### O. A pooled quantity whose membership is a directory listing

`"any limit"` — the primary pooled outcome — was built from every group found
in `data/reference/limits`. **Twice in one day a file appearing in that folder
redefined it**: once by 483 polygons, once by a legitimate new line. The first
moved 548 detections into the within-2 nm band and took the headline ratio
from 0.396 to 5.938, reversing the gradient *toward the hypothesis*.

Every individually named limit still looked correct throughout.

### P. A stored measurement without the inputs that define it

`label-sample.json` records the seed, the gate, the reception filter and the
band edges. It does not record **which limit lines the distance was measured
to**. All 147 chips carry `nm_to_limit` and a stratum derived from it, and
nothing says what it is a distance to. Adding one line moved stored and
recomputed values apart by **22.9 nm**, and the only reason that was visible
is that a test happened to compare them.

### Q. Branches that do not cover the cases

The shift-null report had three branches for four combinations. `if p_shift >
alpha` came first and swallowed both "neither survives" and "the control
survives and the candidates do not", printing *"nothing here survives"* for
both. True of the candidates; silent about the control, which was the
informative half. Wrong by omission through four published runs, and
unexaminable because the branch logic lived inside a `print`.

### R. A container that reports a size it does not hold

Two in one afternoon, opposite ways round.

A `defaultdict` reported **122 distinct aircraft** while every list was empty:
`tracks[h].append(f(ts))` creates the key before evaluating the argument, and
the argument raised on every row. Keys without values.

A dict comprehension `{(k[0], k[1]): v for k, v in units.items()}` silently
dropped the day dimension: **565 units became 78**, the control group fell to
four, and the result flipped from −0.00007 to +0.00443 — from refuted to not
refuted, in the direction of the hypothesis. Values without a word.

### S. A check that is wrong in the safe direction, which is not safe

Mechanism N is a guard that fails to fire. This is its mirror: a guard that
fires on a healthy input, which is not the harmless half of the pair.

A probe flagged two ArcGIS responses as `ERROR OBJECT: the body is an error
even though the transport succeeded`. **Both bodies were real data** — five
features each, with real field names, printed four lines above the flag. An
ArcGIS `FeatureCollection` carries `exceededTransferLimit` beside `type` and
`features`: three keys, one of them in the error-key list, and the test was
"any error-ish key AND at most four keys", running *before* the body's shape
had been determined. It never had the chance to notice it was looking at a
FeatureCollection.

A second, smaller instance the same evening. The replacement flag,
`SERVER-SIDE TRUNCATION`, fired correctly on a layer that was paginated — but
the query itself had passed `resultRecordCount=5`. The truncation was
*requested*, and was reported as though the service had imposed it.

A flag is a report about a body, so `ERROR OBJECT` on a healthy body is this
document's class exactly: a routine stating an outcome its own execution did
not entitle it to state. What makes it worse than an ordinary false positive
is the shared channel. **A false alarm spends the attention the next true
alarm needs**, and the flags all print to the same place, so one that cries
wolf degrades every other check in the file.

The aggravating detail is in the source. A comment written in that same file,
the same hour, says that flagging a body which is fine trains the reader to
ignore the flags, which is how a real one gets missed. The warning was
correct, was written by the person who then committed the thing it warned
about, and did not help.

The fix is an ordering and not a longer key list — a longer list is mechanism
N's response, a guard specified by the symptom in front of it. A body that
parsed as a recognised data shape is not an error object, whatever its keys
are called, and only an unrecognised body is asked the question.

### T. A fix believed to be in place while the code ran without it

Mechanism L is a fix that lands and leaves the broken instrument's earlier
readings standing. This is L inverted: the fix never landed, and the readings
taken after it were treated as though it had.

A probe wrote its report to `probe-<date>.json`. The hazard was identified,
written up, fixed — name the file to the second, refuse to overwrite — and
tested. The edit was sent to the machine, the connection dropped mid-write,
and the next action assumed it had arrived. Three runs that afternoon wrote
one filename. The file on disk held **one** endpoint of the thirteen probed;
two runs' worth of results survived only in a chat transcript.

Two faults, needing two different fixes, and separating them is the point.
The script's default output path was unsafe — identified and fixed. And a
write was reported complete, interrupted, and then relied upon — which is a
stale *premise* rather than a stale conclusion. L leaves a belief about a
result out of date; T leaves a belief about the code out of date, and
everything the code does afterwards inherits it.

Nothing of value was lost, because a transcript happened to exist. That is
luck, and it is recorded as luck rather than as a mitigating design.

### U. A correct number under the wrong name

The purest instance in this catalogue, and the last one found.

`scripts/air_discrepancy.py` ended its read with `return ..., len(files)`.
The caller named it `n_polls`, printed "1,659 polls over 8 days", and wrote
`"n_polls": 1659` into `data/events/air-bands.json`, the published Phase F
artefact.

`len(files)` is a count of parquet partition files. The collector polls every
30 s and flushes every 5 minutes, so a file holds ten polls. Against the
heartbeat log for the same window: **19,329 polls attempted, 16,851
returned, 1,683 files.** The published figure understated the quantity it
claimed to report **by a factor of 11.7**, in public, for three days.

Nothing computed wrongly. `len(files)` is exactly the number of files, and
every arithmetic step was right. The entire defect is the name -- and the
name is the only part a reader ever sees. This document opens by saying that
almost none of these faults was an arithmetic error and that what failed was
what a program said about what it had done; this is that sentence with
nothing else attached.

It survived a pre-registration, a published artefact, a README paragraph and
twenty-two tests of the same script, because every one of those tests
asserted what the pipeline *computed* and none asserted what a field *meant*.
A mislabelled field passes every test that does not read its name.

What caught it was building a second, independent record of the same
quantity. The heartbeat log existed for an unrelated purpose -- declaring
coverage -- and the moment two records of "how much was collected" sat side
by side, one of them was obviously wrong. The comparison is now a function,
`file_poll_agreement`, which compares files against polls through the known
flush ratio and returns a sentence when they disagree, with a tolerance band
so it does not fire on an ordinary run.

The estimator never used the field. It was descriptive, and wrong by an order
of magnitude, which is the kind of error that costs no result and all of the
credibility.

### V. A fix that makes a wrong answer unreachable instead of correcting it

Mechanism S was a guard that called two valid ArcGIS responses errors. The
fix was an ordering: ask "is this an error object" only of a body that did
not parse as a recognised data shape. That was the right fix and it worked.

**It did not fix the function.** `looks_like_error_object` still opened with
an early return on `exceededTransferLimit`, so the predicate still answered
True for a paginated FeatureCollection. The wrong answer had been made
unreachable through one caller, which is not the same as being right, and
the next caller gets it.

Every check written that afternoon went through `summarise`, so every check
agreed. It surfaced the moment a test asked the predicate directly, against
a recorded ArcGIS body -- and the test that found it was written to guard
mechanism S, four hours after S was declared fixed.

The shape generalises past this instance. A defect masked by its caller is
strictly worse than one in the open: it passes its tests, it reads as
resolved in the record, and the next use of the function re-opens it with no
warning and no memory of why. **A fix is a correct answer, not an
unreachable wrong one.**

### W. A summary of a source, used where the source was available

Four instances in one day, and they were not independent.

**"22 keyless feeds."** The WZDx registry has 43 rows. A model's summary of
the registry page said 21 feeds require keys, so 22 was published -- in the
phase plan, the status note, the backlog and a commit message. The CSV says
`needAPIKey` is "false" for **30** and "true" for 13. Nobody opened the file,
which was one request away and is now a committed fixture.

**"Maryland DOT (mdot)."** Read as an organisation called `mdot`, and
matched on `issuingOrganization`. The registry has `issuingOrganization`
"Maryland DOT" and `feedName` "mdot": two columns, presented as one
parenthetical by a summariser, and matched on the wrong one. The test failed
with zero rows.

**"Byte-identical."** Two CHART responses to different `Accept` headers were
reported as byte-identical, on the evidence that both were 39,772 bytes.
They differ in 56 characters, inside a Cloudflare `email-protection` href
whose XOR key rotates per response -- which is exactly why the lengths
matched. The conclusion drawn, that these endpoints do not
content-negotiate, was right. The evidence given for it was a proxy that
happened to agree, which is more dangerous than being wrong outright because
it reads as verified.

**The CHART feed URLs themselves.** Three endpoints probed as data feeds,
all answering HTML, and the URLs came from a summary of a page rather than
from any documented feed list.

The common root is not carelessness about sources. It is that **a summary
arrives already shaped like data** -- a count, a name in parentheses, a
length -- and a shaped thing invites use. The authoritative source was one
command away every time, and in three of the four cases it was later fetched
and contradicted the summary.

The rule that follows is narrow on purpose: a summary is a pointer to a
source, never a substitute for it, and any number or identifier that will be
published is read from the source and committed as a fixture.

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

**An exact zero is instrumented, not believed.** "0 cells recovered" was the
output of a check that had failed on 100% of rows. The habit is cheap: when a
number comes back exactly zero, add counters to every stage before writing it
down.

**Two implementations of one specification, compared.** The scratch
computation and the committed script disagreed, and the committed one was
wrong in the favourable direction. This only worked because the first result
was written down *before* the second was built. An implementation checked
against nothing is checked against the author's intentions.

**Reading all of a record's keys, not the one expected.** A query for
`p_shift_control` on an artefact that stores `p_control_shift` returned
`None`, which is indistinguishable from the value being absent, and was one
sentence from becoming a reported defect.

**Asking which way an amendment cuts.** Excluding TIS-B removed the large,
eye-catching values and left a series with no step at all. An amendment that
makes a positive result *less* likely is one that can be trusted; the reverse
needs an argument.

**Stating a prediction before running.** The scattered null was predicted to
reject and did not. The prediction being wrong is what exposed that the
maritime intuition did not transfer to a difference-of-differences statistic.

**A flag that contradicts the data printed beside it.** `ERROR OBJECT`
appeared four lines below five parsed features with real field names. Nothing
automated caught this and nothing could have: the fixtures were written from
what the author expected servers to send, so the test suite agreed with the
bug. Two adjacent lines of output that cannot both be true remain the only
check that covers what the author did not imagine.

**Verifying that a change is present before acting as though it is.** A line
count and a string match against the file on the machine that will run it.
Cheap, and it is the check that was skipped in mechanism T.

**Calling a predicate directly, not only through its caller.** Mechanism V
hid behind `summarise` for four hours and every test went through
`summarise`. A pure function that decides something gets asked the question
on its own, against a real input.

**Fixtures that are recordings.** `scripts/probe_road_feeds.py --save-bodies`
writes each response verbatim with a manifest carrying its URL, status,
content type, byte counts and SHA-256, and a test asserts every file still
hashes to what the server sent -- because the most tempting repair in a
fixture directory is changing a byte so a test passes. Nobody hand-writing an
ArcGIS fixture includes `exceededTransferLimit`, which is the whole reason S
survived twenty-two tests. On its first run against recordings the suite
found V, the F instance and two of W.

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
8. **Fixing an instrument does not retract its readings.** When a probe is
   found to have been broken, every conclusion it produced is revisited —
   explicitly, as a listed task — not left standing because the bug is closed.
9. **A status code is a fact; its cause is an inference.** No claim about why
   a request failed is written into a finding, a commit or a plan until one
   cheap request has tested it. For a REST service that is a count or metadata
   query, which separates "server down" from "my request is too big" in
   milliseconds.
10. **A rule is specified by its principle, not by the symptom in front of
    it.** A rule naming `4xx` does not fire on `5xx`. Write the reason, not
    the instance.
11. **A pooled quantity declares its membership in code and records it in the
    artefact.** Never inferred from which files happen to be in a directory.
12. **An artefact storing a derived measurement records the inputs that define
    it**, in the same file. A distance records what it is a distance to.
13. **A line of output that states a conclusion is a named function**, tested
    for every combination of its inputs, with one test asserting that N
    distinct cases produce N distinct sentences. The count is what catches a
    missing branch; a swallowed case looks correct in isolation.
14. **An exact zero is instrumented before it is believed.**
15. **A predicted count is a delta against a baseline that was measured**, not
    against earlier arithmetic. Three chained predictions off one real
    measurement produced a confident wrong number.

16. **A check that fires on a healthy input is a defect of the same class as
    one that misses a fault.** A flag's currency is the reader's attention,
    and a false alarm spends what the next true alarm will need; the flags
    share one channel, so one that cries wolf degrades the rest. A guard runs
    only after the shape of its input is known, and never on a condition the
    caller created.
17. **A fix is in place when the running code shows it, not when the edit is
    sent.** Before any action that assumes a change landed, verify it on the
    machine that will run it — a line count, a string match, a version
    banner. A dropped connection reports nothing and is indistinguishable
    from success.

18. **A field is named for what it holds.** Not for what the caller wanted,
    not for what it is usually near. And **where two records of one quantity
    exist, the code compares them** -- through whatever constant relates
    them -- and says so when they disagree, with a tolerance band so the
    check does not become a false alarm. Two independent records of the same
    thing are the cheapest audit available, and the only reason a field
    called `n_polls` holding a file count went three days in public is that
    nothing ever put the two counts next to each other.

19. **A fix is a correct answer, not an unreachable wrong one.** When a
    guard is repaired by changing where it runs, the guard itself is checked
    too, and the predicate is tested directly rather than only through the
    caller that was fixed.
20. **A summary is a pointer to a source, never a substitute for it.** Any
    number, field name or identifier that will be published is read from the
    source and committed as a fixture. A summary arrives already shaped like
    data, which is what makes it usable and what makes it dangerous.

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

The defence is not that no mistakes were made. **Thirty-three** were found,
one of them published and withdrawn. The defence is that **the mistakes were
found by the project's own machinery, they were recorded rather than tidied
away, and the result did not depend on any of them.** Every correction moved
the headline number in the direction that would have helped the hypothesis,
and it still did not arrive.

**And the second half of the catalogue is the stronger evidence, because it
is worse.** Mechanisms L to T were all committed *after* this document
existed, by someone who had read it that week and could recite the class on
request. One of them — 483 polygons accepted as a legal boundary line —
produced a fifteen-fold rise in the headline ratio and reversed its gradient,
in the direction of the hypothesis, and was caught by a comparability check
that reported it as "not comparable" rather than as a corrupted input. It was
hours from being published.

S and T close the argument rather than weakening it. Both were committed
inside a script written for no other purpose than to catch this class of
defect before it could reach an analysis — and that script did catch a real
one on its first run, a count field served as a string, found before any
arithmetic touched it. It also carried two of its own. One of them was
committed an hour after a comment in the same file warned, in general terms,
against exactly that failure. **The strongest available statement is
therefore not that the author learned to avoid the class.** It is that a
probe written against the class finds instances in other people's data and in
its own, and that the record of both is in the same file.

That is the honest shape of the claim. Not *these mistakes were prevented* —
they were not, and knowing the failure mode in detail did not prevent them —
but *the machinery caught them, every time, and the record of catching them
is public.* A second domain was built on the same machinery and returned the
same answer. The argument for believing the result is the catalogue, not the
absence of one.

*A note on the numbers in this document.* Every figure above was re-derived
from the artefacts by a script, not copied from the worklog, and one of them
was wrong when it was: the `>10nm` keep-fraction is quoted here as 0.59,
which was true on the day the drift was found and became 0.5975 after the
next label revision. Quoting a measured value without saying which run it
came from is mechanism **D** in this document's own catalogue, committed
inside the document about mechanism D. It is corrected above rather than
silently, because that is rule 7.
