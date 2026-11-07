// The research layers on the globe, as in the 2D view (2026-11-03).
//
//   Observed · SAR   unexplained Sentinel-1 returns and the fixed structures
//                    removed from them (/events/candidates, /events/sites)
//   Coverage probe   click the globe while Observed is on: when did the
//                    radar last look here (/coverage)
//   Archive trails   recorded aircraft paths for the last 1-24 h (/tracks)
//
// The cards are the 2D view's own (views/discrepancy.js, views/map.js), so a
// return or a track reads the same in both views. The rules are kept too:
// observed returns are hollow and slate, nothing else is; each carries the
// date of the pass that saw it and fades with age; the fixed structures are
// drawn so their removal can be checked; a probe that finds nothing says
// "never searched", which is not "nothing there".

import * as I from "./icons.js";
import { get } from "./api.js";
import { StaticPoints } from "./fleet.js";
import { candidateCardData, siteCardData, probeCardData } from "../views/discrepancy.js";
import { trackCardData } from "../views/map.js";

const C = () => window.Cesium;

// Older passes fade, as in 2D: 0 days 0.95, 60 days 0.85, 240 days 0.65
// (floor raised from 0.45, 2026-11-07).
function ageAlpha(days) {
  if (days == null) return 0.95;
  if (days <= 60) return 0.95 - (0.1 * days) / 60;
  return Math.max(0.65, 0.85 - (0.2 * (days - 60)) / 180);
}

export class Observed {
  constructor(viewer) {
    const Cs = C();
    this.viewer = viewer;
    this.key = "obs";
    this.on = false;
    this.sites = new StaticPoints(viewer, { px: 16, dotAbove: Infinity,
      scaler: new Cs.NearFarScalar(5e3, 1.1, 4e6, 0.6) });
    this.pts = new StaticPoints(viewer, { px: 22, dotAbove: Infinity,
      scaler: new Cs.NearFarScalar(5e3, 1.1, 4e6, 0.6) });
    this.cands = [];
    this.siteRows = [];
    this.meta = null;
    this.siteMeta = null;
  }

  async load() {
    const doc = await get("/events/candidates?reception=heard");
    const now = Date.now();
    this.pts.clear();
    this.cands = doc.features.map((f) => {
      const p = { ...f.properties };
      p.age_days = p.date ? (now - Date.parse(`${p.date}T00:00:00Z`)) / 86400000 : null;
      return { lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1], p };
    });
    this.cands.forEach((c, i) => {
      const { bb } = this.pts.add(c.lon, c.lat, 0, I.candRing(), "#ffffff", { kind: "obs", layer: this, id: i });
      bb.color = C().Color.WHITE.withAlpha(ageAlpha(c.p.age_days));
    });
    this.meta = doc.properties || {};
    // Sites are context, not the product: their failure never blanks the layer.
    try {
      const s = await get("/events/sites");
      this.sites.clear();
      this.siteRows = s.features.map((f) => ({ lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1],
                                               p: f.properties }));
      this.siteRows.forEach((r, i) => this.sites.add(r.lon, r.lat, 0, I.siteRing(), "#ffffff",
        { kind: "obs", layer: this, id: `s${i}` }));
      this.siteMeta = s.properties || {};
    } catch { /* the candidates stand on their own */ }
    this.setOn(this.on);
    return this.meta;
  }

  setOn(on) { this.on = on; this.pts.show = this.sites.show = on; }
  get count() { return this.on ? this.cands.length : 0; }
  item(id) {
    return typeof id === "string" && id.startsWith("s") ? this.siteRows[Number(id.slice(1))] : this.cands[id];
  }
  position(id) {
    const it = this.item(id);
    return it ? C().Cartesian3.fromDegrees(it.lon, it.lat, 0) : null;
  }
  card(id) {
    const it = this.item(id);
    if (!it) return null;
    return typeof id === "string" ? siteCardData(it.p) : candidateCardData(it.p);
  }
}

// The answer to "when did the radar last look here", as a selectable point.
export class Probe {
  constructor(viewer) {
    const Cs = C();
    this.key = "probe";
    this.bbs = viewer.scene.primitives.add(new Cs.BillboardCollection({
      scene: viewer.scene, blendOption: Cs.BlendOption.TRANSLUCENT }));
    this.mark = this.bbs.add({ image: I.pin(I.OBS.cand), width: 30, height: 30, show: false,
      verticalOrigin: Cs.VerticalOrigin.BOTTOM, disableDepthTestDistance: Number.POSITIVE_INFINITY,
      id: { kind: "probe", layer: this, id: 0 } });
    this.answer = null;
    this.busy = false;
    this.at = null;
  }

  async ask(lon, lat) {
    this.at = [lon, lat];
    this.busy = true;
    this.mark.position = C().Cartesian3.fromDegrees(lon, lat, 0);
    this.mark.show = true;
    try {
      this.answer = await get(`/coverage?lon=${lon.toFixed(5)}&lat=${lat.toFixed(5)}`);
    } catch (e) {
      this.answer = { error: e.status === 404 ? "No searched grids on disk yet." : String(e.message || e) };
    } finally {
      this.busy = false;
    }
  }

  clear() { this.mark.show = false; this.answer = null; this.at = null; }
  position() { return this.at ? C().Cartesian3.fromDegrees(this.at[0], this.at[1], 0) : null; }
  card() {
    if (!this.at) return null;
    if (this.busy) return { title: "This point", sub: "asking when the radar last looked&hellip;" };
    if (!this.answer || this.answer.error) {
      return { title: "This point", sub: "coverage not available", rows: [["Why", this.answer?.error || "no answer"]] };
    }
    return probeCardData(this.answer);
  }
}

// Recorded aircraft paths from the collector's archive, coloured by the
// highest altitude each reached, as the 2D view colours them.
export class ArchiveTracks {
  constructor(viewer) {
    const Cs = C();
    this.viewer = viewer;
    this.key = "tracks";
    this.on = false;
    this.hours = 2;
    this.lines = viewer.scene.primitives.add(new Cs.PolylineCollection());
    this.feats = [];
    this.meta = null;
  }

  async load(region) {
    const Cs = C();
    const end = new Date();
    const start = new Date(end.getTime() - this.hours * 3600e3);
    const qs = new URLSearchParams({ start: start.toISOString(), end: end.toISOString(),
                                     domain: "air", aoi: region, limit: "500" });
    const fc = await get(`/tracks?${qs}`);
    this.lines.removeAll();
    this.feats = fc.features;
    this.meta = fc.properties || {};
    fc.features.forEach((f, i) => {
      const c = f.geometry?.coordinates || [];
      if (c.length < 2) return;
      const col = Cs.Color.fromCssColorString(I.altColour(f.properties.max_alt_m, false)).withAlpha(0.85);
      this.lines.add({
        positions: Cs.Cartesian3.fromDegreesArrayHeights(c.flatMap(([x, y]) => [x, y, 30])),
        width: 2.2, material: Cs.Material.fromType("Color", { color: col }),
        id: { kind: "track", layer: this, id: i },
      });
    });
    this.setOn(this.on);
    return this.meta;
  }

  setOn(on) { this.on = on; this.lines.show = on; }
  get count() { return this.on ? this.feats.length : 0; }
  position(i) {
    const c = this.feats[i]?.geometry?.coordinates;
    if (!c?.length) return null;
    const m = c[Math.floor(c.length / 2)];
    return C().Cartesian3.fromDegrees(m[0], m[1], 30);
  }
  card(i) {
    const p = this.feats[i]?.properties;
    return p ? trackCardData(p) : null;
  }
}
