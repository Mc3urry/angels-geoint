// Session wakes: where a contact has been since you started watching.
//
// WHY THIS EXISTS, AND WHY IT IS NOT AN ARCHIVE
//
// The air layer can draw history because a collector has been writing state
// vectors to Parquet for weeks; /tracks reads that archive. The sea layer has
// no such thing, and cannot have one yet: aisstream's terms on STORING
// received positions have not been established, and this project does not
// persist third-party data on an assumption. That question is open on the
// checklist and is the user's to settle, not the code's.
//
// So this is the honest half that needs no answer. The browser already
// receives every position in order to draw it; remembering the last few
// while the tab is open is not storage, it is the map having a memory. It
// lives in RAM, it is never sent anywhere, and a reload erases it
// completely. What that buys is the thing a live map is actually for:
// watching something MOVE, and seeing at a glance which contacts are
// transiting and which have been sitting still.
//
// It is also a different KIND of line from the archive trail, and the panel
// says so rather than letting them look alike:
//
//   archive trail   what the collector recorded, including before you
//                   arrived. Coarse on the national box -- ten-minute polls.
//   session wake    what this tab has seen, at the feed's own cadence.
//                   Starts empty every time and is never longer than how
//                   long you have been looking.

// Caps, all three needed. A national AIS subscription can hold tens of
// thousands of vessels, and an unbounded history of all of them is a browser
// tab that grows until it dies.
const MAX_PLATFORMS = 2500;   // evicting the least recently updated
const MAX_POINTS = 40;        // per platform
const MAX_AGE_MS = 30 * 60e3; // a wake older than this is not "now"

// Below this a new point is jitter, not movement, and a stationary vessel
// would otherwise accumulate forty points of GPS noise and draw a scribble
// where it should draw nothing.
const MIN_STEP_M = 25;

const R = 6371008.8;

function metres(a, b) {
  const [lon1, lat1] = a, [lon2, lat2] = b;
  const p = Math.PI / 180;
  const dx = (lon2 - lon1) * p * Math.cos(((lat1 + lat2) / 2) * p);
  const dy = (lat2 - lat1) * p;
  return Math.hypot(dx, dy) * R;
}

export class Wake {
  constructor({ maxPlatforms = MAX_PLATFORMS, maxPoints = MAX_POINTS } = {}) {
    this.maxPlatforms = maxPlatforms;
    this.maxPoints = maxPoints;
    this.paths = new Map();          // id -> { pts: [[lon,lat,t]], props }
    this.dropped = 0;
  }

  clear() { this.paths.clear(); this.dropped = 0; }

  /** Feed the CURRENT set of contacts. `idKey` names the stable identity --
   *  icao24 at sea level, mmsi at sea. A feature with no identity is skipped
   *  rather than given a synthetic one: a wake assembled from whatever
   *  happened to be at that index last time would be a fabricated track, and
   *  a fabricated track on this map is the worst artefact it could produce. */
  feed(features, idKey, now = Date.now()) {
    for (const f of features) {
      const id = f.properties?.[idKey];
      if (id == null) continue;
      const c = f.geometry?.coordinates;
      if (!c || !Number.isFinite(c[0]) || !Number.isFinite(c[1])) continue;

      let path = this.paths.get(id);
      if (!path) {
        if (this.paths.size >= this.maxPlatforms) this.#evict();
        path = { pts: [], props: {} };
        this.paths.set(id, path);
      }
      // Re-inserting keeps the Map's iteration order as "least recently
      // updated first", which is what #evict relies on.
      this.paths.delete(id);
      this.paths.set(id, path);

      path.props = f.properties;
      const last = path.pts[path.pts.length - 1];
      if (!last || metres(last, c) >= MIN_STEP_M) {
        path.pts.push([c[0], c[1], now]);
        if (path.pts.length > this.maxPoints) path.pts.shift();
      }
    }
    this.#expire(now);
  }

  #evict() {
    const oldest = this.paths.keys().next().value;
    if (oldest !== undefined) { this.paths.delete(oldest); this.dropped += 1; }
  }

  #expire(now) {
    for (const [id, path] of this.paths) {
      path.pts = path.pts.filter((p) => now - p[2] <= MAX_AGE_MS);
      if (!path.pts.length) this.paths.delete(id);
    }
  }

  /** Only paths with somewhere to draw. A single point is a position, which
   *  the symbol layer is already showing; drawing it as a line of length
   *  zero just thickens the dot. */
  toGeoJSON(extra = () => ({})) {
    const features = [];
    for (const [id, path] of this.paths) {
      if (path.pts.length < 2) continue;
      features.push({
        type: "Feature",
        geometry: {
          type: "LineString",
          coordinates: path.pts.map((p) => [p[0], p[1]]),
        },
        properties: { id, n: path.pts.length, ...extra(path.props) },
      });
    }
    return { type: "FeatureCollection", features };
  }

  get stats() {
    let pts = 0, drawn = 0;
    for (const p of this.paths.values()) {
      pts += p.pts.length;
      if (p.pts.length >= 2) drawn += 1;
    }
    return { platforms: this.paths.size, drawn, points: pts,
             dropped: this.dropped, cap: this.maxPlatforms };
  }
}
