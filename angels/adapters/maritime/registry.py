"""MMSI to vessel identity.

PHASE 3.
    def lookup(mmsi) -> VesselIdentity | None

GFW fuses 40+ registries. MMSI is self-declared and reused constantly, which
is itself a signal -- flag mismatches feed core.detectors.identity.
"""
