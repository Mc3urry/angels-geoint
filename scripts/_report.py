"""Let a script write its own report, so the report cannot outlive its run.

`data/events/corrected/` held three `*-report.txt` files captured by hand.
On 2026-09-26 the six geojson artefacts beside them were regenerated and the
reports were not: `gate-report.txt` still announced "134 usable labels
(93 vessel); 13 ambiguous excluded" from the pre-pass-3 label set, sitting in
the same directory as artefacts built from 127 labels and 86 vessels, with
nothing in either file saying which run it belonged to.

That is the project's recurring defect wearing its plainest costume -- a file
that reports something other than what happened, and cannot tell you so. A
report the script writes at the end of its own run carries the command and
the timestamp that produced it and cannot drift from it.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import sys
from pathlib import Path


@contextlib.contextmanager
def tee(path: Path | str):
    """Print as usual, and keep a copy for `path`, stamped with the command."""
    out = Path(path)
    buf = io.StringIO()
    real = sys.stdout

    class _Tee:
        def write(self, s: str) -> int:
            real.write(s)
            buf.write(s)
            return len(s)

        def flush(self) -> None:
            real.flush()

        def isatty(self) -> bool:
            return getattr(real, "isatty", lambda: False)()

    try:
        with contextlib.redirect_stdout(_Tee()):
            yield
    finally:
        stamp = (datetime.datetime.now(datetime.timezone.utc)
                 .replace(microsecond=0).isoformat())
        out.parent.mkdir(parents=True, exist_ok=True)
        # Written even when the run raised: a report that exists only for
        # successful runs is a report that hides the failures.
        out.write_text(
            f"# {Path(sys.argv[0]).name} at {stamp}\n"
            f"# command: {' '.join(sys.argv)}\n"
            f"#\n"
            f"# Written by the run itself. If this file and the artefacts\n"
            f"# beside it disagree, the artefacts are newer -- re-run.\n\n"
            + buf.getvalue(), encoding="utf-8")
