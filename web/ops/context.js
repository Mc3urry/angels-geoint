// Context layers: satellites and ALPR camera locations. Earthquakes,
// wildfire perimeters and weather radar were removed on 2026-10-28, at
// Joshua's request.
//
// CONTEXT, NOT EVIDENCE. They orient the eye, as in God's Eye View, and no
// ANGELS analysis reads any of them. They come from this project's API
// (angels/api/routes/context.py), which fetches each public source at most
// once per TTL for every open tab, and answers 502 rather than an empty
// layer when a source is down: the panel then says "did not answer", never
// "0 cameras".

import * as I from "./icons.js";
import { get, esc } from "./api.js";

const C = () => window.Cesium;

function status(layer, text, kind = "") {
  layer.status = text;
  layer.statusKind = kind;
}

// -- satellites ----------------------------------------------------------------

export class Satellites {
  constructor(viewer) {
    this.viewer = viewer;
    this.key = "sat";
    this.on = false;
    this.sats = [];
    this.bbs = viewer.scene.primitives.add(new (C().BillboardCollection)({ scene: viewer.scene }));
    this.labels = viewer.scene.primitives.add(new (C().LabelCollection)({ scene: viewer.scene }));
    this.orbit = viewer.scene.primitives.add(new (C().PolylineCollection)());
    this.loadedAt = 0;
    status(this, "off");
  }

  async load() {
    if (Date.now() - this.loadedAt < 6 * 3600e3 && this.sats.length) return;
    if (!window.satellite) { status(this, "satellite.js did not load", "err"); return; }
    const doc = await get("/context/satellites");
    this.bbs.removeAll(); this.labels.removeAll();
    const Cs = C();
    this.sats = [];
    for (const s of doc.satellites) {
      let rec;
      try { rec = window.satellite.twoline2satrec(s.line1, s.line2); } catch { continue; }
      const grp = I.SAT_GROUPS[s.group] || I.SAT_GROUPS.visual;
      const it = { ...s, rec, norad: s.line1.slice(2, 7).trim(), color: grp.color };
      it.bb = this.bbs.add({
        image: I.satellite(), width: s.group === "stations" ? 26 : 18,
        height: s.group === "stations" ? 26 : 18,
        color: Cs.Color.fromCssColorString(grp.color),
        id: { kind: "sat", layer: this, id: it.norad },
        scaleByDistance: new Cs.NearFarScalar(1e6, 1.2, 4e7, 0.6),
      });
      if (s.group === "stations") {
        it.lbl = this.labels.add({
          text: s.name, font: "600 12px system-ui, sans-serif",
          fillColor: Cs.Color.WHITE, outlineColor: Cs.Color.BLACK, outlineWidth: 3,
          style: Cs.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cs.Cartesian2(16, 0),
          horizontalOrigin: Cs.HorizontalOrigin.LEFT,
        });
      }
      this.sats.push(it);
    }
    this.loadedAt = Date.now();
    status(this, `${this.sats.length} from CelesTrak`, "ok");
    this.tick();
  }

  eci(rec, date) {
    const sat = window.satellite;
    const pv = sat.propagate(rec, date);
    if (!pv || !pv.position) return null;
    const ecf = sat.eciToEcf(pv.position, sat.gstime(date));
    return { pos: new (C().Cartesian3)(ecf.x * 1000, ecf.y * 1000, ecf.z * 1000), vel: pv.velocity };
  }

  tick() {
    if (!this.on) return;
    const now = new Date();
    for (const s of this.sats) {
      const e = this.eci(s.rec, now);
      s.bb.show = !!e;
      if (!e) continue;
      s.bb.position = e.pos;
      s.vel = e.vel;
      if (s.lbl) s.lbl.position = e.pos;
    }
  }

  setOn(on) {
    this.on = on;
    this.bbs.show = this.labels.show = this.orbit.show = on;
    if (!on) status(this, "off");
  }

  get count() { return this.on ? this.sats.length : 0; }
  byId(id) { return this.sats.find((s) => s.norad === id); }

  position(id) {
    const s = this.byId(id);
    return s ? this.eci(s.rec, new Date())?.pos ?? null : null;
  }

  // One full orbit ahead, so a selected satellite shows where it is going.
  showOrbit(id) {
    this.orbit.removeAll();
    const s = this.byId(id);
    if (!s) return;
    const periodMin = (2 * Math.PI) / s.rec.no;
    const pts = [];
    const t0 = Date.now();
    for (let i = 0; i <= 180; i += 1) {
      const e = this.eci(s.rec, new Date(t0 + (i / 180) * periodMin * 60e3));
      if (e) pts.push(e.pos);
    }
    const Cs = C();
    this.orbit.add({
      positions: pts, width: 2,
      material: Cs.Material.fromType("PolylineGlow", {
        color: Cs.Color.fromCssColorString(s.color).withAlpha(0.9), glowPower: 0.18 }),
    });
  }

