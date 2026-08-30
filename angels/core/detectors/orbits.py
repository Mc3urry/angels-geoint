"""Repeated circling. Does double duty.

PHASE 2.
The same features that identify a surveillance aircraft holding over a fixed
ground point identify a vessel circling at a rendezvous. Write it once,
generically, over turn-rate and displacement statistics -- resist ANY
aviation-specific logic here.

    def classify(track) -> OrbitFeatures

Features worth extracting, following the BuzzFeed approach:
  * bounding box area and flight duration
  * turn-rate histogram -- their most predictive single feature
  * speed and altitude quantiles
  * centroid dwell time
  * path length over net displacement

Ground truth: tests/synthetic.orbit_track.
Consumed by angels.inversion.features.
"""
