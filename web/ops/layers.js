// The evidence layers: what the cooperative feeds say, and where the state
// can see the road. Same endpoints as the 2D viewer, same honesty rules.
//
//   aircraft   /live?aoi=…               ADS-B via OpenSky (the collector's snapshot)
//   vessels    /live?domain=sea&aoi=…    AIS
//   transit    /vehicles/us              GTFS-RT, every keyless US feed
//   work zones /land/workzones?bbox=…    WZDx, declared (G1)
//   cameras    /cameras/us               traffic cameras, every state
//   rail       /vehicles/amtrak          Amtrak, VIA, Brightline
//   states     /land/states?bbox=…       the discontinuity the land question is about

import { Fleet } from "./fleet.js";
import * as I from "./icons.js";
import { get, esc } from "./api.js";
import { ageShort, freshness } from "../views/popup.js";

const C = () => window.Cesium;

export const REGIONS = {
  air: { label: "DC–Baltimore", box: [-77.7, 38.3, -76.0, 39.6],
         airMs: 10e3, seaMs: 10e3,
         view: { lon: -76.78, lat: 38.2, h: 150e3, pitch: -50 } },
  conus: { label: "CONUS", box: [-125.0, 24.0, -66.5, 49.5],
           airMs: 60e3, seaMs: 30e3,
           view: { lon: -96.5, lat: 19.5, h: 5.6e6, pitch: -68 } },
};

const kt = (mps) => (mps == null ? null : mps * 1.943844);
const ft = (m) => (m == null ? null : m * 3.28084);
export const compass = (deg) => (deg == null ? "—"
  : `${String(Math.round(deg) % 360).padStart(3, "0")}° ${
    ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8]}`);
