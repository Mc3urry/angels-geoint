"""Vessel physical limits and AIS receiver coverage.

The maritime coverage problem is harder than the aviation one, because
terrestrial and satellite AIS have completely different footprints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class MaritimePlausibility:
    """Satisfies core.plausibility.PlausibilityModel."""

    # ~50 knots. Fast for a ship; anything above is a bad fix or a spoof.
    max_speed_mps: float = 26.0

    # Ships are heavy. They do not accelerate quickly.
    max_accel_mps2: float = 1.0

    # AIS report rate is speed- and class-dependent: 2 s for a fast Class A
    # underway, up to 180 s for one at anchor. Using the slow end here means
    # the gap detector is conservative by default -- prefer that direction.
    expected_report_interval_s: float = 180.0

    # Wider than aviation. Older Class B units are noticeably sloppier.
    nominal_position_uncertainty_m: float = 50.0

    def coverage(self, lat: float, lon: float, t: datetime) -> float:
        """P(reception) from terrestrial plus satellite AIS.

        PHASE 3. Stub. Returns 1.0, which is wrong everywhere and badly wrong
        offshore.

        The real version:
          * Terrestrial AIS reaches roughly 40-60 nmi from shore. Strong,
            dense, reliable.
          * Satellite AIS covers the rest, but unevenly -- it depends on orbit
            timing, and it suffers message collision in crowded shipping lanes,
            where MORE traffic means WORSE reception.
          * That last point matters for your thesis: reception quality is
            correlated with traffic density, which is also your normalization
            denominator. Model it explicitly or the confound is invisible.
        """
        return 1.0
