// A moving fleet on the globe: aircraft, vessels or transit vehicles.
//
// One class for all three because the honest parts are the same for all
// three, and the 2D viewer learnt that the hard way (reckon.js):
//
//   * a symbol is drawn where the contact SAID it was, projected forward
//     along its own heading at its own speed for at most HORIZON seconds,
//     and not at all when it sent no speed or when the payload says the
//     cadence is too slow for projection to be honest (CONUS, 10 min);
//   * past the horizon it is drawn faint where it was last heard;
//   * the trail behind it is the fixes it REPORTED, kept in this tab only.
//     Nothing about a contact is ever written to disk by this view (README,
//     "Viewer-only exception").
//
// Contacts are reconciled by id, not redrawn: a poll moves the billboards
// that exist, adds the new and removes the gone, so a tracked aircraft does
// not flicker every ten seconds and the camera lock survives a poll.

import { project, HORIZON_S } from "../views/reckon.js";

const TRAIL_MAX = 60;
let scratch = null;                  // fixes per contact
const TRAIL_MAX_AGE_MS = 15 * 60e3;

export class Fleet {
  constructor(viewer, o) {
    const Cesium = window.Cesium;
    this.C = Cesium;
    this.viewer = viewer;
    this.key = o.key;
    this.domain = o.domain;
    this.horizon = o.horizon ?? HORIZON_S[o.domain] ?? 120;
    this.idOf = o.idOf;
    this.timeOf = o.timeOf;
    this.style = o.style;               // (props) -> {image, color, heading|null, px, alpha?, tick?}
    this.labelOf = o.labelOf;
    this.altOf = o.altOf || (() => 0);
    this.labelRange = o.labelRange ?? 160e3;
    this.basePx = o.px ?? 30;
    this.near = o.near ?? 2e4;
    this.maxLabels = o.maxLabels ?? 25;
    this.far = o.far ?? 8e6;
    const scene = viewer.scene;
    this.trails = scene.primitives.add(new Cesium.PolylineCollection());
    this.bbs = scene.primitives.add(new Cesium.BillboardCollection({ scene }));
    this.labels = scene.primitives.add(new Cesium.LabelCollection({ scene }));
    this.items = new Map();
    this.shown = true;
    this.showLabels = true;
    this.showTrails = true;
    this.projecting = true;
    this.labelsLive = false;
    this.filter = null;                 // (props) -> bool
    this.meta = null;
    this.onGone = null;
    this.colorCache = new Map();
  }

  colour(css, alpha = 1) {
    const k = css + alpha;
    let c = this.colorCache.get(k);
    if (!c) {
      c = this.C.Color.fromCssColorString(css).withAlpha(alpha);
      this.colorCache.set(k, c);
    }
    return c;
  }

  get size() { return this.items.size; }

  countVisible() {
    let n = 0;
    for (const it of this.items.values()) if (it.bb.show) n += 1;
    return n;
  }

  ingest(fc, nowMs = Date.now()) {
    const C = this.C;
    this.meta = fc.properties || {};
    // The server decides whether projection is honest at this cadence.
    this.projecting = this.meta.dead_reckon !== false;
    const seen = new Set();
    for (const f of fc.features || []) {
      const p = f.properties || {};
      const id = String(this.idOf(p));
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const [lon, lat] = f.geometry.coordinates;
      const t = this.timeOf(p, nowMs);
      const alt = this.altOf(p);
      let it = this.items.get(id);
      if (!it) {
        it = { id, hist: [], bb: null, tick: null, lbl: null, trail: null, styleKey: "" };
        it.bb = this.bbs.add({
          position: C.Cartesian3.fromDegrees(lon, lat, alt),
          id: { kind: "fleet", layer: this, id },
          scaleByDistance: new C.NearFarScalar(this.near, 1.0, this.far, 0.5),
          disableDepthTestDistance: 3e6,
        });
        this.items.set(id, it);
      } else if (it.lon !== lon || it.lat !== lat) {
        // A new REPORTED position: the old one joins the trail.
        it.hist.push([it.lon, it.lat, it.alt, it.t]);
        if (it.hist.length > TRAIL_MAX) it.hist.shift();
      }
      it.lon = lon; it.lat = lat; it.alt = alt; it.t = t;
      it.heading = p.heading ?? p.bearing ?? null;
      it.speed = p.speed_mps ?? null;
      it.props = p;
      this.restyle(it);
    }
    for (const [id, it] of this.items) {
      if (!seen.has(id)) this.drop(id, it);
    }
    this.tick(nowMs);
  }

