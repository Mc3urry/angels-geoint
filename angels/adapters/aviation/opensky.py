"""OpenSky ingest: ADS-B reports and MLAT observations.

PHASE 1.
Two OpenSky things, easy to conflate:

  * API CLIENT -- self-serve, instant. OAuth2 client credentials, tokens
    expire after 30 minutes. Basic auth with username/password is no longer
    accepted. Gives live state vectors. Use this for Phase 1.
  * HISTORICAL / TRINO -- an application with a human review step, days to
    approve. State vectors back to 2013. Needed from Phase 2 on.

Apply for the second on day one; build against the first while you wait.

    def token() -> str                                   # cached, auto-refresh
    def fetch_states(bbox) -> list[dict]                 # /states/all
    def to_reports(rows) -> list[Report]
    def to_tracks(reports, split_gap_s) -> list[Track]
    def fetch_mlat(t0, t1, bbox) -> list[Observation]    # historical only

The /states/all response is a 2D ARRAY of 18-element state vectors, not
objects. Write the field mapping once and carefully; the index order is easy
to get subtly wrong and produces plausible-looking garbage.

Filter on partition columns in every Trino query or they WILL suspend your
account. This is not a soft limit.
"""
