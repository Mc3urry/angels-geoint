"""Impossible movement.

PHASE 2.
Implied speed or acceleration between consecutive reports exceeding what the
platform can physically do. Cheap, fast, high-signal.

    def validate(track, plausibility) -> list[DiscrepancyEvent]

Ground truth: tests/synthetic.inject_jump.
"""
