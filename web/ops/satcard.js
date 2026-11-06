// A satellite's card, for both views: computed in the browser from
// CelesTrak's published elements (SGP4, satellite.js), never fetched.

import { SAT_GROUPS, satellite as satGlyph } from "./icons.js";
import { esc } from "./api.js";

export function satNow(rec, date = new Date()) {
  const sat = window.satellite;
  const pv = sat.propagate(rec, date);
  if (!pv || !pv.position) return null;
  const gmst = sat.gstime(date);
  const gd = sat.eciToGeodetic(pv.position, gmst);
  return {
    lon: sat.degreesLong(gd.longitude), lat: sat.degreesLat(gd.latitude), altKm: gd.height,
    kms: pv.velocity ? Math.hypot(pv.velocity.x, pv.velocity.y, pv.velocity.z) : null,
    ecf: sat.eciToEcf(pv.position, gmst),
  };
}

export function satCardData(s, date = new Date()) {
  const n = satNow(s.rec, date);
  const periodMin = (2 * Math.PI) / s.rec.no;
  return {
    title: s.name, sub: `NORAD ${esc(s.norad)} · ${esc(SAT_GROUPS[s.group]?.label ?? s.group)}`,
    chip: s.color, glyph: satGlyph(),
    rows: [
      ["Altitude", n ? `${Math.round(n.altKm).toLocaleString()} km` : "—"],
      ["Speed", n?.kms != null ? `${n.kms.toFixed(2)} km/s` : "—"],
      ["Inclination", `${(s.rec.inclo * 180 / Math.PI).toFixed(1)}°`],
      ["Period", `${periodMin.toFixed(1)} min`],
    ],
    evidence: "<b>Context, not evidence.</b> Position computed in this browser from CelesTrak's published elements (SGP4).",
  };
}

// Where it will be over one orbit, as sub-points: the ground track.
export function groundTrack(rec, date = new Date(), steps = 180) {
  const periodMs = ((2 * Math.PI) / rec.no) * 60e3;
  const out = [];
  for (let i = 0; i <= steps; i += 1) {
    const n = satNow(rec, new Date(date.getTime() + (i / steps) * periodMs));
    if (n) out.push([n.lon, n.lat]);
  }
  return out;
}