  drop(id, it) {
    this.bbs.remove(it.bb);
    if (it.tick) this.bbs.remove(it.tick);
    if (it.lbl) this.labels.remove(it.lbl);
    if (it.trail) this.trails.remove(it.trail);
    this.items.delete(id);
    if (this.onGone) this.onGone(this, id);
  }

  restyle(it) {
    const C = this.C;
    const s = this.style(it.props);
    it.color = s.color;               // what it means: trails, cards
    it.tint = s.tint ?? s.color;      // what multiplies the glyph
    it.rotates = s.heading != null;
    const k = `${s.image}|${s.color}|${s.px}|${s.tick ? 1 : 0}`;
    it.alpha = s.alpha ?? 1;
    if (k === it.styleKey) return;
    it.styleKey = k;
    const px = s.px ?? this.basePx;
    it.bb.image = s.image;
    it.bb.width = px; it.bb.height = px;
    it.bb.alignedAxis = it.rotates ? C.Cartesian3.UNIT_Z : C.Cartesian3.ZERO;
    if (s.tick && !it.tick) {
      it.tick = this.bbs.add({
        image: s.tick, width: px, height: px,
        id: { kind: "fleet", layer: this, id: it.id },
        alignedAxis: C.Cartesian3.UNIT_Z,
        scaleByDistance: new C.NearFarScalar(this.near, 1.0, this.far, 0.5),
        disableDepthTestDistance: 3e6,
      });
    } else if (!s.tick && it.tick) {
      this.bbs.remove(it.tick); it.tick = null;
    }
  }

  // Where the contact is now, best estimate, and whether that is a guess.
  where(it, nowMs = Date.now()) {
    const age = (nowMs - it.t) / 1000;
    let lon = it.lon, lat = it.lat;
    if (this.projecting && it.speed > 0.5 && it.heading != null && age > 0 && age < this.horizon) {
      [lon, lat] = project(it.lat, it.lon, it.heading, it.speed * age);
    }
    return { lon, lat, alt: it.alt, age, stale: age > this.horizon };
  }

  position(id, nowMs = Date.now()) {
    const it = this.items.get(id);
    if (!it) return null;
    const w = this.where(it, nowMs);
    return this.C.Cartesian3.fromDegrees(w.lon, w.lat, w.alt);
  }

  tick(nowMs = Date.now()) {
    const C = this.C;
    for (const it of this.items.values()) {
      const visible = this.shown && (!this.filter || this.filter(it.props));
      it.bb.show = visible;
      if (it.tick) it.tick.show = visible && it.heading != null;
      if (!visible) {
        if (it.lbl) it.lbl.show = false;
        if (it.trail) it.trail.show = false;
        continue;
      }
      const w = this.where(it, nowMs);
      it.now = w;
      // A contact that has not moved keeps its position: rewriting five
      // thousand unchanged billboards four times a second costs a frame.
      let pos = it.bb.position;
      if (w.lon !== it._lon || w.lat !== it._lat || w.alt !== it._alt) {
        pos = C.Cartesian3.fromDegrees(w.lon, w.lat, w.alt);
        it.bb.position = pos;
        it._lon = w.lon; it._lat = w.lat; it._alt = w.alt;
      }
      const alpha = w.stale ? Math.min(it.alpha, 0.35) : it.alpha;
      it.bb.color = this.colour(it.tint, alpha);
      const rot = it.heading != null ? -C.Math.toRadians(it.heading) : 0;
      if (it.rotates) it.bb.rotation = rot;
      if (it.tick) { it.tick.position = pos; it.tick.rotation = rot; it.tick.color = this.colour(it.color, alpha); }
      if (it.lbl) it.lbl.position = pos;     // shown or not by labelPass
      if (it.trail) it.trail.show = this.showTrails;
    }
  }

