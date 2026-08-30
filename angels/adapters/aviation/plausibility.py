"""Aircraft physical limits and ADS-B receiver coverage.

Numbers are deliberately generous -- these are hard physical bounds used to
catch impossible reports, not typical values. A detector that fires on a fast
business jet is a detector nobody trusts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class AviationPlausibility:
    """Satisfies core.plausibility.PlausibilityModel."""

    # A commercial jet cruises near 250 m/s. Leave headroom for military and
    # for strong tailwinds -- ground speed is what we observe, not airspeed.
    max_speed_mps: float = 340.0

    # ~1.5 g, comfortably above anything an airliner does.
    max_accel_mps2: float = 15.0

    # ADS-B position messages arrive roughly twice a second; OpenSky state
    # vectors are resampled to about 1 s for authenticated users.
    expected_report_interval_s: float = 1.0

    # GPS-derived and quite good.
    nominal_position_uncertainty_m: float = 10.0

    def coverage(self, lat: float, lon: float, t: datetime) -> float:
        """P(reception) from the ground receiver network.

        PHASE 2. Currently a stub that claims perfect coverage everywhere,
        which is false and will make every gap look intentional.

        The real version, roughly:
          * ADS-B is line of sight, so reception depends on receiver density
            AND on aircraft altitude -- an aircraft at 35,000 ft is visible
            far further than one at 2,000 ft.
          * Build a receiver-density surface from OpenSky sensor metadata.
          * Expect near-zero coverage over open ocean and sparse terrain.

        Until this is real, treat every gap result as provisional.
        """
        return 1.0
