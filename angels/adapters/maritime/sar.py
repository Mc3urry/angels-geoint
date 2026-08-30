"""SAR vessel detections: CFAR baseline and xView3 labels.

PHASE 3.
Two sources of Observations:

    def load_xview3_labels(path) -> list[Observation]    # do this first
    def detect_cfar(scene, guard, background, pfa) -> list[Observation]

Use the xView3 labels as your observation layer so the pipeline works before
the detector does. CFAR is then a BOUNDED one-week methods exercise, not a
dependency -- benchmark it against the published numbers, put it in a table,
and stop regardless of the result.

CFAR is a sliding window: estimate local sea clutter statistics in a
background ring, threshold the cell under test against them. Ships are bright
point scatterers on dark water, so it works well.

What will actually generate your false positives:
  * azimuth ambiguities -- phantom ships offset from real bright targets
  * high wind roughening the sea surface and raising clutter
  * wind farms, platforms and buoys, which look like vessels forever

Budget real time for a static infrastructure mask. Land masking quality drives
the false-positive rate more than the detector does.
"""