export const ageText = (s) => (s == null ? "—" : s < 90 ? `${Math.round(s)} s`
  : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

const SQUAWK = { 7500: "unlawful interference", 7600: "radio failure", 7700: "emergency" };

// -- fleets ------------------------------------------------------------------

export function makeAir(viewer) {
  const f = new Fleet(viewer, {
    key: "air", domain: "air", px: 32, labelRange: 170e3,
    idOf: (p) => p.icao24,
    timeOf: (p, now) => (p.t ? p.t * 1000 : now),
    altOf: (p) => (p.on_ground ? 0 : Math.max(0, p.alt_m ?? 0)),
    labelOf: (p) => p.callsign,
    // The 2D viewer's symbol: a white plane on a disc in the altitude colour.
    style: (p) => {
      const sq = SQUAWK[Number(p.squawk)];
      const c = sq ? I.EMERGENCY : I.altColour(p.alt_m, p.on_ground);
      return { image: I.planeDisc(c), color: c, tint: "#ffffff", heading: p.heading,
               px: sq ? 38 : p.on_ground ? 24 : 30 };
    },
  });
  f.card = (it) => {
    const p = it.props;
    const sq = SQUAWK[Number(p.squawk)];
    const vr = p.vertical_rate;
    const age = it.now?.age;
    return {
      title: p.callsign, sub: `ICAO ${esc(p.icao24)}${p.country ? ` &middot; ${esc(p.country)}` : ""}`,
      chip: it.color, glyph: I.planeDisc(it.color),
      age: ageShort(age), ageState: freshness(age, 30, 120),
      rows: [
        sq ? ["Squawk", `${esc(p.squawk)} &middot; ${sq}`, { alarm: true }] : null,
        ["Altitude", p.on_ground ? "on the ground"
          : `${Math.round(ft(p.alt_m)).toLocaleString()} ft <span class="dim">${Math.round(p.alt_m).toLocaleString()} m</span>`,
          { strong: true }],
        ["Speed", p.speed_mps != null ? `${Math.round(kt(p.speed_mps))} kt` : null],
        ["Track", p.heading != null ? compass(p.heading) : null],
        ["Climb", vr == null ? null : Math.abs(vr) < 0.6 ? "level"
          : `${vr > 0 ? "climbing" : "descending"} ${Math.round(Math.abs(vr) * 196.85).toLocaleString()} ft/min`],
        sq ? null : ["Squawk", p.squawk ? esc(p.squawk) : null],
      ].filter(Boolean),
      evidence: "<b>Self-reported</b> by the aircraft's transponder (ADS-B, via OpenSky). Nothing independent saw it.",
    };
  };
  return f;
}

export function makeSea(viewer) {
  const f = new Fleet(viewer, {
    key: "sea", domain: "sea", px: 30, labelRange: 120e3,
    idOf: (p) => p.mmsi || p.id,
    timeOf: (p, now) => (p.t ? p.t * 1000 : now - (p.age_s || 0) * 1000),
    labelOf: (p) => p.name || p.label || p.mmsi,
    // The 2D viewer's symbols: a hull in the group colour under way, a dot
    // when stopped; undeclared is hollow.
    style: (p) => {
      const g = I.SEA_GROUP_OF[p.subtype] || I.SEA_GROUP_OF.unknown;
      const moving = (p.sog_kn ?? 0) > 0.5 && p.heading != null;
      return moving
        ? { image: I.hullFilled(g.color, !!g.hollow), color: g.color, tint: "#ffffff", heading: p.heading, px: 30 }
        : { image: I.dotFilled(g.color, !!g.hollow), color: g.color, tint: "#ffffff", heading: null, px: 18 };
    },
  });
  f.groupOf = (p) => (I.SEA_GROUP_OF[p.subtype] || I.SEA_GROUP_OF.unknown).key;
  f.card = (it) => {
    const p = it.props;
    const g = I.SEA_GROUP_OF[p.subtype] || I.SEA_GROUP_OF.unknown;
    const size = [p.length_m && `${Math.round(p.length_m)}&nbsp;m`, p.width_m && `${Math.round(p.width_m)}&nbsp;m beam`,
      p.draught_m && `${Number(p.draught_m).toFixed(1)}&nbsp;m draught`].filter(Boolean).join(" &middot; ");
    const kn = p.sog_kn;
    const age = it.now?.age;
    return {
      title: p.name || p.label || p.mmsi,
      sub: [`MMSI ${esc(p.mmsi)}`, p.call_sign && esc(p.call_sign), p.ais_class && `class ${esc(p.ais_class)}`]
        .filter(Boolean).join(" &middot; "),
      chip: g.color, glyph: I.hullFilled(g.color, !!g.hollow),
      age: ageShort(age), ageState: freshness(age, 180, 600),
      rows: [
        ["Type", `${g.label}${p.subtype && g.key !== "unknown" ? ` &middot; ${esc(p.subtype)}` : ""}`],
        ["Speed", kn == null ? null : kn <= 0.5 ? `${Number(kn).toFixed(1)}&nbsp;kn <span class="dim">(stopped)</span>`
          : `${Number(kn).toFixed(1)}&nbsp;kn &middot; ${compass(p.heading)}`],
        ["Size", size || null],
        ["Bound for", p.destination ? esc(p.destination) : null],
      ],
      evidence: "<b>Self-reported</b> by the vessel (AIS). Nothing independent saw it.",
    };
  };
  return f;
}

// Transit across the US, from /vehicles/us: the research collector's 38
// border feeds plus every other keyless US feed, polled by the API only
// while this view asks (angels/api/routes/vehicles.py). Rows arrive compact
// and main.js turns them into features with these properties.
export function makeLand(viewer) {
  const modeOf = (p) => I.MODE_OF_FEED[p.feed_id] || "bus";
  const f = new Fleet(viewer, {
    key: "land", domain: "land", px: 26, labelRange: 25e3, near: 5e3, far: 6e5,
    idOf: (p) => `${p.feed_id}:${p.vehicle_key || p.label}`,
    timeOf: (p, now) => (p.fix_t ? p.fix_t * 1000 : now - (p.age_s || 0) * 1000),
    labelOf: (p) => (p.route_id ? `${p.route_id}` : p.label || ""),
    style: (p) => {
      const m = I.MODES[modeOf(p)];
      return p.in_service
        ? { image: m.glyph(), color: m.color, heading: null, px: 26,
            tick: p.bearing != null ? I.headingTick() : null }
        : { image: m.glyph(), color: I.NO_TRIP, heading: null, px: 20, alpha: 0.75 };
    },
  });
  f.modeOf = modeOf;
  f.card = (it) => {
    const p = it.props;
    const m = I.MODES[modeOf(p)];
    const age = it.now?.age;
    return {
      title: p.provider || p.feed_id,
      sub: [p.label && `vehicle ${esc(p.label)}`, p.route_id && `route ${esc(p.route_id)}`]
        .filter(Boolean).join(" &middot; ") || esc(p.feed_id),
      chip: p.in_service ? m.color : I.NO_TRIP, glyph: m.glyph(),
      age: ageShort(age), ageState: freshness(age, 60, 300),
      rows: [
        ["Service", p.in_service ? `${m.label.toLowerCase()} on a trip` : "no trip: usually parked"],
        ["Speed", p.speed_mps != null ? `${Math.round(p.speed_mps * 3.6)} km/h &middot; ${compass(p.bearing)}` : null],
        ["Clock", p.age_from === "vehicle" ? "the vehicle's own" : "our poll's; the feed sent none"],
        ["Motion", p.motion === "derived" ? "derived from the previous poll"
          : p.motion === "none" ? "none sent: shown where reported" : "the vehicle's own"],
      ],
      evidence: "<b>Self-reported</b> by the agency's vehicle (GTFS-RT). Nothing independent saw it.",
    };
  };
  return f;
}

// -- intercity rail -------------------------------------------------------------
//
// Amtrak, VIA Rail and Brightline, from /vehicles/amtrak (Amtraker, over
// Amtrak's own map). A train reports every few minutes, so it is projected
// along its heading for up to five minutes, not ninety seconds.

export const RAIL_COLOR = "#1d4f91";

export function makeRail(viewer) {
  const f = new Fleet(viewer, {
    key: "rail", domain: "land", horizon: 300, px: 30, labelRange: 400e3, near: 1e4, far: 6e6,
    idOf: (p) => p.id,
    timeOf: (p, now) => (p.ts ? p.ts * 1000 : now),
    labelOf: (p) => (p.number ? `${p.provider === "Amtrak" ? "" : `${p.provider} `}${p.number}` : ""),
    style: (p) => ({ image: I.rail(), color: RAIL_COLOR, heading: null, px: 30,
                     tick: p.heading != null ? I.headingTick() : null }),
  });
  f.card = (it) => {
    const p = it.props;
    const age = it.now?.age;
    return {
      title: `${p.route || p.provider} ${p.number || ""}`.trim(),
      sub: [esc(p.provider), p.origin && p.destination ? `${esc(p.origin)} to ${esc(p.destination)}` : null]
        .filter(Boolean).join(" &middot; "),
      chip: RAIL_COLOR, glyph: I.rail(),
      age: ageShort(age), ageState: freshness(age, 300, 900),
      rows: [
        ["Speed", p.speed_mps != null ? `${Math.round(p.speed_mps / 0.44704)} mph &middot; ${compass(p.heading)}` : null],
        ["Train", esc(p.id)],
      ],
      evidence: "<b>Self-reported</b> by the railway's own train tracking (via Amtraker). Nothing independent saw it.",
    };
  };
  return f;
}

// -- DOT snowplows ------------------------------------------------------------------

export function makePlows(viewer) {
  const f = new Fleet(viewer, {
    key: "plow", domain: "land", horizon: 180, px: 26, labelRange: 60e3, near: 5e3, far: 3e6,
    idOf: (p) => p.id,
    timeOf: (p, now) => (p.ts ? p.ts * 1000 : now),
    labelOf: (p) => p.label || "",
    style: (p) => ({ image: I.plow(), color: I.COL.plow, heading: null, px: 26,
                     tick: p.heading != null ? I.headingTick() : null }),
  });
  f.card = (it) => {
    const p = it.props;
    const age = it.now?.age;
    return {
      title: `${p.agency} ${p.label}`.trim(), sub: `${esc(p.state)} &middot; winter operations`,
      chip: I.COL.plow, glyph: I.plow(),
      age: ageShort(age), ageState: freshness(age, 180, 600),
      rows: [["Speed", p.speed_mps != null ? `${Math.round(p.speed_mps / 0.44704)} mph &middot; ${compass(p.heading)}` : null]],
      evidence: "<b>Self-reported</b> by the state's own fleet tracking. Nothing independent saw it.",
    };
  };
  return f;
}

// -- work zones --------------------------------------------------------------

export class WorkZones {
  constructor(viewer) {
    const Cs = C();
    this.viewer = viewer;
    this.key = "wz";
    this.on = false;
    this.lines = viewer.scene.primitives.add(new Cs.PolylineCollection());
    this.bbs = viewer.scene.primitives.add(new Cs.BillboardCollection({ scene: viewer.scene }));
    this.feats = [];
    this.meta = null;
  }

  async load(region) {
    const Cs = C();
    const fc = await get(`/land/workzones?bbox=${REGIONS[region].box.join(",")}`);
    this.lines.removeAll(); this.bbs.removeAll();
    this.feats = fc.features; this.meta = fc.properties;
    const col = Cs.Color.fromCssColorString(I.COL.workzone);
    const mat = Cs.Material.fromType("PolylineGlow", { color: col.withAlpha(0.95), glowPower: 0.22 });
    fc.features.forEach((f, i) => {
      const g = f.geometry;
      const lines = g.type === "LineString" ? [g.coordinates]
        : g.type === "MultiLineString" ? g.coordinates : [];
      const id = { kind: "wz", layer: this, id: i };
      for (const ln of lines) {
        if (ln.length < 2) continue;
        this.lines.add({
          positions: Cs.Cartesian3.fromDegreesArrayHeights(ln.flatMap(([x, y]) => [x, y, 6])),
          width: 7, material: mat, id,
        });
      }
      const at = g.type === "Point" ? g.coordinates : lines[0]?.[Math.floor(lines[0].length / 2)];
      if (at) {
        this.bbs.add({
          position: Cs.Cartesian3.fromDegrees(at[0], at[1], 6),
          image: I.workzone(), width: 22, height: 22, color: col, id,
          disableDepthTestDistance: 3e6,
          scaleByDistance: new Cs.NearFarScalar(5e3, 1.1, 5e6, 0.45),
        });
      }
    });
    this.setOn(this.on);
  }

  setOn(on) { this.on = on; this.lines.show = this.bbs.show = on; }
  get count() { return this.on ? this.feats.length : 0; }
  position(i) {
    const g = this.feats[i]?.geometry;
    if (!g) return null;
    const p = g.type === "Point" ? g.coordinates : g.type === "LineString" ? g.coordinates[0] : g.coordinates[0]?.[0];
    return p ? C().Cartesian3.fromDegrees(p[0], p[1], 6) : null;
  }
  lonlat(i) {
    const g = this.feats[i]?.geometry;
    const p = g?.type === "Point" ? g.coordinates : g?.type === "LineString" ? g.coordinates[0] : g?.coordinates?.[0]?.[0];
    return p || null;
  }

  card(i) {
    const p = this.feats[i]?.properties;
    if (!p) return null;
    return {
      title: "Work zone", sub: `${esc(p.road || "road not named")} ${esc(p.direction || "")}`,
      chip: I.COL.workzone, glyph: I.workzone(),
      rows: [
        ["Impact", p.impact ? esc(p.impact) : null],
        ["Dates", `${esc((p.start || "").slice(0, 10) || "?")} to ${esc((p.end || "").slice(0, 10) || "open")}`],
        ["Feed", esc(p.feed)],
      ],
      evidence: "<b>Declared</b> by the agency (WZDx). Not observed.",
    };
  }
}

// -- traffic cameras, the whole US --------------------------------------------
//
// From /cameras/us: Maryland CHART, Caltrans, WSDOT, TxDOT, Austin, DelDOT,
// King County, and LiveTrafficCam for every other state (angels/core/
// cameras_us.py). Compact rows; the API answers at once and fills in while
// its sources load, so the page asks again until nothing is loading.

const CAM = Object.fromEntries(["id", "lon", "lat", "name", "src", "kind", "media", "page", "state", "note"]
  .map((c, i) => [c, i]));

export class Cameras {
  constructor(viewer) {
    const Cs = C();
    this.viewer = viewer;
    this.key = "cam";
    this.on = false;
    this.bbs = viewer.scene.primitives.add(new Cs.BillboardCollection({ scene: viewer.scene }));
    this.rows = [];
    this.meta = null;
    this.ids = new Set();
  }

  // Video (a live stream or the publisher's player) and still frames are
  // told apart, because "watch" means something different for each.
  static colour(kind) { return kind === "image" ? I.COL.camNone : I.COL.camStream; }

  async load() {
    const Cs = C();
    const doc = await get("/cameras/us");
    this.meta = doc;
    const img = I.camera();
    const scale = new Cs.NearFarScalar(3e3, 1.1, 5e6, 0.35);
    for (const r of doc.rows) {
      if (this.ids.has(r[CAM.id])) continue;
      this.ids.add(r[CAM.id]);
      const i = this.rows.push(r) - 1;
      this.bbs.add({
        position: Cs.Cartesian3.fromDegrees(r[CAM.lon], r[CAM.lat], 8),
        image: img, width: 22, height: 22,
        color: Cs.Color.fromCssColorString(Cameras.colour(r[CAM.kind])),
        id: { kind: "cam", layer: this, id: i },
        disableDepthTestDistance: 3e6, scaleByDistance: scale,
      });
    }
    this.setOn(this.on);
    return doc;
  }

  setOn(on) { this.on = on; this.bbs.show = on; }
  get count() { return this.on ? this.rows.length : 0; }
  position(i) {
    const r = this.rows[i];
    return r ? C().Cartesian3.fromDegrees(r[CAM.lon], r[CAM.lat], 8) : null;
  }
  row(i) {
    const r = this.rows[i];
    return r ? Object.fromEntries(Object.entries(CAM).map(([k, j]) => [k, r[j]])) : null;
  }

  card(i) {
    const c = this.row(i);
    if (!c) return null;
    return {
      title: c.name || "Traffic camera",
      sub: [esc(c.src), esc(c.state), c.note ? esc(c.note) : null].filter(Boolean).join(" &middot; "),
      chip: Cameras.colour(c.kind), glyph: I.camera(),
      rows: [
        ["Picture", c.kind === "hls" ? "live video stream" : c.kind === "player" ? "the publisher's live player"
          : "still frame, refreshed by the publisher"],
      ],
      watch: c.media ? { i } : null,
      link: c.page ? [c.page, "Publisher's page"] : null,
      evidence: `Where <b>the state</b> can see the road. Played from ${esc(c.src)}'s own servers; nothing is recorded here.`,
    };
  }
}

// -- state lines -------------------------------------------------------------

export class States {
  constructor(viewer) {
    this.viewer = viewer;
    this.key = "states";
    this.on = true;
    this.lines = viewer.scene.primitives.add(new (C().PolylineCollection)());
    this.n = 0;
  }

  async load(region) {
    const Cs = C();
    const fc = await get(`/land/states?bbox=${REGIONS[region].box.join(",")}`);
    this.lines.removeAll();
    const mat = Cs.Material.fromType("PolylineDash", {
      // The 2D viewer's dark dash, on a pale gap so it reads on imagery too.
      color: Cs.Color.fromCssColorString(I.COL.state), gapColor: Cs.Color.WHITE.withAlpha(0.75), dashLength: 18 });
    const rings = [];
    for (const f of fc.features) {
      const g = f.geometry || {};
      if (g.type === "LineString") rings.push(g.coordinates);
      else if (g.type === "MultiLineString" || g.type === "Polygon") rings.push(...g.coordinates);
      else if (g.type === "MultiPolygon") for (const poly of g.coordinates) rings.push(...poly);
    }
    for (const r of rings) {
      if (r.length < 2) continue;
      this.lines.add({
        positions: Cs.Cartesian3.fromDegreesArrayHeights(r.flatMap(([x, y]) => [x, y, 4])),
        width: 2.6, material: mat,
      });
    }
    this.n = fc.features.length;
    this.setOn(this.on);
  }

  setOn(on) { this.on = on; this.lines.show = on; }
  get count() { return this.on ? this.n : 0; }
}
