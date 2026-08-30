"""Poll OpenSky and append to Parquet.

PHASE 1.
Run this in the background all week. You are building your own archive while
the historical access application is pending, and by Friday you will have real
multi-day data to work with.

    python scripts/ingest_aviation.py --interval 10

Partition by hour: data/raw/aviation/hour=YYYYMMDDHH/*.parquet
"""
