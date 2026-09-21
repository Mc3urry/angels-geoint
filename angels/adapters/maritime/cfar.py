"""CFAR: finding bright points against a background that varies.

PHASE 3. The arithmetic of ship detection, with no file handling in it, so it
can be tested against synthetic imagery with known answers.

THE IDEA

A ship is a corner reflector on a surface that scatters away. In VV
polarisation the sea returns almost nothing and a steel hull returns a lot, so
a vessel is a bright point on a dark field.

A fixed threshold does not work, because "dark" is not a constant. Sea clutter
rises with wind, with incidence angle across the swath, and with proximity to
land. A threshold that finds ships in calm water finds thousands of whitecaps
in a breeze, and one tuned for a breeze misses everything on a calm day.

So the threshold is computed locally, per pixel, from the pixels around it:

    background ring   estimate mu and sigma of the clutter HERE
    guard ring        excluded, so a large ship does not raise its own
                      background and hide itself
    cell under test   flagged if it exceeds mu + k*sigma

CFAR stands for Constant False Alarm Rate: choosing k in units of sigma rather
than in DN means the expected false alarm rate stays roughly fixed as the
clutter level changes, which is the entire point.

WHY INTEGRAL IMAGES

A 433-megapixel scene with a 41x41 background window is 7e11 multiply-adds
done naively. The same result comes from two passes of a cumulative sum: the
sum over any rectangle is four lookups in the integral image, regardless of
window size. That turns hours into seconds and is the only reason this runs on
a laptop.

The variance comes from a second integral image over the squares, using
E[X^2] - E[X]^2. That identity is numerically poor when the mean is large
relative to the spread -- which is exactly the case for uint16 SAR DN -- so
the arrays are converted to float64 and the variance is clamped at zero rather
than allowed to go slightly negative and produce NaN.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Window sizes in PIXELS, at 10 m GRD spacing.
#
# SIZED BY THE LARGEST VESSEL, not by convenience. A ship wider than the guard
# ring puts its own bright pixels into its own background estimate, raising mu
# and sigma until it no longer exceeds its own threshold. It does not appear
# as a weak detection -- it vanishes entirely, and the vessels that vanish are
# the biggest ones.
#
# Measured on synthetic ships of increasing size:
#
#     guard  11 px (110 m)  ->  loses everything over 210 m
#     guard  21 px (210 m)  ->  loses everything over 270 m
#     guard  41 px (410 m)  ->  detects up to 390 m
#
# The largest container ships are about 400 m. A 110 m guard would have
# silently dropped every Post-Panamax vessel entering Baltimore -- the most
# conspicuous traffic in the study area -- while detecting fishing boats
# perfectly, which is the sort of bias that survives all the way to a
# conclusion.
GUARD = 41            # 410 m -- clears the largest hull afloat
BACKGROUND = 121      # 1.2 km -- enough ring area around a 410 m guard

# Threshold in sigmas. Lower finds more and costs precision; the maritime
# literature uses roughly 4-8 for ship detection in VV. Tune against a scene
# with known AIS traffic rather than by eye.
K_SIGMA = 6.0


@dataclass(frozen=True)
class Detection:
    """One bright cluster, in PIXEL coordinates.

    Deliberately not geographic. Turning pixels into positions needs the GCP
    grid, which belongs to the file, not to the arithmetic -- keeping them
    apart is what lets this module be tested without a gigabyte on disk.
    """

    row: float            # centroid, sub-pixel
    col: float
    peak: float           # brightest DN in the cluster
    mean: float           # mean DN over the cluster
    background: float     # local clutter mean where it was found
    sigma: float          # local clutter standard deviation
    pixels: int           # cluster size

    @property
    def snr(self) -> float:
        """How far above local clutter, in sigmas. The confidence signal."""
        return (self.peak - self.background) / max(self.sigma, 1e-6)

    @property
    def length_m(self) -> float:
        """Very rough size, assuming 10 m pixels and a roughly square cluster.

        A real length needs the cluster's principal axis and a correction for
        the smearing a moving target suffers in azimuth. This is a sanity
        figure only: it separates "plausible vessel" from "one hot pixel".
        """
        return float(np.sqrt(self.pixels) * 10.0)


def _integral(a: np.ndarray) -> np.ndarray:
    """Summed-area table with a zero row and column, so window sums are
    four lookups with no edge special-casing."""
    out = np.zeros((a.shape[0] + 1, a.shape[1] + 1), dtype=np.float64)
    np.cumsum(np.cumsum(a, axis=0, dtype=np.float64), axis=1, out=out[1:, 1:])
    return out


def _window_sum(ii: np.ndarray, half: int) -> np.ndarray:
    """Sum over a (2*half+1) square centred on every pixel.

    Edges are handled by clipping the window against the array, so pixels near
    the border get a smaller but still valid window rather than a wrapped or
    reflected one. Reflecting would invent clutter that is not there; wrapping
    would import the far side of the scene.
    """
    h, w = ii.shape[0] - 1, ii.shape[1] - 1
    r = np.arange(h)
    c = np.arange(w)
    r0 = np.clip(r - half, 0, h)[:, None]
    r1 = np.clip(r + half + 1, 0, h)[:, None]
    c0 = np.clip(c - half, 0, w)[None, :]
    c1 = np.clip(c + half + 1, 0, w)[None, :]
    total = ii[r1, c1] - ii[r0, c1] - ii[r1, c0] + ii[r0, c0]
    count = (r1 - r0) * (c1 - c0)
    return total, count.astype(np.float64)


def local_stats(arr: np.ndarray, *, guard: int = GUARD,
                background: int = BACKGROUND) -> tuple[np.ndarray, np.ndarray]:
    """Mean and standard deviation of the clutter ring around every pixel.

    The ring is the background square MINUS the guard square. Excluding the
    guard is not a detail: a 200 m vessel inside a 410 m background window
    raises the local mean enough to threshold itself away. The guard is why a
    large ship is still detectable.
    """
    if background <= guard:
        raise ValueError("background window must be larger than the guard")

    a = arr.astype(np.float64, copy=False)
    ii1 = _integral(a)
    ii2 = _integral(a * a)

    bg_half, g_half = background // 2, guard // 2
    s_bg, n_bg = _window_sum(ii1, bg_half)
    q_bg, _ = _window_sum(ii2, bg_half)
    s_g, n_g = _window_sum(ii1, g_half)
    q_g, _ = _window_sum(ii2, g_half)

    s = s_bg - s_g
    q = q_bg - q_g
    n = np.maximum(n_bg - n_g, 1.0)

    mu = s / n
    # E[X^2] - E[X]^2 loses precision badly for large means, which uint16 DN
    # certainly are. float64 above, and a floor at zero here, because a
    # variance of -1e-9 becomes NaN under sqrt and NaN propagates silently
    # through every comparison as False -- a detector that quietly finds
    # nothing.
    var = np.maximum(q / n - mu * mu, 0.0)
    return mu, np.sqrt(var)


def threshold_mask(arr: np.ndarray, *, guard: int = GUARD,
                   background: int = BACKGROUND,
                   k: float = K_SIGMA,
                   valid: np.ndarray | None = None
                   ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pixels exceeding mu + k*sigma of their local clutter.

    `valid` masks out pixels that must never be tested -- land, nodata. They
    are excluded from the OUTPUT, but note they still contribute to the
    background statistics of their neighbours, which is why land masking has
    to happen before this in any serious pipeline: a coastline in the
    background ring inflates sigma for hundreds of metres offshore and
    suppresses real detections there.
    """
    mu, sigma = local_stats(arr, guard=guard, background=background)
    hot = arr.astype(np.float64, copy=False) > (mu + k * sigma)
    if valid is not None:
        hot &= valid
    return hot, mu, sigma


