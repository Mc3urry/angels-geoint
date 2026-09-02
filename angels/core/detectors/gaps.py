"""Reporting silence.

PHASE 2.
Find intervals longer than the platform should ever go without reporting,
weighted by the probability a report would have been received at all.

    def find(track, plausibility) -> list[DiscrepancyEvent]

Write the naive threshold version first and run it against
tests/synthetic.inject_gap. Then add coverage weighting and compare -- the
size of the difference between the two is a genuinely interesting number.
Keep it.

BEFORE ANY OF THAT: call core.uptime.blind_intervals and drop every candidate
falling inside one. A silence during our own downtime is not a finding, and
this filter is cheaper and more certain than anything the coverage model does.
Order matters -- exclude what we know, then model what we do not.
"""
