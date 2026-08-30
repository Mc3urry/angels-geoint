"""ICAO24 to operator lookup.

PHASE 2.
Turns a hex address into something a human recognises, for the dossier view.

    def lookup(icao24) -> AircraftIdentity | None

OpenSky publishes an aircraft metadata database. Registration prefixes are
country-coded, and the operator field is where shell-company patterns show up
-- which matters for the inversion.
"""
