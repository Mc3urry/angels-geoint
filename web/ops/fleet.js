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
//
// THE GLITCHES, 2026-11-02. Four things made symbols misbehave, and each
// has its rule here:
//
//   1. DEPTH. Billboards were depth-tested against the globe beyond 3,000 km
//      from the camera, so at continent zoom a symbol on the ground was cut
//      in half by the surface and flickered. Now no symbol is depth-tested;
//      the far side of the Earth is hidden by a horizon test instead
//      (Cesium's EllipsoidalOccluder), each frame the camera moves.
//   2. STEPS. Positions were moved four times a second while the globe drew
//      sixty, so a moving symbol stepped, and a followed one shook against
//      a camera that moved smoothly. Moving contacts are now placed every
//      frame (frame()); the 4 Hz pass keeps colour, filters and staleness.
//   3. SNAPS. A new report put the symbol where the contact said it was, a
//      jump from where the projection had drawn it. The difference is now
//      eased out over SETTLE_MS; a jump longer than SNAP_M is drawn as one,
//      because that is news, not error.
//   4. SMEAR. A 64 px pictogram shrunk to 8 px is noise. Far from the camera
//      (above `dotAbove`) every contact is a dot in its colour, from a
//      texture drawn at that size, and no symbol is shrunk below 60 %.
//
// Billboards are drawn in one translucent pass, in order (BlendOption
// TRANSLUCENT): the default opaque-then-translucent passes drew the soft
// edge of one symbol over the middle of the next.

import { project, HORIZON_S } from "../views/reckon.js";
import { dot } from "./icons.js";

const TRAIL_MAX = 60;
let scratch = null;                  // fixes per contact
const TRAIL_MAX_AGE_MS = 15 * 60e3;
const SETTLE_MS = 1500;
const SNAP_M = 3000;
const DEG_M = 111320;

