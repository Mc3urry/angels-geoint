"""Identifier collisions and reuse.

PHASE 2.
The same identifier reporting from two places at once is physically
impossible, so it is either a spoof or a data error. Both are worth knowing.

    def collisions(tracks) -> list[DiscrepancyEvent]
    def reuse(tracks) -> list[DiscrepancyEvent]

Ground truth: tests/synthetic.duplicate_identity.
"""
