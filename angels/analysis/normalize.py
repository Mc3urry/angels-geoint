"""Events to rates. THE CRITICAL ONE.

PHASE 4.
Everything downstream is meaningless without this, and it is fiddlier than it
sounds.

Dark events obviously cluster where traffic is heavy. That is not a finding.
Your dependent variable has to be a RATE -- events normalized by total
observed traffic in the same cell and period -- so you are testing whether the
PROPENSITY to go dark varies geographically, not whether ships exist.

    def traffic_density(tracks, grid, period) -> Grid
    def event_rate(events, density, grid, period) -> Grid

The hard part: uneven observation looks exactly like uneven behaviour. Your
denominator must account for coverage, which is itself spatially patterned.
This is the most likely place for Phase 4 to overrun, and the first thing a
sharp committee member will probe.
"""
