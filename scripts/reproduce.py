"""Can someone else get this result? Check, run, and verify against the record.

    python scripts/reproduce.py              # inventory, then run the cheap stages
    python scripts/reproduce.py --check      # inventory only, run nothing
    python scripts/reproduce.py --all        # also re-run the boundary nulls (slow)

WHAT REPRODUCIBLE MEANS HERE, AND WHAT IT DOES NOT

It does not mean "the code is on GitHub". It means someone who clones this
repo can regenerate the numbers in `FINDINGS.md` and check that they come
out the same. Those are different claims, and only the second one is worth
anything to an advisor or a reviewer.

So this script does three things, in order:

    inventory   for every stage, list the inputs it needs and say which are
                present, which are fetchable, and which exist only inside
                56 GB of raw Sentinel-1 scenes that will never be in a repo.
    run         execute the stages whose inputs are all present.
    VERIFY      compare what was just regenerated against what is committed.

The third is the part that makes this more than a build script. A pipeline
that runs is not a pipeline that reproduces. Every stochastic step in this
project is seeded -- `boundary_analysis.py` at 20240621, the sampler and the
correction at 20260925 -- so a correct re-run must produce byte-identical
numbers, and any difference is drift worth knowing about.

NOTHING HERE OVERWRITES THE RECORD

Output goes to `data/events/reproduce/`. The committed artefacts are the
thing being checked against; a verifier that clobbers its own reference is
not a verifier.

WHAT A FRESH CLONE ACTUALLY GETS, TODAY

Four data files, 974 KB: the labels, the draw that produced them, the
published band result, and the scored candidate set. That is enough for the
classifier and, once `fetch_limits.py` has run, for the clutter correction
and the gate analysis. It is NOT enough for `boundary_analysis.py`, which
needs the per-scene searched grids in `sar-*.geojson` (15 MB) and the
matched/unmatched split in `dark-*.geojson` (6 MB), nor for the dossiers'
AIS pass (3.2 GB of reference parquet) or the image chips (raw scenes).

This script prints that gap rather than papering over it, and the gap is the
argument for whichever of those files is worth tracking.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS, INTERIM, REFERENCE, ROOT

OUT = EVENTS / "reproduce"

# Where a missing input has to come from. "fetch" means a script in this repo
# can get it; "raw" means it is derived from Sentinel-1 scenes and a clone
# will not have it.
SOURCES = {
    "fetch": "run scripts/fetch_limits.py",
    "raw": "derived from the raw scenes; not reproducible from a clone",
    "track": "small and derived -- a candidate for version control",
    "ml": 'pip install -e ".[ml]"',
}


def _glob(pattern: str) -> list[Path]:
    base, _, pat = pattern.rpartition("/")
    return sorted((EVENTS if base == "events" else ROOT / base).glob(pat))


STAGES = [
    {
        "name": "limits",
        "why": "the legal lines every spatial step measures against",
        "needs": [(REFERENCE / "limits", "fetch")],
        "cmd": None,
        "note": "not run automatically: it reaches the network",
    },
    {
        "name": "classifier",
        "why": "P(vessel) per candidate, from the labels",
        "needs": [(EVENTS / "candidates-scored.geojson", "track"),
                  (EVENTS / "labels.jsonl", "track")],
        "cmd": ["scripts/classify_candidates.py", "--out",
                str(OUT / "candidate-pvessel.json")],
        "soft": ["sklearn"],
    },
    {
        "name": "clutter correction",
        "why": "the headline check: does the null survive the contamination",
        "needs": [(EVENTS / "candidates-scored.geojson", "track"),
                  (EVENTS / "labels.jsonl", "track"),
                  (EVENTS / "boundary-bands.json", "track"),
                  (REFERENCE / "limits", "fetch")],
        "cmd": ["scripts/correct_clutter.py", "--out-dir", str(OUT / "corrected"),
                "--write-replicates", "0"],
    },
    {
        "name": "gate",
        "why": "what the detection gate costs, measured against the labels",
        "needs": [(EVENTS / "candidates-scored.geojson", "track"),
                  (EVENTS / "labels.jsonl", "track"),
                  (REFERENCE / "limits", "fetch")],
        "cmd": ["scripts/tune_gate.py"],
    },
    {
        "name": "boundary analysis",
        "why": "the published result itself",
        "needs": [(EVENTS / "candidates-scored.geojson", "track"),
                  (REFERENCE / "limits", "fetch"),
                  ("events/sar-*.geojson", "raw"),
                  ("events/dark-*.geojson", "raw")],
        "cmd": None,            # built from the committed settings; see below
        "slow": True,
        "verify": "boundary",
    },
    {
        "name": "dossiers",
        "why": "one record per unexplained detection",
        "needs": [(EVENTS / "candidates-scored.geojson", "track"),
                  (EVENTS / "labels.jsonl", "track"),
                  (EVENTS / "persistent-sites.geojson", "raw"),
                  ("events/sar-*.geojson", "raw"),
                  ("events/dark-*.geojson", "raw"),
                  (REFERENCE / "limits", "fetch")],
        "cmd": ["scripts/build_dossiers.py", "--no-ais", "--out",
                str(OUT / "dossiers.json")],
        "optional": [(REFERENCE / "ais", "raw"),
                     (INTERIM / "candidates", "raw")],
    },
]


def _dur(secs: float) -> str:
    if secs < 90:
        return f"{secs:.1f} s"
    m, sec = divmod(int(secs), 60)
    return f"{m}m {sec:02d}s"


def _importable(module: str) -> bool:
    """Is the package there, without importing it for real.

    A stage that needs scikit-learn should say so in the inventory, not run
    and fail. The first version declared `soft: ["sklearn"]` and then never
    consulted it, so a missing optional extra came out as a bare FAILED --
    indistinguishable from a broken pipeline.
    """
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def present(item) -> bool:
    if isinstance(item, Path):
        return item.exists() and (not item.is_dir() or any(item.iterdir()))
    return bool(_glob(item))


def label(item) -> str:
    if isinstance(item, Path):
        try:
            return str(item.relative_to(ROOT))
        except ValueError:
            return str(item)
    return item.replace("events/", "data/events/")


def boundary_cmd() -> list[str]:
    """Re-run the boundary analysis the way the committed file was made.

    Hardcoding the defaults was wrong, and the first real run showed it: the
    published result was produced with `--near-nm 10`, which adds the two
    local step-test entries -- the test FINDINGS.md leans on hardest. Without
    the flag those keys were simply absent from the rebuild, and the verifier
    could only say "present in only one". Six limits matched to the digit and
    the one that mattered most was not checked at all.

    So the command is derived from the artefact instead of assumed. Newer
    files record `near_nm` outright; for the one committed on 2026-09-25 it is
    read back out of the limit names, which embed it as "(within 10 nm)".
    """
    cmd = ["scripts/boundary_analysis.py", "--out",
           str(OUT / "boundary-bands.json")]
    ref = EVENTS / "boundary-bands.json"
    if not ref.exists():
        return cmd
    try:
        doc = json.loads(ref.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cmd
    near = doc.get("near_nm")
    if near is None:
        found = {m.group(1) for k in doc.get("limits", {})
                 if (m := re.search(r"\(within ([\d.]+) nm\)", k))}
        near = float(found.pop()) if len(found) == 1 else None
    if near is not None:
        cmd += ["--near-nm", f"{near:g}"]
    for flag, key in (("--trials", "trials"), ("--stride", "stride"),
                      ("--shift-trials", "shift_trials")):
        if doc.get(key) is not None:
            cmd += [flag, str(doc[key])]
    gate = doc.get("gate") or {}
    if gate.get("min_snr") is not None:
        cmd += ["--min-snr", str(gate["min_snr"])]
    if gate.get("min_pixels") is not None:
        cmd += ["--min-pixels", str(gate["min_pixels"])]
    if doc.get("reception_kept"):
        cmd += ["--reception", ",".join(doc["reception_kept"])]
    return cmd


def verify_boundary(made: Path) -> tuple[list[str], list[str]]:
    """Regenerated band numbers against the committed ones, exactly.

    Every null in `boundary_analysis.py` is seeded, so a correct re-run is
    byte-identical. Comparing only the headline would hide drift in the
    bands that produced it, so this walks every limit and every band.
    """
    ref = EVENTS / "boundary-bands.json"
    if not ref.exists() or not made.exists():
        return (["one side missing; nothing to compare"], [])
    ra = json.loads(ref.read_text(encoding="utf-8"))
    rb = json.loads(made.read_text(encoding="utf-8"))

    # A run at different settings is not drift, and reporting it as drift
    # would punish the obvious thing to do first -- a short run to see how
    # long a long one takes. The nulls are Monte Carlo: fewer trials means
    # different p-values by construction.
    keys = ("trials", "shift_trials", "stride", "near_nm", "gate",
            "reception_kept", "candidate_file")
    # A field the COMMITTED file never recorded is not a disagreement. The
    # 2026-09-25 artefact predates `near_nm` and `shift_trials`, and treating
    # their absence as a mismatch would flag every faithful re-run from now
    # on -- the same false alarm as comparing a short run's p-values, in the
    # other direction.
    unrecorded = [k for k in keys if k not in ra and k in rb]
    settings = {k: (ra[k], rb.get(k)) for k in keys if k in ra}
    differ = {k: v for k, v in settings.items() if v[0] != v[1]}
    note = ([f"(the committed file predates {', '.join(unrecorded)}; "
             f"not compared)"] if unrecorded else [])
    if differ:
        out = ["NOT COMPARABLE -- the two runs used different settings:"]
        out += [f"  {k}: committed {x!r} vs rebuilt {y!r}"
                for k, (x, y) in differ.items()]
        out.append("  Band counts below are still meaningful; p-values are "
                   "not. Re-run at the committed settings to verify.")
        if any(k in differ for k in ("trials", "stride", "shift_trials")):
            return (out + _band_diffs(ra["limits"], rb["limits"],
                                      deterministic_only=True), note)
        return (out, note)
    return (_band_diffs(ra["limits"], rb["limits"]), note)


def _band_diffs(a: dict, b: dict, *, deterministic_only: bool = False) -> list[str]:
    fields = ("candidates_by_band", "expected_by_band", "ais_seen_by_band")
    if not deterministic_only:
        fields += ("chi2", "p_scattered", "p_shift", "chi2_control",
                   "p_control_shift")
    out = []
    for name in sorted(set(a) | set(b)):
        if name not in a or name not in b:
            out.append(f"{name}: present in only one")
            continue
        for field in fields:
            x, y = a[name].get(field), b[name].get(field)
            if x != y:
                out.append(f"{name} / {field}: committed {x!r} -> rebuilt {y!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="inventory only")
    ap.add_argument("--all", action="store_true",
                    help="also run the slow stages")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    print("\n  INVENTORY\n")
    runnable: list[dict] = []
    ready: list[str] = []
    blocked: list[str] = []
    needs_dep: list[str] = []
    # Build the lazy commands BEFORE deciding which stages have none. The
    # first version read this list while the boundary stage's command was
    # still unbuilt, and announced the most expensive stage in the pipeline
    # as "no command here -- it reaches the network". It has never touched
    # the network.
    for st in STAGES:
        if st.get("verify") == "boundary" and st.get("cmd") is None:
            st["cmd"] = boundary_cmd()
    no_cmd = [st["name"] for st in STAGES if not st.get("cmd")]
    for st in STAGES:
        missing = [(i, src) for i, src in st["needs"] if not present(i)]
        ok = not missing
        mark = "ready" if ok else "blocked"
        print(f"    {st['name']:20} {mark:8} {st['why']}")
        for i, src in missing:
            print(f"      missing  {label(i):42} {SOURCES[src]}")
        for i, src in st.get("optional", []):
            if not present(i):
                print(f"      degraded {label(i):42} {SOURCES[src]}")
        soft = [m for m in st.get("soft", []) if not _importable(m)]
        for m in soft:
            print(f"      needs     {m:42} {SOURCES['ml']}")
        if ok and not soft:
            ready.append(st["name"])
            if st.get("cmd"):
                runnable.append(st)
        elif soft:
            needs_dep.append(st["name"])
        else:
            blocked.append(st["name"])

    line = f"\n    {len(ready)} of {len(STAGES)} stages are runnable"
    if blocked:
        line += "; blocked on inputs: " + ", ".join(blocked)
    if needs_dep:
        line += "; blocked on a package: " + ", ".join(needs_dep)
    print(line)
    if no_cmd:
        print(f"    {', '.join(no_cmd)}: no command here -- it reaches the "
              f"network, so run it yourself")
    if args.check:
        print()
        return 0

    print("\n  RUN\n")
    ran, failed, skipped = [], [], []
    timings: list[tuple[str, float]] = []
    for st in runnable:
        if st.get("slow") and not args.all:
            print(f"    {st['name']:20} skipped   (slow; pass --all)")
            skipped.append(st["name"])
            continue
        cmd = [sys.executable, *st["cmd"]]
        print(f"    {st['name']:20} running   {' '.join(st['cmd'][:1])}")
        t0 = time.monotonic()
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        timings.append((st["name"], time.monotonic() - t0))
        if r.returncode == 0:
            ran.append(st)
            print(f"    {'':20} ok        {_dur(timings[-1][1])}")
        else:
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:] or \
                ["(no output)"]
            failed.append((st["name"], tail))
            print(f"    {'':20} FAILED")
            for line in tail:
                print(f"    {'':22} {line[:110]}")

    print("\n  VERIFY\n")
    checked = False
    for st in ran:
        if st.get("verify") != "boundary":
            continue
        checked = True
        diffs, notes = verify_boundary(OUT / "boundary-bands.json")
        if not diffs:
            print("    boundary analysis   IDENTICAL to the committed result")
            print("    every band, every limit, every p-value. The seeds hold.")
        else:
            print(f"    boundary analysis   {len(diffs)} DIFFERENCE(S):")
            for d in diffs[:12]:
                print(f"      {d}")
        for n in notes:
            print(f"      note: {n}")
    if not checked:
        why = ("it was skipped as slow -- re-run with --all"
               if "boundary analysis" in skipped else
               "its inputs are not here: the per-scene sar-* and dark-* files")
        print(f"    nothing to verify: the boundary stage did not run, "
              f"because\n    {why}.")
        print("    It is the only stage with a committed reference to check")
        print("    against, so until it runs, 'ran 4, failed 0' means the")
        print("    pipeline executed -- not that it reproduced anything.")

    print("\n  SUMMARY\n")
    print(f"    ran {len(ran)}, skipped {len(skipped)}, failed {len(failed)}, "
          f"blocked {len(blocked)}"
          + (f", in {_dur(sum(x for _, x in timings))}" if timings else ""))
    for name, secs in timings:
        print(f"      {name:22}{_dur(secs):>10}")
    for name, err in failed:
        for line in err:
            print(f"      {name}: {line[:110]}")
    print(f"\n    output in {OUT.relative_to(ROOT)} -- nothing committed was "
          f"overwritten\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
