"""AIS ingest.

PHASE 3.
MarineCadastre bulk downloads are the most forgiving place to develop --
dense, clean, free, US waters. Point somewhere harder once it works.

    def load(paths) -> list[Report]
    def to_tracks(reports, split_gap_s) -> list[Track]

Constrain the AOI and time window from day one. Global AIS runs to billions of
messages and this kills more student projects than any modelling problem.
Parquet plus DuckDB, never pandas over raw CSV.
"""
