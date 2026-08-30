"""Chronolocation from shadow geometry.

PHASE 5.
Shadow-length-to-height ratio gives solar elevation. The set of points where
the sun sits at that elevation at that instant is a circle on the earth.

    def solar_elevation(lat, lon, t) -> float
    def location_band(ratio, t, step_deg) -> list[tuple[float, float]]
    def intersect(bands) -> Polygon

Use pvlib or astropy for solar position. Do not hand-roll the ephemeris.

VALIDATION -- this is what makes it quantitative rather than anecdotal:
scrape geotagged photos with EXIF timestamps, strip the metadata, run the
solver, measure error in km. Automate it and you get hundreds of cases and a
real error distribution.

Isolated by design. Cutting this costs a UI panel and nothing structural.
"""
