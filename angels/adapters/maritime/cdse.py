"""Copernicus Data Space: search and download Sentinel-1.

PHASE 3. The maritime observation channel's front door.

Two services, and they authenticate differently, which is the first thing that
trips people up:

    CATALOGUE   catalogue.dataspace.copernicus.eu/odata/v1
                Searching is ANONYMOUS. No token, no account, no quota. This
                is why scripts/check_sar_coverage.py works before you have
                credentials.

    DOWNLOAD    the same host, /Products(id)/$value
                Needs a Bearer token from the Keycloak identity server, via a
                plain username/password grant.

THE TOKEN IS SHORT-LIVED AND THE FILES ARE LARGE

An access token lasts about ten minutes. A Sentinel-1 GRD product is roughly a
gigabyte. On a slow evening connection those two facts collide: the token
expires mid-transfer and the server drops you, usually past the halfway mark.

So downloads here are RESUMABLE by HTTP Range, and the token is re-minted
before each attempt. A partial file is kept as `.part` and continued rather
than restarted, which turns a token expiry from a lost download into a pause.
This is not defensive programming for its own sake -- it is the difference
between fetching fifty scenes unattended and babysitting each one.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import httpx

log = logging.getLogger(__name__)

ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
CLIENT_ID = "cdse-public"

TOKEN_TTL_S = 600
TOKEN_MARGIN_S = 60


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

class MissingCredentials(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "CDSE_USERNAME and CDSE_PASSWORD are not set. Register free at "
            "https://dataspace.copernicus.eu (no review queue), then put them "
            "in .env -- see .env.example. Searching the catalogue needs no "
            "credentials; only downloading does."
        )


class TokenManager:
    """Keycloak password grant, refreshed on demand.

    Mirrors adapters/aviation/opensky.TokenManager deliberately. Two services,
    two auth schemes, one shape -- so neither adapter teaches you a habit the
    other one punishes.
    """

    def __init__(self, username: str = "", password: str = "",
                 *, client: httpx.Client | None = None) -> None:
        self.username = username or os.getenv("CDSE_USERNAME", "")
        self.password = password or os.getenv("CDSE_PASSWORD", "")
        self._client = client
        self._token = ""
        self._expires = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires - TOKEN_MARGIN_S:
            return self._token
        if not self.username or not self.password:
            raise MissingCredentials()

        c = self._client or httpx.Client(timeout=60.0)
        try:
            r = c.post(TOKEN_URL, data={
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "client_id": CLIENT_ID,
            })
            if r.status_code != 200:
                raise RuntimeError(
                    f"CDSE auth failed (HTTP {r.status_code}). Check "
                    f"CDSE_USERNAME and CDSE_PASSWORD in .env. Response: "
                    f"{r.text[:200]}")
            body = r.json()
        finally:
            if self._client is None:
                c.close()

        self._token = body["access_token"]
        self._expires = time.time() + float(body.get("expires_in", TOKEN_TTL_S))
        return self._token


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Scene:
    """One Sentinel-1 acquisition, as the catalogue describes it."""

    id: str
    name: str
    t: datetime
    coverage: float           # fraction of the study box, upper bound
    size_bytes: int
    online: bool
    footprint: tuple[float, float, float, float] | None = None

    @property
    def product_type(self) -> str:
        parts = self.name.split("_")
        return parts[2][:3] if len(parts) > 2 else "?"

    @property
    def satellite(self) -> str:
        return self.name[:3]

    @property
    def orbit(self) -> str:
        """Absolute orbit number, field 7 of the SAFE name.

        S1D_IW_GRDH_1SDV_<start>_<stop>_004274_007DD6_26E5
                                        ^^^^^^ this one

        All slices of one overflight share it, which is what makes them
        groupable into a pass.
        """
        parts = self.name.split("_")
        return parts[6] if len(parts) > 6 else "?"

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1e6

    def __str__(self) -> str:
        return (f"{self.t:%Y-%m-%d %H:%M}  {100 * self.coverage:>3.0f}% box  "
                f"{self.size_mb:>7.0f} MB  {self.name[:48]}")


def wkt(box: tuple[float, float, float, float]) -> str:
    lomin, lamin, lomax, lamax = box
    pts = [(lomin, lamin), (lomax, lamin), (lomax, lamax),
           (lomin, lamax), (lomin, lamin)]
    return "POLYGON((" + ", ".join(f"{x} {y}" for x, y in pts) + "))"


def footprint_box(p: dict) -> tuple[float, float, float, float] | None:
    geo = p.get("GeoFootprint")
    coords: list = []
    if isinstance(geo, dict) and geo.get("coordinates"):
        def flat(c):
            if c and isinstance(c[0], (int, float)):
                yield c
            else:
                for sub in c:
                    yield from flat(sub)
        coords = list(flat(geo["coordinates"]))
    else:
        s = p.get("Footprint") or ""
        if "((" not in s:
            return None
        body = s[s.index("((") + 2: s.rindex("))")]
        for pair in body.replace("(", "").replace(")", "").split(","):
            bits = pair.split()
            if len(bits) >= 2:
                try:
                    coords.append([float(bits[0]), float(bits[1])])
                except ValueError:
                    pass
    if not coords:
        return None
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def overlap_fraction(fp, box) -> float:
    """Fraction of `box` covered by footprint `fp`. Bounding-box upper bound.

    A Sentinel-1 footprint is a slanted parallelogram, so its bbox overstates
    the overlap. Overstating coverage understates how many scenes you need,
    which is the safe direction to be wrong in: it can never make a thin
    dataset look adequate.
    """
    if not fp:
        return 0.0
    ax0, ay0, ax1, ay1 = fp
    bx0, by0, bx1, by1 = box
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    barea = (bx1 - bx0) * (by1 - by0)
    return (ix * iy) / barea if barea > 0 else 0.0


def grid_coverage(footprints, box, *, n: int = 240) -> float:
    """Fraction of `box` covered by the UNION of several footprints.

    Summing individual coverages would double-count where slices overlap, and
    taking the bounding box of the union would count the empty corners of a
    diagonal strip as covered. Both are wrong in the optimistic direction,
    which is the direction that makes a thin dataset look adequate.

    So: rasterise. Lay an n x n grid over the study box, mark every cell whose
    centre falls inside any footprint, count the marks. Crude, honest, and
    fast enough at 240x240 that it does not matter.
    """
    bx0, by0, bx1, by1 = box
    if bx1 <= bx0 or by1 <= by0:
        return 0.0
    dx, dy = (bx1 - bx0) / n, (by1 - by0) / n

    hit = 0
    for j in range(n):
        cy = by0 + (j + 0.5) * dy
        for i in range(n):
            cx = bx0 + (i + 0.5) * dx
            for fp in footprints:
                if fp and fp[0] <= cx <= fp[2] and fp[1] <= cy <= fp[3]:
                    hit += 1
                    break
    return hit / (n * n)


@dataclass(frozen=True)
class Pass:
    """One satellite overflight: every slice sharing an orbit number.

    WHY THIS TYPE EXISTS, AND WHY ASKING FOR ONE SCENE WAS THE WRONG QUESTION.

    AOI_SEA is 545 x 398 km, about 217,000 square kilometres. A Sentinel-1 IW
    swath is 250 km wide and a GRD slice runs roughly 170 km along track, so
    ONE SCENE CAN NEVER COVER MORE THAN ABOUT 20% OF THE BOX. Asking for a
    scene covering 40% returns nothing, not because coverage is poor but
    because the request is geometrically impossible.

    The unit of observation is therefore not the scene, it is the PASS: the
    strip of adjacent slices the satellite laid down in one overflight, all
    sharing a timestamp to within a couple of minutes. That is what "the
    satellite looked at the bay at 22:49 on 24 August" actually means, and it
    is what matching.py needs -- a single instant of ground truth over as much
    water as the pass saw.
    """

    satellite: str
    orbit: str
    t: datetime
    scenes: list[Scene]
    coverage: float

    @property
    def lon_span(self) -> tuple[float, float]:
        fps = [s.footprint for s in self.scenes if s.footprint]
        if not fps:
            return (0.0, 0.0)
        return (min(f[0] for f in fps), max(f[2] for f in fps))

    @property
    def limits(self) -> tuple[int, ...]:
        """Which jurisdictional limits this overflight actually contains.

        THE NUMBER THAT SHOULD DECIDE WHAT TO DOWNLOAD, and the one coverage
        percentage cannot see. The first pass fetched covered 54% of AOI_SEA
        and did not touch the Chesapeake or any of the 3, 12 and 24 nm limits
        -- its westmost pixel was at -75.19, and the 24 nm line is at -75.47.
        It was half the study box of open Atlantic.

        Area is not the independent variable. Boundaries are.
        """
        from angels.config import limits_crossed
        return limits_crossed(*self.lon_span)

    @property
    def size_bytes(self) -> int:
        return sum(s.size_bytes for s in self.scenes)

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1e9

    def __str__(self) -> str:
        lim = ("+".join(str(n) for n in self.limits) + " nm") if self.limits \
            else "no limit"
        return (f"{self.t:%Y-%m-%d %H:%M}  {100 * self.coverage:>3.0f}% box  "
                f"{lim:>12}  {len(self.scenes):>2} slices  "
                f"{self.size_gb:>4.1f} GB  {self.satellite} {self.orbit}")


def group_passes(scenes: list[Scene], box,
                 *, min_coverage: float = 0.0) -> list[Pass]:
    """Collapse slices into overflights, with true union coverage."""
    by_orbit: dict[tuple[str, str], list[Scene]] = {}
    for s in scenes:
        by_orbit.setdefault((s.satellite, s.orbit), []).append(s)

    out: list[Pass] = []
    for (sat, orbit), group in by_orbit.items():
        group.sort(key=lambda s: s.t)
        cov = grid_coverage([s.footprint for s in group], box)
        if cov < min_coverage:
            continue
        out.append(Pass(satellite=sat, orbit=orbit, t=group[0].t,
                        scenes=group, coverage=cov))

    out.sort(key=lambda p: p.t)
    return out


def search(box, t0: datetime, t1: datetime, *,
           product_type: str | None = "GRD",
           min_coverage: float = 0.0,
           page: int = 1000,
           client: httpx.Client | None = None) -> list[Scene]:
    """Sentinel-1 acquisitions over the box, deduplicated.

    ANONYMOUS -- no token needed.

    Deduplicated by name, because the catalogue holds several records per
    acquisition (processing baselines, online and archived copies). They are
    the same photograph of the same water at the same second; counting them
    separately inflates the apparent sample size roughly threefold.
    """
    flt = (
        f"Collection/Name eq 'SENTINEL-1' and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt(box)}') and "
        f"ContentDate/Start gt {t0:%Y-%m-%dT%H:%M:%S}.000Z and "
        f"ContentDate/Start lt {t1:%Y-%m-%dT%H:%M:%S}.000Z"
    )

    own = client is None
    c = client or httpx.Client(timeout=90.0, follow_redirects=True)
    raw: list[dict] = []
    try:
        skip = 0
        while True:
            r = c.get(ODATA, params={"$filter": flt, "$top": page,
                                     "$skip": skip,
                                     "$orderby": "ContentDate/Start asc"})
            if r.status_code != 200:
                raise RuntimeError(f"catalogue HTTP {r.status_code}: "
                                   f"{r.text[:300]}")
            batch = r.json().get("value", [])
            raw += batch
            if len(batch) < page or skip > 20000:
                break
            skip += page
    finally:
        if own:
            c.close()

    # Deduplicate at the level of the ACQUISITION, not the filename.
    #
    # THE BUG THIS FIXES, which cost 4 GB and 10 minutes on the first real
    # download. Copernicus publishes each slice in two encodings:
    #
    #   S1D_IW_GRDH_1SDV_<start>_<stop>_004274_007DD6_26E5
    #   S1D_IW_GRDH_1SDV_<start>_<stop>_004274_007DD6_9F31_COG
    #
    # Same satellite, same orbit, same datatake, same thirty seconds of the
    # same water. Only the trailing product ID and format differ. Deduping by
    # full NAME let both through, so a six-slice pass was really three slices
    # fetched twice -- 8.4 GB where 4 would have done, and thirty passes would
    # have been 250 GB instead of 125.
    #
    # It would also have quietly corrupted the analysis: every vessel in the
    # overlap appears in two scenes at one instant, so matching.py would see
    # doubled detections and the dark-vessel count would be inflated by
    # whatever fraction of the archive happened to be duplicated.
    #
    # Fields 0..7 -- through the mission datatake ID -- identify the
    # acquisition. Everything after that is packaging.
    best: dict[str, dict] = {}
    for p in raw:
        name = p.get("Name", "")
        if not name:
            continue
        key = "_".join(name.split("_")[:8])
        held = best.get(key)
        if held is None:
            best[key] = p
        elif "_COG" in name and "_COG" not in held.get("Name", ""):
            # Prefer the COG: internally tiled with overviews, so the detector
            # can read windows without pulling the whole raster.
            best[key] = p

    out: list[Scene] = []
    for p in best.values():
        name = p["Name"]
        parts = name.split("_")
        ptype = parts[2][:3] if len(parts) > 2 else "?"
        if product_type and ptype != product_type:
            continue

        try:
            t = datetime.fromisoformat(
                p["ContentDate"]["Start"].replace("Z", "+00:00"))
        except Exception:
            continue

        fp = footprint_box(p)
        cov = overlap_fraction(fp, box)
        if cov < min_coverage:
            continue

        out.append(Scene(
            id=p.get("Id", ""),
            name=name,
            t=t,
            coverage=cov,
            size_bytes=int(p.get("ContentLength") or 0),
            online=bool(p.get("Online", True)),
            footprint=fp,
        ))

    out.sort(key=lambda s: s.t)
    return out


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------

# Hosts we are willing to send the access token to. See follow_authed below.
TRUSTED_HOSTS = (
    "dataspace.copernicus.eu",
    "copernicus.eu",
)


def _is_trusted(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS)


def follow_authed(client: httpx.Client, url: str, headers: dict,
                  *, max_hops: int = 10):
    """GET a URL, following redirects while KEEPING the Authorization header.

    THE BUG THIS EXISTS FOR, because it looks exactly like a wrong password.

    /Products(id)/$value does not serve the file. It answers 302 and points at
    a download node on a different host. httpx -- correctly, by default --
    STRIPS the Authorization header when a redirect crosses origins, because
    blindly forwarding credentials to wherever a server points you is how
    tokens leak. The second request therefore arrives unauthenticated and the
    node answers 401.

    So `--check` succeeds, the token is valid, and every download fails with
    what reads as an authentication problem. It is not: the credential was
    never sent.

    The fix is to follow the chain by hand and re-attach the header -- but
    ONLY for hosts in TRUSTED_HOSTS. Re-attaching unconditionally would
    reintroduce the exact vulnerability httpx was protecting against: a
    compromised or misconfigured redirect could walk away with a token that
    grants access to the account. An open redirect on any Copernicus host is a
    far smaller risk than one on an arbitrary host, and that is the trade
    being made deliberately rather than by accident.
    """
    for _ in range(max_hops):
        req = client.build_request("GET", url, headers=headers)
        r = client.send(req, stream=True, follow_redirects=False)

        if r.status_code not in (301, 302, 303, 307, 308):
            return r

        location = r.headers.get("location", "")
        r.close()
        if not location:
            raise RuntimeError(f"HTTP {r.status_code} with no Location header")

        url = urllib.parse.urljoin(url, location)      # may be relative
        if not _is_trusted(url):
            raise RuntimeError(
                f"redirected off Copernicus to {urllib.parse.urlsplit(url).hostname!r}; "
                f"refusing to forward the access token there"
            )

    raise RuntimeError(f"too many redirects (>{max_hops})")

def download(scene: Scene, dest_dir: Path, tokens: TokenManager, *,
             chunk: int = 1 << 20, max_attempts: int = 6,
             progress: bool = True) -> Path:
    """Fetch one product to dest_dir/<name>.zip, resuming if interrupted.

    Returns the finished path. A completed file is not re-downloaded.

    RESUME IS THE WHOLE POINT. The access token lasts about ten minutes and
    the product is about a gigabyte; on an ordinary connection the token dies
    mid-transfer. Without Range resume every such failure restarts from zero,
    and a fifty-scene fetch becomes impossible to run unattended.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"{scene.name}.zip"
    part = dest_dir / f"{scene.name}.zip.part"

    if final.exists() and final.stat().st_size > 0:
        log.info("already have %s", final.name)
        return final

    url = f"{ODATA}({scene.id})/$value"
    total = scene.size_bytes or 0

    for attempt in range(1, max_attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        if total and have >= total:
            break

        headers = {"Authorization": f"Bearer {tokens.token()}"}
        if have:
            headers["Range"] = f"bytes={have}-"

        try:
            # follow_redirects=False, because the redirect is followed by
            # follow_authed() instead -- httpx would strip the Authorization
            # header crossing to the download node and every request would
            # come back 401. See follow_authed's docstring.
            with httpx.Client(timeout=httpx.Timeout(60.0, read=300.0),
                              follow_redirects=False) as c:
                r = follow_authed(c, url, headers)
                # try/finally rather than `with r:` -- an httpx Response
                # returned by send(stream=True) is NOT a context manager, only
                # the client.stream() helper is. Using `with` here raised
                # TypeError on every attempt while the test suite stayed green,
                # because the fake Response in the tests implemented __enter__
                # and the real one does not. A double more capable than the
                # object it stands for tests nothing.
                try:
                    if r.status_code in (401, 403):
                        # A genuine auth failure now, not a stripped header.
                        tokens._expires = 0.0
                        raise RuntimeError(
                            f"auth rejected ({r.status_code}) by "
                            f"{r.url.host}")
                    if have and r.status_code == 200:
                        # Server ignored the Range header and is sending the
                        # whole file. Start over rather than appending a second
                        # copy onto the first -- silently corrupting the zip.
                        log.warning("range ignored; restarting %s", scene.name)
                        part.unlink(missing_ok=True)
                        have = 0
                    elif have and r.status_code != 206:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    elif not have and r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")

                    if not total:
                        cl = r.headers.get("content-length")
                        if cl:
                            total = have + int(cl)

                    mode = "ab" if have else "wb"
                    t_start = time.time()
                    with part.open(mode) as fh:
                        for block in r.iter_bytes(chunk):
                            fh.write(block)
                            have += len(block)
                            if progress and total:
                                pct = 100 * have / total
                                rate = (have / 1e6) / max(time.time() - t_start, 1e-3)
                                print(f"\r    {scene.name[:40]}  "
                                      f"{pct:5.1f}%  {have/1e6:7.0f}/"
                                      f"{total/1e6:.0f} MB  {rate:5.1f} MB/s",
                                      end="", flush=True)
                finally:
                    r.close()
            if progress:
                print()
            break

        except Exception as exc:
            if progress:
                print()
            if attempt >= max_attempts:
                raise
            wait = min(60, 2 ** attempt)
            log.warning("%s: %s -- retry %d/%d in %ds (keeping %.0f MB)",
                        scene.name, exc, attempt, max_attempts, wait,
                        (part.stat().st_size if part.exists() else 0) / 1e6)
            time.sleep(wait)

    part.replace(final)
    return final


def download_many(scenes: list[Scene], dest_dir: Path,
                  tokens: TokenManager | None = None, *,
                  progress: bool = True) -> Iterator[Path]:
    tokens = tokens or TokenManager()
    for i, s in enumerate(scenes, 1):
        if progress:
            print(f"  [{i}/{len(scenes)}] {s.t:%Y-%m-%d %H:%M}")
        yield download(s, dest_dir, tokens, progress=progress)


def months_ago(n: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=30.44 * n)
