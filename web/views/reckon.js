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

export function reckonFleet(fleet, now, domain = "air") {
  const horizon = HORIZON_S[domain] ?? 120;
  return {
    type: "FeatureCollection",
    features: fleet.map((a) => {
      let coords = a.coords;
      const age = (now - a.t) / 1000;
      // A vessel stopped at a berth has speed 0 and no meaningful heading.
      // Projecting it anywhere would be inventing motion, so the speed test
      // is a real test and not a null guard.
      if (a.speed_mps > 0.5 && a.heading != null && age > 0 && age < horizon) {
        coords = project(a.coords[1], a.coords[0], a.heading, a.speed_mps * age);
      }
      return {
        type: "Feature",
        geometry: { type: "Point", coordinates: coords },
        properties: { ...a.props, stale: age > horizon },
      };
    }),
  };
}
