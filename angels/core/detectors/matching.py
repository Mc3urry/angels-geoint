"""Observation to report association. THE CORE SUBTRACTION.

PHASE 2.
What a sensor saw, minus what anyone reported. The leftovers are the product.

    def unmatched(observations, tracks, tolerance_m) -> list[DiscrepancyEvent]

Write this one carefully. For each observation:
  1. advance every candidate track to the observation timestamp with
     Track.position_at -- this is the running fix
  2. associate within a radius combining BOTH uncertainties and the distance
     the platform could have moved during interpolation
  3. anything left unassociated is a candidate

Getting that radius wrong in either direction is the difference between a
system that flags everything and one that flags nothing.
"""