export const NO_DEPTH = Number.POSITIVE_INFINITY;

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
    this.style = o.style;               // (props) -> {image, color, heading|null, px, alpha?, tick?, hollow?}
    this.labelOf = o.labelOf;
    this.altOf = o.altOf || (() => 0);
    this.labelRange = o.labelRange ?? 160e3;
    this.basePx = o.px ?? 30;
    this.near = o.near ?? 2e4;
    this.maxLabels = o.maxLabels ?? 25;
    this.far = o.far ?? 8e6;
    this.dotAbove = o.dotAbove ?? 2.5e6; // camera height above which contacts are dots
    this.dotPx = o.dotPx ?? 9;
    this.lift = o.lift ?? 0;            // metres a ground trail is raised
    this.dots = false;
    const scene = viewer.scene;
    const blend = Cesium.BlendOption.TRANSLUCENT;
    this.scaler = new Cesium.NearFarScalar(this.near, 1.0, this.far, 0.6);
    this.trails = scene.primitives.add(new Cesium.PolylineCollection());
    this.bbs = scene.primitives.add(new Cesium.BillboardCollection({ scene, blendOption: blend }));
    this.labels = scene.primitives.add(new Cesium.LabelCollection({ scene, blendOption: blend }));
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
    for (const it of this.items.values()) if (it.vis) n += 1;
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
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
      const t = this.timeOf(p, nowMs);
      const alt = this.altOf(p);
      let it = this.items.get(id);
      let before = null;
      if (!it) {
        it = { id, hist: [], bb: null, tick: null, lbl: null, trail: null, styleKey: "",
               vis: false, up: true, corr: null };
        it.bb = this.bbs.add({
          position: C.Cartesian3.fromDegrees(lon, lat, alt),
          id: { kind: "fleet", layer: this, id },
          scaleByDistance: this.scaler,
          disableDepthTestDistance: NO_DEPTH,
        });
        this.items.set(id, it);
      } else {
        if (it.lon !== lon || it.lat !== lat) {
          // A new REPORTED position: the old one joins the trail.
          it.hist.push([it.lon, it.lat, it.alt, it.t]);
          if (it.hist.length > TRAIL_MAX) it.hist.shift();
        }
        before = it.now && it.vis ? it.now : null;
      }
      it.lon = lon; it.lat = lat; it.alt = alt; it.t = t;
      it.heading = p.heading ?? p.bearing ?? null;
      it.speed = p.speed_mps ?? null;
      it.props = p;
      // Ease from where it was drawn to where it now says it is.
      it.corr = null;
      if (before) {
        const w = this.where(it, nowMs);
        const dLon = before.lon - w.lon, dLat = before.lat - w.lat;
        const m = Math.hypot(dLon * DEG_M * Math.cos((w.lat * Math.PI) / 180), dLat * DEG_M);
        if (m > 0.5 && m < SNAP_M) it.corr = { dLon, dLat, dAlt: before.alt - w.alt, t0: nowMs };
      }
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
    it.alpha = s.alpha ?? 1;
    const dots = this.dots;
    it.tint = dots ? "#ffffff" : (s.tint ?? s.color);   // what multiplies the glyph
    it.rotates = !dots && s.heading != null;
    const wantTick = !dots && !!s.tick;
    const k = `${dots ? `dot|${s.color}|${s.hollow ? 1 : 0}` : `${s.image}|${s.px}`}|${it.rotates ? 1 : 0}|${wantTick ? 1 : 0}`;
    if (k === it.styleKey) return;
    it.styleKey = k;
    const px = dots ? this.dotPx : (s.px ?? this.basePx);
    it.bb.image = dots ? dot(s.color, !!s.hollow) : s.image;
    it.bb.width = px; it.bb.height = px;
    // A dot is drawn at its own size whatever the distance.
    it.bb.scaleByDistance = dots ? undefined : this.scaler;
    it.bb.alignedAxis = it.rotates ? C.Cartesian3.UNIT_Z : C.Cartesian3.ZERO;
    if (!it.rotates) it.bb.rotation = 0;
    if (it.lbl) it.lbl.pixelOffset = new C.Cartesian2(0, px / 2 + 3);
    if (wantTick && !it.tick) {
      it.tick = this.bbs.add({
        image: s.tick, width: px, height: px, position: it.bb.position,
        id: { kind: "fleet", layer: this, id: it.id },
        alignedAxis: C.Cartesian3.UNIT_Z,
        scaleByDistance: this.scaler,
        disableDepthTestDistance: NO_DEPTH,
        show: false,
      });
    } else if (!wantTick && it.tick) {
      this.bbs.remove(it.tick); it.tick = null;
    } else if (it.tick) {
      it.tick.width = px; it.tick.height = px;
    }
  }

  // Dots far out, symbols close in. Called with the camera's height.
  setLod(cameraHeight) {
    const dots = cameraHeight > this.dotAbove;
    if (dots === this.dots) return;
    this.dots = dots;
    for (const it of this.items.values()) this.restyle(it);
    this.tick();
  }

  // Where the contact is now, best estimate, and whether that is a guess.
  where(it, nowMs = Date.now()) {
    const age = (nowMs - it.t) / 1000;
    let lon = it.lon, lat = it.lat, alt = it.alt;
    let moving = false;
    if (this.projecting && it.speed > 0.5 && it.heading != null && age > 0 && age < this.horizon) {
      [lon, lat] = project(it.lat, it.lon, it.heading, it.speed * age);
      moving = true;
    }
    const c = it.corr;
    if (c) {
      const k = 1 - (nowMs - c.t0) / SETTLE_MS;
      if (k > 0) {
        lon += c.dLon * k; lat += c.dLat * k; alt += c.dAlt * k;
        moving = true;
      } else it.corr = null;
    }
    return { lon, lat, alt, age, stale: age > this.horizon, moving };
  }

  position(id, nowMs = Date.now()) {
    const it = this.items.get(id);
    if (!it) return null;
    const w = this.where(it, nowMs);
    return this.C.Cartesian3.fromDegrees(w.lon, w.lat, w.alt);
  }

  // Place one contact where it is now; true if it moved.
  place(it, nowMs) {
    const w = this.where(it, nowMs);
    it.now = w;
    if (w.lon === it._lon && w.lat === it._lat && w.alt === it._alt) return false;
    const pos = this.C.Cartesian3.fromDegrees(w.lon, w.lat, w.alt);
    it.bb.position = pos;
    if (it.tick) it.tick.position = pos;
    if (it.lbl) it.lbl.position = pos;
    it._lon = w.lon; it._lat = w.lat; it._alt = w.alt;
    return true;
  }

  // Shown if the filters allow it and the Earth is not in the way.
  reveal(it, occ) {
    const up = !occ || occ.isPointVisible(it.bb.position);
    it.up = up;
    it.bb.show = it.vis && up;
    if (it.tick) it.tick.show = it.vis && up && it.heading != null;
    if (it.lbl && !(it.vis && up)) it.lbl.show = false;
    if (it.trail && !(it.vis && up)) it.trail.show = false;
  }

  // 4 Hz: which contacts are drawn, in what colour, which way they point.
  tick(nowMs = Date.now(), occ = this.occ) {
    const C = this.C;
    for (const it of this.items.values()) {
      it.vis = this.shown && (!this.filter || this.filter(it.props));
      if (!it.vis) {
        it.bb.show = false;
        if (it.tick) it.tick.show = false;
        if (it.lbl) it.lbl.show = false;
        if (it.trail) it.trail.show = false;
        continue;
      }
      this.place(it, nowMs);
      const w = it.now;
      const alpha = w.stale ? Math.min(it.alpha, 0.35) : it.alpha;
      it.bb.color = this.colour(it.tint, alpha);
      const rot = it.heading != null ? -C.Math.toRadians(it.heading) : 0;
      if (it.rotates) it.bb.rotation = rot;
      if (it.tick) { it.tick.rotation = rot; it.tick.color = this.colour(it.color, alpha); }
      this.reveal(it, occ);
    }
  }

  // Every frame: move what is moving, and when the camera moved, ask the
  // horizon again for everything.
  frame(nowMs, occ, cameraMoved) {
    this.occ = occ;
    if (!this.shown) return;
    for (const it of this.items.values()) {
      if (!it.vis) continue;
      const moved = (it.now?.moving || it.corr) ? this.place(it, nowMs) : false;
      if (moved || cameraMoved) this.reveal(it, occ);
    }
  }

  // Labels exist only while the camera is low enough to read them: five
  // thousand labels over a continent are a texture, not information.
  labelPass(cameraHeight, camera) {
    const C = this.C;
    const want = this.shown && this.showLabels && !this.dots && cameraHeight < this.labelRange * 1.6;
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
    // rest are named on hover. A label already up stays up while it is in
    // the nearest half again as many, so labels do not blink on and off as
    // contacts trade places each second (2026-11-02).
    const near = [];
    // Nearest among those ON SCREEN: the camera's own frustum. A cone was
    // used until 2026-11-02, and it was wider than the picture, so with the
    // camera tilted the nearest contacts were the ones just below the
    // bottom edge, and the labels went to them.
    const cv = camera.frustum.computeCullingVolume(camera.positionWC, camera.directionWC, camera.upWC);
    const sphere = this.sphere || (this.sphere = new C.BoundingSphere(new C.Cartesian3(), 1));
    for (const it of this.items.values()) {
      if (!it.bb.show || !this.labelOf(it.props)) { if (it.lbl) it.lbl.show = false; continue; }
      const v = C.Cartesian3.subtract(it.bb.position, camera.positionWC, scratch);
      const d = C.Cartesian3.magnitudeSquared(v);
      C.Cartesian3.clone(it.bb.position, sphere.center);
      const inView = d <= this.labelRange * this.labelRange
        && cv.computeVisibility(sphere) !== C.Intersect.OUTSIDE;
      if (!inView) { if (it.lbl) it.lbl.show = false; continue; }
      near.push([d, it]);
    }
    near.sort((x, y) => x[0] - y[0]);
    const keep = new Set();
    const hold = Math.ceil(this.maxLabels * 1.5);
    for (let i = 0; i < near.length && i < hold; i += 1) {
      const it = near[i][1];
      if (i < this.maxLabels || (it.lbl && it.lbl.show)) keep.add(it);
    }
    for (const [, it] of near) {
      if (!keep.has(it)) { if (it.lbl) it.lbl.show = false; continue; }
      const text = this.labelOf(it.props);
      if (!it.lbl) {
        // The 2D viewer's label: dark mono on a white halo.
        it.lbl = this.labels.add({
          text,
          font: "600 11px 'IBM Plex Mono', ui-monospace, Menlo, Consolas, monospace",
          fillColor: C.Color.fromCssColorString("#0e1a24"),
          outlineColor: C.Color.WHITE,
          outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE,
          verticalOrigin: C.VerticalOrigin.TOP,
          pixelOffset: new C.Cartesian2(0, (it.bb.height || this.basePx) / 2 + 3),
          disableDepthTestDistance: NO_DEPTH,
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
      const on = this.shown && this.showTrails && !this.dots && it.bb.show && it.hist.length > 0;
      if (!on) {
        if (it.trail) it.trail.show = false;
        continue;
      }
      const pts = [];
      for (const [lo, la, al] of it.hist) pts.push(lo, la, al + this.lift);
      const w = it.now || this.where(it, nowMs);
      pts.push(w.lon, w.lat, w.alt + this.lift);
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

// A layer of points that do not move (cameras, work zones, ALPR): the same
// depth and horizon rules as a fleet, and the same dots far out.
export class StaticPoints {
  constructor(viewer, o) {
    const Cs = window.Cesium;
    this.C = Cs;
    this.dotAbove = o.dotAbove ?? 1.2e6;
    this.dotPx = o.dotPx ?? 8;
    this.px = o.px ?? 22;
    this.scaler = o.scaler ?? new Cs.NearFarScalar(3e3, 1.1, 5e6, 0.6);
    this.ddc = o.ddc;
    this.bbs = viewer.scene.primitives.add(new Cs.BillboardCollection({
      scene: viewer.scene, blendOption: Cs.BlendOption.TRANSLUCENT }));
    this.dots = false;
    this.pts = [];                       // {bb, image, css}
  }

  clear() { this.bbs.removeAll(); this.pts = []; }

  add(lon, lat, h, image, css, id) {
    const Cs = this.C;
    const bb = this.bbs.add({
      position: Cs.Cartesian3.fromDegrees(lon, lat, h), id,
      disableDepthTestDistance: NO_DEPTH, distanceDisplayCondition: this.ddc,
    });
    const pt = { bb, image, css };
    this.style(pt);
    this.pts.push(pt);
    return pt;
  }

  style(pt) {
    const { bb } = pt;
    if (this.dots) {
      bb.image = dot(pt.css); bb.width = bb.height = this.dotPx; bb.color = this.C.Color.WHITE;
      bb.scaleByDistance = undefined;
    } else {
      bb.image = pt.image; bb.width = bb.height = this.px;
      bb.scaleByDistance = this.scaler;
      bb.color = this.C.Color.fromCssColorString(pt.css);
    }
  }

  setLod(cameraHeight) {
    const dots = cameraHeight > this.dotAbove;
    if (dots === this.dots) return;
    this.dots = dots;
    for (const pt of this.pts) this.style(pt);
  }

  cull(occ, cameraHeight = 0) {
    if (!this.bbs.show) return;
    // Beyond the display distance none is drawn anyway.
    if (this.ddc && cameraHeight > this.ddc.far) return;
    for (const { bb } of this.pts) bb.show = occ.isPointVisible(bb.position);
  }

  set show(v) { this.bbs.show = v; }
  get show() { return this.bbs.show; }
}
