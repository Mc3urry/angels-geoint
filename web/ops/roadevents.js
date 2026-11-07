// Road events from the CARS hubs (Indiana, Kansas, Minnesota, Nebraska),
// 2026-11-07, for both views. Iowa's hub redirects to the DOT's home page,
// so it is a named gap (angels/core/cars_hub.py, GAPS).
//
// DECLARED, NOT OBSERVED, AND NOT PHASE G. Each is what a state's 511
// system says about its roads (/roads/events; angels/core/cars_hub.py):
// an incident, a closure, roadwork, a restriction, the surface and the
// weather. The login that reads them stays on the server. They are shown
// for context and are not part of the registered land test, whose declared
// set is the WZDx work zones.
//
// The card is shared: the flat map and the globe both call
// roadEventCardData, so an event reads the same in either.

import * as I from "./icons.js";
import { get, esc } from "./api.js";
import { StaticPoints } from "./fleet.js";

const C = () => window.Cesium;

// Where the hubs are, for a view that should show them when turned on.
export const HUB_BOX = [-104.1, 36.9, -84.7, 49.4];

export const kindOf = (p) => I.ROAD_EVENT_KINDS[p.kind] || I.ROAD_EVENT_KINDS.other;

const when = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null
    : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};

export function roadEventCardData(p) {
  const k = kindOf(p);
  const dir = p.direction && p.direction !== "not directional" ? p.direction : "";
  return {
    title: p.headline || k.label,
    sub: [esc(p.road || "road not named"), esc(dir), esc(p.state)].filter(Boolean).join(" &middot; "),
    chip: k.color, glyph: I.roadEvent(),
    rows: [
      ["Kind", `${esc(k.label)}${p.planned ? " &middot; planned" : ""}`],
      ["Details", p.text ? esc(p.text) : null],
      ["From", esc(when(p.start) || "now")],
      ["Until", esc(when(p.end) || "not given")],
      ["Updated", when(p.updated) ? esc(when(p.updated)) : null],
    ],
    evidence: "<b>Declared</b> by the state's 511 system, through the CARS hub. Not observed. "
      + "Viewer only: not part of the registered land test.",
  };
}

// What the API says about each hub, in one line.
export function hubLine(props) {
  if (!props) return "";
  const src = Object.entries(props.sources || {});
  const bad = src.filter(([, s]) => s.error && !s.n).map(([st, s]) => `${st} ${s.error}`);
  const ok = src.filter(([, s]) => s.n).map(([st, s]) => `${st} ${s.n.toLocaleString()}`);
  const gaps = Object.keys(props.gaps || {});
  return [ok.length ? ok.join(", ") : "", bad.length ? `not answering: ${bad.join("; ")}` : "",
          gaps.length ? `no hub: ${gaps.join(", ")}` : ""]
    .filter(Boolean).join(". ");
}

// -- the globe ---------------------------------------------------------------------

export class RoadEvents {
  constructor(viewer) {
    const Cs = C();
    this.viewer = viewer;
    this.key = "rev";
    this.on = false;
    this.lines = viewer.scene.primitives.add(new Cs.PolylineCollection());
    this.pts = new StaticPoints(viewer, { px: 24, dotPx: 9, dotAbove: 1.5e6,
      scaler: new Cs.NearFarScalar(5e3, 1.1, 5e6, 0.6) });
    this.feats = [];
    this.meta = null;
  }

  // The whole set, whatever the region: the hubs are in the Midwest and
  // neither region box is.
  async load() {
    const Cs = C();
    const fc = await get("/roads/events");
    this.lines.removeAll(); this.pts.clear();
    this.feats = fc.features; this.meta = fc.properties;
    fc.features.forEach((f, i) => {
      const k = kindOf(f.properties);
      const id = { kind: "rev", layer: this, id: i };
      const g = f.geometry;
      if (g.type === "LineString") {
        this.lines.add({
          positions: Cs.Cartesian3.fromDegreesArrayHeights(g.coordinates.flatMap(([x, y]) => [x, y, 8])),
          width: 5, material: Cs.Material.fromType("Color", { color: Cs.Color.fromCssColorString(k.color).withAlpha(0.9) }),
          id,
        });
      }
      const at = this.lonlat(i);
      if (at) this.pts.add(at[0], at[1], 8, I.roadEvent(), k.color, id);
    });
    this.setOn(this.on);
    return fc.properties;
  }

  setOn(on) { this.on = on; this.lines.show = this.pts.show = on; }
  get count() { return this.on ? this.feats.length : 0; }
  lonlat(i) {
    const g = this.feats[i]?.geometry;
    return g ? (g.type === "Point" ? g.coordinates : g.coordinates[0]) : null;
  }
  position(i) {
    const p = this.lonlat(i);
    return p ? C().Cartesian3.fromDegrees(p[0], p[1], 8) : null;
  }
  card(i) {
    const p = this.feats[i]?.properties;
    return p ? roadEventCardData(p) : null;
  }
}
