"""Flight features for the surveillance classifier.

PHASE 2.
Consumes core.detectors.orbits.classify and shapes it into a feature vector.

    def extract(track) -> dict[str, float]
    def build_matrix(tracks) -> tuple[np.ndarray, list[str]]

Rebuild the BuzzFeed feature set from your own data first, and verify you can
roughly reproduce their reported performance on THEIR labels before trusting
yours on your own. That check is cheap and it will save you from shipping a
broken model with a confident writeup.
"""
