"""The adapter contract.

The complete set of domain knowledge the core is allowed to know: four numbers
and one function. If you find yourself wanting a fifth thing, stop -- that is
usually a detector reaching into domain specifics it should not know about.

    Aircraft : max_speed ~300 m/s, interval ~1s,    uncertainty ~10m
    Vessels  : max_speed ~15 m/s,  interval 2-180s, uncertainty ~10-100m
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlausibilityModel(Protocol):
    """What an adapter must supply so the core detectors can do their work."""

    #: Fastest this class of platform can physically travel (m/s).
    max_speed_mps: float

    #: Fastest it can change speed (m/s^2). Catches teleporting reports.
    max_accel_mps2: float

    #: How often a healthy platform reports, in seconds.
    expected_report_interval_s: float

    #: Typical positional error of a report from this source, in metres.
    nominal_position_uncertainty_m: float

    def coverage(self, lat: float, lon: float, t: datetime) -> float:
        """P(a report would be RECEIVED here, given that it was sent).

        The hardest function in the project, and the one that decides whether
        your results mean anything.

        A platform that vanishes mid-ocean may have switched off -- or may
        simply have left receiver coverage. Those look identical in the data
        and mean opposite things. Worse, coverage is itself geographically
        patterned, which directly confounds the boundary hypothesis.

        Returns a value in [0, 1]. A flat 1.0 is a valid starting stub, but
        every gap result is provisional until this is real.
        """
        ...
