"""Deciding which pixels are sea, before deciding which of them are ships.

PHASE 3.

WHY THIS IS NOT A DETAIL

A CFAR detector asks whether a pixel is bright relative to its surroundings.
Over water that question is meaningful. Over land it is meaningless and the
answer is yes, constantly -- a rooftop, a road sign, a silo and a parked lorry
are all corner reflectors, and there are millions of them.

The first honest run of detect_ships.py on a real Chesapeake scene returned
4,029 detections from six tiles, roughly 6% of one scene. The two densest
clusters were at -76.75/38.95 and -75.55/39.10: Upper Marlboro, Maryland, and
the fields outside Smyrna, Delaware. Neither is navigable.

WHY THE FIRST ATTEMPT FAILED

The stand-in masked pixels whose local mean exceeded three times the MEDIAN OF
THE TILE. That works when a tile is mostly water: the median is then the sea
level and land stands above it. When a tile is mostly land the median IS land,
the threshold rises with it, and the mask quietly inverts -- it protects the
land and masks nothing. The failure is worst exactly where it matters most,
and it is invisible from the output, which looks like a busy scene.

WHAT REPLACES IT

One threshold for the WHOLE SCENE, derived from the whole scene's histogram.
A 250 km swath containing a coast is bimodal: a tall narrow dark mode (water)
and a broad bright one (land). Otsu's method finds the split that minimises
the variance within the two classes, and because the histogram comes from the
entire raster rather than from one tile, no tile can drag the threshold to
itself.

THE GUARD THAT MATTERS

Otsu always returns a threshold. Handed an image with only one mode -- a slice
of open ocean -- it will split the sea into "darker sea" and "brighter sea"
and report, with no sign of distress, that half the Atlantic is land. So the
split is checked for bimodality before it is believed, and a scene that is not
bimodal is declared all water. Masking nothing is a recoverable error; masking
the sea is a silent one.

THE COST, WHICH IS REAL

Land is buffered seaward by half the CFAR background window, because a
coastline inside that window inflates sigma and suppresses genuine detections
for hundreds of metres offshore. That buffer is about 600 m of coast where
this pipeline can see nothing -- so vessels alongside, at a pier, or anchored
close in are outside what any result here may claim. The 3 nm limit is 5,556 m
out, so the jurisdictional boundaries this project is about all sit well
beyond the blind band. That is a fact to state in the methods, not a footnote:
the mask defines the part of the sea about which anything may be said.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import cfar


class MaskError(RuntimeError):
    """The sea/land split could not be made, or should not be trusted."""


# Land and water must be separated by at least this ratio in mean amplitude
# before the split is believed. Sea in VV is close to noise; land is an order
# of magnitude brighter. A ratio near 1 means Otsu cut a single mode in half.
MIN_CONTRAST = 1.8

# A scene where almost everything lands on one side of the threshold is either
# open ocean (fine, and handled) or entirely inland (not a maritime scene, and
# worth saying so rather than returning an empty sea).
MIN_WATER_FRACTION = 0.02

# Reference only, NOT a threshold. Coefficient of variation of the decimated
# scene: pure speckle over open water averages down to about 0.07 across 256
# pixels, while land keeps structure at 160 m. It is reported so a human can
# judge an ambiguous scene, and deliberately not branched on -- measured on
# synthetic land the two populations overlap between 0.15 and 0.30, and real
# ocean carrying wind streaks, swell and rain cells sits inside that band. A
# constant placed there would be a coin toss wearing a decimal point.
TYPICAL_OCEAN_CV = 0.07


@dataclass(frozen=True)
class WaterMask:
    """Which pixels of a scene may be tested for vessels.

    Held at DECIMATED resolution -- one cell per `decimation` pixels each way.
    A full-resolution boolean for a 425 MP scene is 425 MB; at 16x it is 1.6 MB
    and the shoreline is still located to about 160 m, which is finer than the
    600 m buffer deliberately applied to it.
    """

    water: np.ndarray          # decimated, True where testable
    decimation: int
    shape: tuple[int, int]     # full-resolution (height, width)
    threshold: float           # in log1p(DN)
    contrast: float            # land mean / sea mean, linear DN
    water_fraction: float      # of the illuminated scene, after buffering
    bimodal: bool
    texture: float = 0.0       # CV of the decimated scene; only set when the
                               # split failed and texture had to decide

    def __str__(self) -> str:
        if not self.bimodal:
            return ("no coast in this scene -- all water, by texture "
                    f"{self.texture:.2f} (land would exceed "
                    f"{MAX_OCEAN_CV})")
        return (f"{100 * self.water_fraction:.0f}% water, land/sea contrast "
                f"{self.contrast:.1f}x, {self.decimation}x decimated")

    def tile(self, row0: int, col0: int, height: int, width: int) -> np.ndarray:
        """The mask for one full-resolution window, as a full-resolution bool.

        Nearest-neighbour upsampling. The mask is a decision about 160 m cells
        and interpolating it would invent a shoreline that is smoother than
        the one measured, at a resolution the measurement does not support.
        """
        d = self.decimation
        rr = np.clip(np.arange(row0, row0 + height) // d,
                     0, self.water.shape[0] - 1)
        cc = np.clip(np.arange(col0, col0 + width) // d,
                     0, self.water.shape[1] - 1)
        return self.water[np.ix_(rr, cc)]


def otsu(values: np.ndarray, bins: int = 512) -> tuple[float, float]:
    """Otsu's threshold, and the between-class variance it achieved.

    The variance is returned because the threshold alone is not evidence of
    anything -- Otsu is a minimisation, and a minimisation over a unimodal
    histogram still has a minimum.
    """
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise MaskError("no finite pixels to threshold")

    hist, edges = np.histogram(finite, bins=bins)
    centres = (edges[:-1] + edges[1:]) / 2
    total = hist.sum()
    if total == 0:
        raise MaskError("empty histogram")

    w = np.cumsum(hist) / total                       # weight of class 0
    m = np.cumsum(hist * centres) / total
    m_all = m[-1]

    denom = w * (1 - w)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = np.where(denom > 0, (m_all * w - m) ** 2 / denom, 0.0)

    i = int(np.nanargmax(between))
    return float(centres[i]), float(between[i])


def _overview(src, decimation: int) -> np.ndarray:
    """A decimated read of the whole raster.

    rasterio serves this from the COG's overview pyramid where one exists, so
    a 425 MP scene comes back as a 1.6 MP array in about a second rather than
    being read in full.
    """
    h, w = src.height, src.width
    out = (max(1, h // decimation), max(1, w // decimation))
    return src.read(1, out_shape=out, masked=False)


def from_raster(src, *, decimation: int = 16,
                buffer_m: float | None = None,
                pixel_m: float = 10.0) -> WaterMask:
    """Sea/land for an open rasterio dataset, from its own histogram.

    `buffer_m` defaults to half the CFAR background window -- the distance at
    which a coastline stops contaminating a background ring. Passing 0 keeps
    the raw shoreline, which is right for inspecting the mask and wrong for
    running the detector.
    """
    small = _overview(src, decimation).astype(np.float64)

    # The swath is a parallelogram inside a rectangular raster, so the corners
    # are filled with exact zeros. They are darker than water and would pull
    # the water mode down and the threshold with it, so they are excluded from
    # the histogram entirely -- and marked unusable in the result.
    lit = small > 0
    if not lit.any():
        raise MaskError("the raster is entirely zero -- truncated download?")

    log = np.log1p(small)
    thresh, between = otsu(log[lit])

    sea = lit & (log <= thresh)
    land = lit & ~sea

    sea_mean = float(small[sea].mean()) if sea.any() else 0.0
    land_mean = float(small[land].mean()) if land.any() else 0.0
    contrast = land_mean / sea_mean if sea_mean > 0 else float("inf")

    bimodal = contrast >= MIN_CONTRAST and sea.any() and land.any()
    if not bimodal:
        # ONE MODE. But WHICH one? The first version of this guard assumed
        # ocean and returned "all water", and on a slice covering inland
        # Pennsylvania that produced 112,954 detections of towns, roads and
        # silos -- none of them inside the study area, all of them formatted
        # exactly like vessels.
        #
        # Failing to tell land from sea is not a reason to call it sea. It is
        # the most dangerous of the two defaults, because unmasked land does
        # not look like an error downstream; it looks like traffic.
        #
        # So the two cases are separated by TEXTURE rather than assumed.
        # Decimation by 16 averages 256 pixels, which removes almost all
        # speckle: open water then reads as a nearly flat field, while land
        # keeps real structure at 160 m -- field boundaries, towns, ridges.
        cv = float(small[lit].std() / max(small[lit].mean(), 1e-9))
        raise MaskError(
            f"cannot tell land from sea in this scene: Otsu found ONE mode "
            f"(contrast {contrast:.2f}x, below {MIN_CONTRAST}).\n"
            f"  That happens for open ocean AND for a scene that is all land, "
            f"and nothing here can separate them -- texture is {cv:.2f} "
            f"against about {TYPICAL_OCEAN_CV} for calm water, but land and "
            f"weathered sea overlap across that range.\n"
            f"  Refusing rather than guessing. Assuming ocean is what searched "
            f"inland Pennsylvania and returned 112,954 towns and silos "
            f"formatted as vessels.\n"
            f"  If this slice really is open water, re-run it with "
            f"--no-land-mask and say so in the methods. If it is not, check "
            f"WHICH slice it is: the outer slices of a pass often miss the "
            f"sea box entirely."
        )

    if buffer_m is None:
        buffer_m = cfar.BACKGROUND / 2 * pixel_m

    cells = int(np.ceil(buffer_m / (decimation * pixel_m)))
    if cells > 0:
        # Dilate land seaward. Reuses the summed-area machinery from cfar --
        # a window sum over a boolean is nonzero exactly where the window
        # touches land.
        near, _ = cfar._window_sum(cfar._integral(land.astype(np.float64)),
                                   cells)
        sea = sea & (near == 0)

    frac = float(sea.sum() / max(1, lit.sum()))
    if frac < MIN_WATER_FRACTION:
        raise MaskError(
            f"only {100 * frac:.1f}% of this scene is open water after "
            f"buffering. Either it is an inland scene, or the threshold is "
            f"wrong. Detecting vessels here would mean detecting them in "
            f"fields."
        )

    return WaterMask(water=sea, decimation=decimation,
                     shape=(src.height, src.width), threshold=thresh,
                     contrast=contrast, water_fraction=frac, bimodal=True,
                     texture=float(small[lit].std()
                                   / max(small[lit].mean(), 1e-9)))
