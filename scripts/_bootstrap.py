"""Re-run under the interpreter that has ANGELS installed, if this is not it.

Imported first by every script in this directory:

    import _bootstrap  # noqa: F401
    from angels.config import ...

WHY

`python scripts/inspect_scene.py` runs under whatever `python` means on PATH,
and on a machine with several Python installations that is not a decision
anyone made -- it is a side effect of install order. On 15 September 2026 a
conda install put a Python 3.14 into ArcGIS Pro's conda root, which sits first
on PATH, and every script in this repo began failing with:

    ModuleNotFoundError: No module named 'angels'

which reads as a broken project and is actually a correct project being handed
to the wrong interpreter. tasks.ps1 and collector.ps1 were taught to resolve
the right one, but that only helps commands routed through them; a script run
directly still got whatever PATH offered.

So the scripts now fix it themselves. If the current interpreter cannot import
the project, this finds one that can and re-runs the same command under it.

WHAT IT DELIBERATELY DOES NOT DO

It never installs anything, never modifies PATH, and never silently continues
under a broken interpreter. If no candidate works, the original ImportError is
raised with a message naming what was tried -- because the one thing worse
than the wrong interpreter is not knowing which interpreter you got.

It also announces the switch on stderr. A script that quietly relaunches
itself elsewhere would be its own kind of invisible behaviour, and this file
exists to make an invisible thing visible.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Set on the child so a failed re-run cannot re-run again, forever.
SENTINEL = "ANGELS_BOOTSTRAPPED"

_WIN = os.name == "nt"
_BIN = "Scripts/python.exe" if _WIN else "bin/python"
_EXE = "python.exe" if _WIN else "bin/python"


def candidates() -> list[Path]:
    """Interpreters that might have the project, best first.

    An explicit override wins; then a venv inside the repo; then the
    conventional conda environment locations. PATH is NOT included -- it is
    where we already are, and it is what failed.
    """
    out: list[Path] = []

    override = os.environ.get("ANGELS_PYTHON")
    if override:
        out.append(Path(override))

    root = Path(__file__).resolve().parents[1]
    out.append(root / ".venv" / _BIN)

    home = Path.home()
    for env in ("envs/angels", "miniforge3/envs/angels",
                "miniconda3/envs/angels", "anaconda3/envs/angels"):
        out.append(home / env / _EXE)

    return out


def _same(a: Path, b: Path) -> bool:
    """Are these the same interpreter -- WITHOUT resolving symlinks?

    Resolving is the obvious implementation and is wrong here. A venv's
    python is a symlink to the base interpreter, so resolving makes two
    genuinely different ENVIRONMENTS compare equal, and the candidate gets
    skipped as "already running it" when in fact it has a different
    site-packages and is exactly the one we need.

    The environment is the thing that differs; the binary often is not. The
    SENTINEL env var prevents an infinite loop, so this comparison only has
    to avoid the trivial self-match.
    """
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


def ensure() -> None:
    try:
        import angels  # noqa: F401
        return
    except ModuleNotFoundError as exc:
        if exc.name != "angels":
            raise          # something else is missing; not our problem
        original = exc

    if os.environ.get(SENTINEL):
        # We already switched once and it still cannot import. Re-running
        # again would loop; say so plainly instead.
        raise original

    here = Path(sys.executable)
    tried: list[str] = []

    for c in candidates():
        tried.append(str(c))
        if not c.exists() or _same(c, here):
            continue

        env = dict(os.environ, **{SENTINEL: "1"})
        print(f"  [bootstrap] {here.name} cannot import angels; "
              f"re-running under\n  [bootstrap] {c}", file=sys.stderr)
        # subprocess rather than os.execv: on Windows execv is emulated and
        # mangles quoting in arguments containing spaces, which every path on
        # this machine does.
        sys.exit(subprocess.run([str(c), *sys.argv], env=env).returncode)

    print(f"\n  No interpreter with ANGELS installed was found.\n", file=sys.stderr)
    print(f"  running under: {here}", file=sys.stderr)
    print(f"  looked in:", file=sys.stderr)
    for t in tried:
        print(f"    {t}", file=sys.stderr)
    print(f"\n  See docs/environment.md to create one.\n", file=sys.stderr)
    raise original


ensure()
