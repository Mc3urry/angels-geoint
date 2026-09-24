// Dead reckoning, shared by every live domain.
//
// Extracted from live.js when the sea layer arrived. The technique is
// identical for an aircraft and a vessel -- project the last fix forward
// along its heading at its speed -- and the only thing that differs is how
// long the guess stays worth making. Keeping one copy means a fix to the
// spherical maths cannot land in one domain and not the other.
//
// It is also the project's own namesake technique pointed at its own map:
// estimating where something must be from its last fix plus motion, with no
// external reference.

const R = 6371008.8;

export function project(lat, lon, bearingDeg, distM) {
  const d = distM / R;
  const b = (bearingDeg * Math.PI) / 180;
  const p1 = (lat * Math.PI) / 180;
  const l1 = (lon * Math.PI) / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1),
                             Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [((l2 * 180) / Math.PI + 540) % 360 - 180, (p2 * 180) / Math.PI];
}

// How long a guess stays better than admitting we do not know.
//
// The two numbers are different because the reporting intervals are. ADS-B
// arrives every second or so, and a 120s silence from an aircraft means
// something has gone wrong -- keep flying it and it crosses the map. AIS from
// a vessel under way arrives every 2-10s but from one at anchor only every 3
// minutes, and an anchored vessel is not going anywhere, so a longer horizon
// costs nothing and stops the harbour flickering.
export const HORIZON_S = { air: 120, sea: 300 };

// WHEN PROJECTING IS THE WRONG ANSWER
//
// Dead reckoning is only honest while the guess is better than admitting we
// do not know, and that depends entirely on how old the fix can be. The
// DC-Baltimore box is polled every 30 s, so an aircraft has moved about 2 km
// and the projection is close. The national box is polled every TEN MINUTES:
// the same aircraft has moved 150 km, and a projected symbol would be a
// confident drawing of a place it demonstrably is not -- looking exactly as
// certain as the 30 s one.
//
// So the caller can turn projection off with `{ project: false }`, and each
// feature then carries `uncertainty_m`: how far the thing could have gone
// since it was last heard. The map draws THAT instead of a false position.
// The server decides which mode applies (`dead_reckon` in the /live payload)
// rather than the client inferring it from the cadence.
export function reckonFleet(fleet, now, domain = "air", opts = {}) {
  const horizon = opts.horizon ?? HORIZON_S[domain] ?? 120;
  const projecting = opts.project !== false;
  return {
    type: "FeatureCollection",
    features: fleet.map((a) => {
      let coords = a.coords;
      const age = (now - a.t) / 1000;
      // A vessel stopped at a berth has speed 0 and no meaningful heading.
      // Projecting it anywhere would be inventing motion, so the speed test
      // is a real test and not a null guard.
      if (projecting && a.speed_mps > 0.5 && a.heading != null
          && age > 0 && age < horizon) {
        coords = project(a.coords[1], a.coords[0], a.heading, a.speed_mps * age);
      }
      return {
        type: "Feature",
        geometry: { type: "Point", coordinates: coords },
        properties: {
          ...a.props,
          stale: age > horizon,
          age_s: Math.max(0, Math.round(age)),
          // Zero while projecting: the symbol is already the best estimate.
          // Otherwise the radius of what we genuinely do not know.
          uncertainty_m: projecting
            ? 0
            : Math.max(0, (a.speed_mps || 0) * Math.min(age, horizon)),
        },
      };
    }),
  };
}
