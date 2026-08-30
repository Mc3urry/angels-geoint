"""Distance to governance discontinuities.

PHASE 4.
The direct test of the thesis.

    def load_eez(path) -> GeoDataFrame          # Marine Regions / Flanders
    def load_fir(path) -> GeoDataFrame          # flight information regions
    def distance_to_nearest(events, boundaries) -> Series

Get the boundary geometries EARLY. Their quality shapes what analysis is even
possible, and finding that out in month five is expensive.
"""
