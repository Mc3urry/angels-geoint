"""What every domain adapter must provide.

An adapter has exactly two jobs:

  1. Translate its native feed into core Tracks and Observations.
  2. Supply a PlausibilityModel describing its platforms physical limits
     and its receiver coverage.

That is the entire seam. If an adapter needs to reach into core to do its job,
or core needs to know which adapter it is talking to, the boundary has leaked
-- record it in docs/core-changelog.md and fix it deliberately.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Protocol, runtime_checkable

from angels.core.models import Domain, Observation, Track
from angels.core.plausibility import PlausibilityModel


@runtime_checkable
class Adapter(Protocol):
    """One data domain: aviation, maritime, or whatever you add next."""

    domain: Domain
    plausibility: PlausibilityModel

    def tracks(self, t_start: datetime, t_end: datetime,
               bbox: tuple[float, float, float, float]) -> Iterable[Track]:
        """Cooperative reports, grouped into tracks.

        bbox is (min_lon, min_lat, max_lon, max_lat).
        """
        ...

    def observations(self, t_start: datetime, t_end: datetime,
                     bbox: tuple[float, float, float, float]
                     ) -> Iterable[Observation]:
        """Independent sensor detections over the same window.

        Aviation returns MLAT fixes. Maritime returns SAR detections. Both are
        positions nobody had to volunteer.
        """
        ...
