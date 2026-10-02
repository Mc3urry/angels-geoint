"""Draw a limit's bands from boundary-bands.json. No hand-made figures.

    python scripts/plot_bands.py
    python scripts/plot_bands.py --limit "3 nm state seaward limit"

WHY THIS EXISTS

`docs/boundary-step-12nm.svg` was drawn by hand on 24 September from numbers
that moved on 30 September and again on 2 October. A figure with no script
behind it cannot tell you it has gone stale, and a reader cannot tell either:
it is a picture of numbers, indistinguishable from a picture of the right
numbers. Every figure in this repository is now generated from the committed
artefact, so regenerating the result regenerates the figure.

WHAT IT DRAWS

Observed over expected, per distance band, for the candidates and for the
AIS-explained control on the same axes. Parity is 1.0: above it there are
more returns than searched water predicts, below it fewer. Marker area is
proportional to the number of detections, because a band holding four
returns and a band holding five hundred should not read alike.

The control is the point of the figure, not decoration. The candidates'
numbers alone cannot distinguish "no effect" from "no power"; the control
measured on the same bands and the same searched-water denominator can.

Stdlib only -- no matplotlib, for the same reason limits.py reads a
shapefile without geopandas.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS

# Validated 2026-10-02 with the data-viz palette validator, categorical
# slots 1 and 2, all-pairs, both modes: every check PASS. Worst pair CVD
# dE 24.7 light / 26.8 dark against a >= 8 target; normal-vision 33.6 / 31.8
# against a >= 15 floor; both >= 3:1 on their surface.
LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e",
         "grid": "#d8d7d2", "cand": "#2a78d6", "ctl": "#eb6834"}
DARK = {"surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7",
        "grid": "#3a3a38", "cand": "#3987e5", "ctl": "#d95926"}

W, H = 760, 400
L, R, T, B = 62, 176, 46, 56


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def ratios(entry: dict) -> tuple[list[str], list, list]:
    """(band names, candidate points, control points). A point is
    (obs/exp, count) or None where expected is zero -- a ratio with a zero
    denominator is not plotted as anything, least of all as zero."""
    bands = list(entry["candidates_by_band"])

    def series(obs_key: str, exp_key: str):
        obs, exp = entry[obs_key], entry[exp_key]
        out = []
        for b in bands:
            e = exp.get(b) or 0.0
            out.append(None if e <= 0 else (obs.get(b, 0) / e, obs.get(b, 0)))
        return out

    return (bands,
            series("candidates_by_band", "expected_by_band"),
            series("ais_seen_by_band", "ais_seen_expected"))


def svg(limit: str, entry: dict, meta: dict) -> str:
    bands, cand, ctl = ratios(limit_entry := entry)
    pts = [p for p in cand + ctl if p]
    if not pts:
        raise SystemExit(f"  nothing plottable for {limit!r}")
    top = max(1.25, math.ceil(max(v for v, _ in pts) * 4) / 4 + 0.1)
    big = max(n for _, n in pts) or 1

    def x(i: int) -> float:
        return L + (W - L - R) * (i + 0.5) / len(bands)

    def y(v: float) -> float:
        return H - B - (H - B - T) * (v / top)

    def rad(n: int) -> float:
        return 4.0 + 11.0 * math.sqrt(n / big)       # area proportional

    o: list[str] = []
    o.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'width="{W}" height="{H}" role="img" '
             f'aria-labelledby="ttl desc">')
    o.append(f'<title id="ttl">{esc(limit)}: observed over expected by '
             f'distance band</title>')
    o.append(f'<desc id="desc">Dot plot. Parity is 1.0. Candidates and the '
             f'AIS-explained control on the same bands and the same '
             f'searched-water denominator. Marker area is proportional to '
             f'detection count. The figures are in the table beside this '
             f'image.</desc>')
    # Both dark scopes, so an OS setting and a theme toggle both work.
    o.append("<style>\n"
             "  .s{fill:%(surface)s}.ink{fill:%(ink)s}.ink2{fill:%(ink2)s}\n"
             "  .gr{stroke:%(grid)s}.cd{fill:%(cand)s}.ct{fill:%(ctl)s}\n"
             "  .cds{stroke:%(cand)s}.cts{stroke:%(ctl)s}.ring{stroke:%(surface)s}\n"
             % LIGHT +
             "  @media (prefers-color-scheme:dark){\n"
             "   .s{fill:%(surface)s}.ink{fill:%(ink)s}.ink2{fill:%(ink2)s}\n"
             "   .gr{stroke:%(grid)s}.cd{fill:%(cand)s}.ct{fill:%(ctl)s}\n"
             "   .cds{stroke:%(cand)s}.cts{stroke:%(ctl)s}\n"
             "   .ring{stroke:%(surface)s}}\n" % DARK +
             "  text{font:13px system-ui,-apple-system,Segoe UI,sans-serif}\n"
             "  .sm{font-size:11px}.bd{font-weight:600}\n"
             "</style>")
    o.append(f'<rect class="s" width="{W}" height="{H}"/>')

    # y grid, recessive, and the parity line carrying the only emphasis
    v = 0.0
    while v <= top + 1e-9:
        yy = y(v)
        parity = abs(v - 1.0) < 1e-9
        dash = "" if parity else 'stroke-dasharray="2 4" '
        wide = 2 if parity else 1
        fade = 0.9 if parity else 0.45
        o.append(f'<line class="gr" x1="{L}" x2="{W - R + 10}" y1="{yy:.1f}" '
                 f'y2="{yy:.1f}" stroke-width="{wide}" {dash}'
                 f'opacity="{fade}"/>')
        o.append(f'<text class="ink2 sm" x="{L - 10}" y="{yy + 4:.1f}" '
                 f'text-anchor="end">{v:.2f}</text>')
        v += 0.25
    o.append(f'<text class="ink2 sm" x="{L + 6}" y="{y(1.0) - 7:.1f}">'
             f'parity</text>')
    ymid = (T + H - B) / 2
    o.append(f'<text class="ink2 sm" transform="rotate(-90 18 {ymid:.0f})" '
             f'x="18" y="{ymid:.0f}" text-anchor="middle">observed / '
             f'expected</text>')

    for i, b in enumerate(bands):
        o.append(f'<text class="ink2 sm" x="{x(i):.1f}" y="{H - B + 20}" '
                 f'text-anchor="middle">{esc(b)}</text>')

    # control first so the candidates, the subject, sit on top
    for cls, ring, series in (("ct", "cts", ctl), ("cd", "cds", cand)):
        pairs = [(i, p) for i, p in enumerate(series) if p]
        d = " ".join(f'{"M" if k == 0 else "L"}{x(i):.1f},{y(p[0]):.1f}'
                     for k, (i, p) in enumerate(pairs))
        o.append(f'<path class="{ring}" d="{d}" fill="none" stroke-width="2" '
                 f'opacity="0.55"/>')
        for i, (val, n) in pairs:
            o.append(f'<circle class="{cls} ring" cx="{x(i):.1f}" '
                     f'cy="{y(val):.1f}" r="{rad(n):.1f}" '
                     f'stroke-width="2"/>')

    # legend AND direct labels: identity never rests on colour alone
    rows = [(cls, label, y([p for p in series if p][-1][0]))
            for cls, label, series in (("cd", "candidates", cand),
                                       ("ct", "AIS-seen control", ctl))]
    rows.sort(key=lambda r: r[2])
    for k in range(1, len(rows)):
        gap = rows[k][2] - rows[k - 1][2]
        if gap < 18:                       # 11px line box plus breathing room
            rows[k] = (rows[k][0], rows[k][1], rows[k - 1][2] + 18)
    for cls, label, yy in rows:
        o.append(f'<circle class="{cls}" cx="{W - R + 20}" cy="{yy:.1f}" '
                 f'r="5"/>')
        o.append(f'<text class="ink sm" x="{W - R + 32}" y="{yy + 4:.1f}">'
                 f'{esc(label)}</text>')

    o.append(f'<text class="ink bd" x="{L - 52}" y="{T - 22}">'
             f'{esc(limit)}</text>')
    sub = (f'shift p {entry["p_shift"]:.4f} candidates, '
           f'{entry["p_control_shift"]:.4f} control  ·  '
           f'{meta["trials"]:,} scattered / {meta["shift_trials"]:,} shift '
           f'trials  ·  marker area = detections')
    o.append(f'<text class="ink2 sm" x="{L - 52}" y="{T - 6}">{esc(sub)}'
             f'</text>')
    o.append("</svg>")
    return "\n".join(o)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bands", type=Path,
                    default=EVENTS / "boundary-bands.json")
    ap.add_argument("--limit", default="12 nm territorial sea")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if not args.bands.exists():
        print(f"\n  No {args.bands}. Run scripts/boundary_analysis.py.\n")
        return 1
    doc = json.loads(args.bands.read_text(encoding="utf-8"))
    entry = doc.get("limits", {}).get(args.limit)
    if entry is None:
        print(f"\n  {args.limit!r} is not in {args.bands.name}. Present: "
              f"{', '.join(sorted(doc.get('limits', {})))}\n")
        return 1
    if not entry.get("testable", True):
        print(f"\n  {args.limit!r} is marked NOT TESTED "
              f"({entry.get('untestable_because')}).")
        print("  Refusing to draw it: a figure of p = 1.0000 by construction "
              "reads as\n  a measured null and is not one.\n")
        return 1

    out = args.out or (Path("docs") /
                       (args.limit.split()[0] + "nm-bands.svg"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg(args.limit, entry, doc), encoding="utf-8")
    print(f"\n  {args.limit}")
    print(f"    candidates  shift p {entry['p_shift']:.4f}")
    print(f"    control     shift p {entry['p_control_shift']:.4f}")
    print(f"  wrote {out}  ({out.stat().st_size / 1024:.1f} KB)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
