"""Track geometry for the map.

PHASE 1.
    GET /tracks?bbox=&start=&end=&domain=

Returns a GeoJSON FeatureCollection of LineStrings, one per track, with
platform_id and callsign as properties.

Keep the response lean -- decimate long tracks server-side. The map does not
need every one-second fix to draw a recognisable path, and shipping them all
is how the frontend starts stuttering.
"""