# A sane ceiling on how much of a scene may be "bright". Real vessels are a
# vanishing fraction of an ocean scene -- a few thousand pixels out of 433
# million. Anything near this fraction means the threshold is too low, or land
# is unmasked, and the connected-components pass below would crawl through
# millions of pixels in Python before producing a useless answer.
MAX_HOT_FRACTION = 0.02


class TooManyDetections(RuntimeError):
    """The threshold admitted an implausible share of the scene."""


def label_clusters(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Connected components, 8-connected, without scipy.

    Two-pass union-find. Written out rather than imported because scipy is a
    large dependency to add for one function, and because a vessel is a
    handful of adjacent pixels -- the cluster sizes here are tiny even if the
    image is not.
    """
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    rows, cols = np.nonzero(mask)
    for r, c in zip(rows.tolist(), cols.tolist()):
        neigh = []
        for dr, dc in ((-1, -1), (-1, 0), (-1, 1), (0, -1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < h and 0 <= cc < w and labels[rr, cc]:
                neigh.append(int(labels[rr, cc]))
        if not neigh:
            parent.append(len(parent))
            labels[r, c] = len(parent) - 1
        else:
            m = min(neigh)
            labels[r, c] = m
            for n in neigh:
                union(m, n)

    if len(parent) == 1:
        return labels, 0

    remap = {}
    for i in range(1, len(parent)):
        root = find(i)
        if root not in remap:
            remap[root] = len(remap) + 1
    flat = labels.ravel()
    nz = flat > 0
    flat[nz] = [remap[find(int(v))] for v in flat[nz]]
    return labels, len(remap)


def detect(arr: np.ndarray, *, guard: int = GUARD,
           background: int = BACKGROUND, k: float = K_SIGMA,
           min_pixels: int = 2, max_pixels: int = 5000,
           valid: np.ndarray | None = None) -> list[Detection]:
    """Bright clusters against local clutter, as pixel-space Detections.

    min_pixels rejects single hot pixels, which in SAR are overwhelmingly
    speckle and thermal noise rather than vessels. max_pixels rejects blobs too
    large to be a ship -- unmasked land, a storm cell, an island -- because a
    detector that reports the Delmarva Peninsula as a 40 km vessel is not
    wrong in an interesting way.
    """
    hot, mu, sigma = threshold_mask(arr, guard=guard, background=background,
                                    k=k, valid=valid)

    # Fail loudly rather than slowly. Without this, an unmasked coastline sets
    # tens of millions of pixels and the Python-level labelling below runs for
    # hours before returning something meaningless -- indistinguishable, while
    # it runs, from a big scene taking a while.
    frac = hot.mean()
    if frac > MAX_HOT_FRACTION:
        raise TooManyDetections(
            f"{100 * frac:.1f}% of the window exceeded the threshold "
            f"(limit {100 * MAX_HOT_FRACTION:.0f}%). Vessels are a vanishing "
            f"fraction of an ocean scene, so this means k={k} is too low for "
            f"this clutter, or land is in the window and unmasked."
        )

    labels, n = label_clusters(hot)
    if n == 0:
        return []

    a = arr.astype(np.float64, copy=False)
    out: list[Detection] = []
    for lab in range(1, n + 1):
        rr, cc = np.nonzero(labels == lab)
        if not (min_pixels <= rr.size <= max_pixels):
            continue
        vals = a[rr, cc]
        # Intensity-weighted centroid: the brightest part of a vessel is the
        # superstructure, and weighting pulls the reported position toward it
        # rather than to the middle of whatever speckle came along.
        wsum = vals.sum()
        out.append(Detection(
            row=float((rr * vals).sum() / wsum),
            col=float((cc * vals).sum() / wsum),
            peak=float(vals.max()),
            mean=float(vals.mean()),
            background=float(mu[rr, cc].mean()),
            sigma=float(sigma[rr, cc].mean()),
            pixels=int(rr.size),
        ))

    out.sort(key=lambda d: -d.snr)
    return out
