"""Reception modelling: shared helpers.

PHASE 2.
The hardest correctness problem in the project, and the one that decides
whether any gap result means anything.

A platform that stops reporting may have switched off -- or may have left
receiver coverage. Identical in the data, opposite in meaning. And coverage is
itself geographically patterned, so it directly confounds the boundary
hypothesis your whole thesis rests on.

Each adapter supplies its own coverage() because the physics differ. This
module holds what they share: building density surfaces from receiver
positions, and turning a density into a reception probability.

    def density_surface(receivers, bbox, cell_deg) -> Grid
    def reception_probability(density, distance_m, altitude_m) -> float
"""
