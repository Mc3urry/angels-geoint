"""Pool the land masks that worked into one shoreline the others can use.

    python scripts/build_coastline.py
    python scripts/build_coastline.py --stride 2      # denser, slower

Writes data/reference/coastline/shoreline.json.

WHY

water.from_raster refuses when a scene's own histogram will not split land
from sea, which happens in high wind -- so the coverage that vanishes is the
coverage on rough days, and rough days are also the days with the most
clutter. Measured on the 2024 passes: slice 1 failed on five of twelve
dates, which searched 40-45k km2 against 60-97k on the others.

Sentinel-1 retraces the same ground every twelve days and the coast does not
move, so a mask measured on a pass where the split worked describes the same
shoreline. This pools every scene whose split DID work, in ground
coordinates, and detect_ships.py falls back to it when a scene is refused.

Re-run it when new passes are added: more votes, better shoreline.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime import water
from angels.adapters.maritime.coastline import LandGrid
from angels.adapters.maritime.geolocate import Geolocator
from angels.config import RAW, REFERENCE

SAR = RAW / "sar"
OUT = REFERENCE / "coastline" / "shoreline.json"


def vv_member(zpath: Path) -> str:
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            low = info.filename.lower()
            if "/measurement/" in low and "-vv-" in low \
                    and low.endswith((".tiff", ".tif")):
                return info.filename
    raise FileNotFoundError(f"no VV measurement raster inside {zpath.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stride", type=int, default=None,
                    help="sample every Nth decimated cell (default 4)")
    ap.add_argument("--path", type=Path, help="one scene, for a quick look")
    args = ap.parse_args()

    import rasterio

    scenes = [args.path] if args.path else sorted(SAR.glob("*.zip"))
    if not scenes:
        print(f"\n  No scenes in {SAR}\n")
        return 1

    grid = LandGrid()
    kwargs = {"stride": args.stride} if args.stride else {}
    refused = 0
    for z in scenes:
        try:
            with rasterio.open(f"zip://{z.as_posix()}!/{vv_member(z)}") as src:
                gcps, crs = src.get_gcps()
                loc = Geolocator(gcps, crs)
                mask = water.from_raster(src)
                n = grid.add_scene(loc, mask, name=z.stem, **kwargs)
            print(f"  {z.name[17:32]}  {n:,} samples  "
                  f"{100 * mask.water_fraction:.0f}% water")
        except water.MaskError:
            refused += 1
            print(f"  {z.name[17:32]}  refused -- this is a scene the "
                  f"shoreline is FOR")
        except Exception as exc:                      # noqa: BLE001
            print(f"  {z.name[17:32]}  FAILED: {type(exc).__name__}: {exc}")

    if not grid.scenes:
        print("\n  No scene split cleanly, so there is nothing to pool.\n")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(grid.to_json()), encoding="utf-8")
    decided = sum(1 for k in set(grid.land) | set(grid.water))
    print(f"\n  {grid}")
    print(f"  {refused} scene(s) refused and will use this instead")
    print(f"  wrote {OUT}  ({OUT.stat().st_size / 1e6:.1f} MB, "
          f"{decided:,} cells)")
    print("\n  Next: re-run detect_ships.py on the refused scenes.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
