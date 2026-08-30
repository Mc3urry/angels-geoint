"""Reporting silence.

PHASE 2.
Find intervals longer than the platform should ever go without reporting,
weighted by the probability a report would have been received at all.

    def find(track, plausibility) -> list[DiscrepancyEvent]

Write the naive threshold version first and run it against
tests/synthetic.inject_gap. Then add coverage weighting and compare -- the
size of the difference between the two is a genuinely interesting number.
Keep it.
"""
