"""Candidate ranking.

PHASE 2.
Not every discrepancy deserves a human. This decides the order of the queue.

The ranking function is your actual contribution -- the subtraction that
produces candidates is trivial, but deciding which of ten thousand unmatched
returns is worth looking at is not.

Signals worth combining:
  * size or class of the platform (a 200 m hull matters more than a skiff)
  * where it happened (inside an EEZ or MPA? near a boundary?)
  * corroboration (did a nearby track go silent just before?)
  * duration and how cleanly the detector fired
  * coverage quality at that location -- a gap in strong coverage is far more
    interesting than one at the edge of reception

    def score(event, context) -> float          # 0..1
    def rank(events, context) -> list[Event]
"""