  // Labels exist only while the camera is low enough to read them: five
  // thousand labels over a continent are a texture, not information.
  labelPass(cameraHeight, camera) {
    const C = this.C;
    const want = this.shown && this.showLabels && cameraHeight < this.labelRange * 1.6;
    if (!want) {
      if (this.labelsLive) {
        this.labels.removeAll();
        for (const it of this.items.values()) it.lbl = null;
        this.labelsLive = false;
      }
      return;
    }
    this.labelsLive = true;
    scratch = scratch || new C.Cartesian3();
    // ONLY THE NEAREST FEW. Every contact in range labelled was a wall of
    // route numbers once the camera came down to a city (2026-10-28), so a
    // fleet labels its `maxLabels` contacts nearest the camera, and the
    // rest are named on hover.
    const near = [];
    for (const it of this.items.values()) {
      if (!it.bb.show || !this.labelOf(it.props)) { if (it.lbl) it.lbl.show = false; continue; }
      // Nearest among those IN VIEW: the cone the camera looks down.
      const v = C.Cartesian3.subtract(it.bb.position, camera.positionWC, scratch);
      const d = C.Cartesian3.magnitudeSquared(v);
      const inView = d > 0 && C.Cartesian3.dot(v, camera.directionWC) / Math.sqrt(d) > 0.55;
      if (!inView || d > this.labelRange * this.labelRange) { if (it.lbl) it.lbl.show = false; continue; }
      near.push([d, it]);
    }
    near.sort((x, y) => x[0] - y[0]);
    const keep = new Set(near.slice(0, this.maxLabels).map(([, it]) => it));
    for (const [, it] of near) {
      if (!keep.has(it)) { if (it.lbl) it.lbl.show = false; continue; }
      const text = this.labelOf(it.props);
      if (!it.lbl) {
        // The 2D viewer's label: dark mono on a white halo.
        it.lbl = this.labels.add({
          text,
          font: "600 11px ui-monospace, Menlo, Consolas, monospace",
          fillColor: C.Color.fromCssColorString("#0e1a24"),
          outlineColor: C.Color.WHITE,
          outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE,
          verticalOrigin: C.VerticalOrigin.TOP,
          pixelOffset: new C.Cartesian2(0, (it.bb.height || this.basePx) / 2 + 3),
          disableDepthTestDistance: 3e6,
          position: it.bb.position,
          id: { kind: "fleet", layer: this, id: it.id },
        });
      } else if (it.lbl.text !== text) {
        it.lbl.text = text;
      }
      it.lbl.position = it.bb.position;
      it.lbl.show = true;
    }
  }

  // Trails: the reported fixes, oldest first, then the symbol itself.
  trailPass(nowMs = Date.now()) {
    const C = this.C;
    for (const it of this.items.values()) {
      while (it.hist.length && nowMs - it.hist[0][3] > TRAIL_MAX_AGE_MS) it.hist.shift();
      const on = this.shown && this.showTrails && it.bb.show && it.hist.length > 0;
      if (!on) {
        if (it.trail) it.trail.show = false;
        continue;
      }
      const pts = [];
      for (const [lo, la, al] of it.hist) pts.push(lo, la, al);
      const w = it.now || this.where(it, nowMs);
      pts.push(w.lon, w.lat, w.alt);
      const positions = C.Cartesian3.fromDegreesArrayHeights(pts);
      if (!it.trail) {
        it.trail = this.trails.add({
          positions, width: 2,
          material: C.Material.fromType("Color", { color: this.colour(it.color, 0.55) }),
        });
      } else {
        it.trail.positions = positions;
        it.trail.material.uniforms.color = this.colour(it.color, 0.55);
      }
      it.trail.show = true;
    }
  }

  setShown(on) {
    this.shown = on;
    this.tick();
    if (!on) for (const it of this.items.values()) if (it.trail) it.trail.show = false;
  }

  find(q) {
    const out = [];
    for (const it of this.items.values()) {
      const p = it.props;
      const hay = [this.labelOf(p), it.id, p.name, p.callsign, p.mmsi, p.label, p.route_id, p.provider]
        .filter(Boolean).join(" ").toLowerCase();
      if (hay.includes(q)) out.push(it);
      if (out.length >= 8) break;
    }
    return out;
  }
}