  card(id) {
    const s = this.byId(id);
    if (!s) return null;
    const sat = window.satellite;
    const e = this.eci(s.rec, new Date());
    let altKm = null;
    if (e) {
      const gd = sat.eciToGeodetic(sat.propagate(s.rec, new Date()).position, sat.gstime(new Date()));
      altKm = gd.height;
    }
    const v = s.vel ? Math.hypot(s.vel.x, s.vel.y, s.vel.z) : null;
    const periodMin = (2 * Math.PI) / s.rec.no;
    return {
      title: s.name, sub: `NORAD ${esc(s.norad)} · ${esc(I.SAT_GROUPS[s.group]?.label ?? s.group)}`,
      chip: s.color, glyph: I.satellite(),
      rows: [
        ["Altitude", altKm != null ? `${Math.round(altKm).toLocaleString()} km` : "—"],
        ["Speed", v != null ? `${v.toFixed(2)} km/s` : "—"],
        ["Inclination", `${(s.rec.inclo * 180 / Math.PI).toFixed(1)}°`],
        ["Period", `${periodMin.toFixed(1)} min`],
      ],
      evidence: "<b>Context, not evidence.</b> Position computed in this browser from CelesTrak's published elements (SGP4).",
    };
  }
}

// -- ALPR (viewer-only scope exception) ---------------------------------------
//
// THE WHOLE COUNTRY, LOADED ONCE. The first version asked for the view in
// front of the camera and only below 4 square degrees, so the default view
// never asked and the row read 0. Now the API fetches every mapped ALPR in
// the US once a day and the page draws them all; they appear as the camera
// comes down, because tens of thousands of hexagons over a continent say
// nothing a count does not.

export const ALPR_SHOW_BELOW_M = 1.5e6;

export class Alpr {
  constructor(viewer) {
    this.viewer = viewer;
    this.key = "alpr";
    this.on = false;
    this.bbs = viewer.scene.primitives.add(new (C().BillboardCollection)({ scene: viewer.scene }));
    this.rows = [];
    this.loadedAt = 0;
    status(this, "off");
  }

  async load() {
    if (this.rows.length && Date.now() - this.loadedAt < 6 * 3600e3) return;
    status(this, "asking OpenStreetMap for the whole country (can take a minute)", "warn");
    const Cs = C();
    const doc = await get("/context/alpr");
    this.bbs.removeAll();
    this.rows = doc.rows;
    const col = Cs.Color.fromCssColorString(I.COL.alpr);
    const img = I.alpr();
    const far = new Cs.DistanceDisplayCondition(0, ALPR_SHOW_BELOW_M);
    const scale = new Cs.NearFarScalar(2e3, 1.1, ALPR_SHOW_BELOW_M, 0.4);
    doc.rows.forEach((r, i) => {
      this.bbs.add({
        position: Cs.Cartesian3.fromDegrees(r[0], r[1], 2),
        image: img, width: 24, height: 24, color: col,
        id: { kind: "alpr", layer: this, id: i },
        distanceDisplayCondition: far, scaleByDistance: scale,
        disableDepthTestDistance: 3e6,
      });
    });
    this.bbs.show = this.on;
    this.loadedAt = Date.now();
    const stale = doc.stale_s ? `, ${Math.round(doc.stale_s / 3600)} h old` : "";
    status(this, `${doc.n.toLocaleString()} mapped in the US${stale}; shown below 1,500 km`, stale ? "warn" : "ok");
  }

  setOn(on) {
    this.on = on;
    this.bbs.show = on;
    if (!on) status(this, "off");
  }
  get count() { return this.on ? this.rows.length : 0; }
  position(i) {
    const r = this.rows[i];
    return r ? C().Cartesian3.fromDegrees(r[0], r[1], 2) : null;
  }

  card(i) {
    const r = this.rows[i];
    if (!r) return null;
    const [, , osm, operator, maker, facing] = r;
    return {
      title: "Licence-plate camera", sub: esc(operator || "operator not mapped"),
      chip: I.COL.alpr, glyph: I.alpr(),
      rows: [
        ["Maker", maker ? esc(maker) : null],
        ["Facing", facing ? esc(facing) : null],
      ],
      link: [`https://www.openstreetmap.org/node/${Number(osm)}`, `OSM node ${Number(osm)}`],
      evidence: "<b>Context, not evidence.</b> Where a volunteer mapped one, from public OpenStreetMap tags. Never what it reads. Shown under the viewer-only scope exception.",
    };
  }
}
