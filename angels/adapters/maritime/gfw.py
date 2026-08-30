"""Global Fishing Watch API.

PHASE 3.
Free token, non-commercial use. Under the revised solo scope this is an
INPUT, not just a benchmark -- using a validated instrument so your time goes
to the analysis that is actually yours. Say so plainly in the methods and
nobody will blink.

    def dark_vessels(bbox, t0, t1) -> list[Observation]
    def ais_off_events(bbox, t0, t1) -> list[DiscrepancyEvent]
    def vessel_identity(mmsi) -> VesselIdentity | None

Their published AIS-off events also serve as a weak label source for
validating your own gap detector.
"""
