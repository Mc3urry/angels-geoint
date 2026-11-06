// Satellites on the flat map, as on the globe (2026-11-03).
//
// CONTEXT, NOT EVIDENCE. Each is drawn at the point on the ground beneath
// it, computed in this browser from CelesTrak's elements (via the API's
// /context/satellites, cached there) and moved every two seconds. Clicking
// one draws its ground track for one orbit. The card is the globe's own
// (ops/satcard.js).

import * as G from "../ops/icons.js";
import { satCardData, satNow, groundTrack } from "../ops/satcard.js";
import { attachHover, card } from "./popup.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
const EMPTY = { type: "FeatureCollection", features: [] };
const MOVE_MS = 2000;

let sats = [];
let visible = false;
let timer = 0;
let loadedAt = 0;
let error = null;

export const satsInfo = () => ({ n: sats.length, visible, error, loadedAt });
export const satList = () => (visible ? sats : []);

async function load() {
  if (sats.length && Date.now() - loadedAt < 6 * 3600e3) return;
  if (!window.satellite) throw new Error("satellite.js did not load");
  const r = await fetch(`${API}/context/satellites`);
  if (!r.ok) throw new Error(r.status === 502 ? "CelesTrak did not answer" : `HTTP ${r.status}`);
  const doc = await r.json();
  sats = [];
  for (const s of doc.satellites) {
    let rec;
    try { rec = window.satellite.twoline2satrec(s.line1, s.line2); } catch { continue; }
    const grp = G.SAT_GROUPS[s.group] ? s.group : "visual";
    sats.push({ ...s, group: grp, rec, norad: s.line1.slice(2, 7).trim(), color: G.SAT_GROUPS[grp].color });
  }
  loadedAt = Date.now();
}

export function satPosition(s, date = new Date()) {
  const n = satNow(s.rec, date);
  return n ? [n.lon, n.lat] : null;
}

function draw(map) {
  const now = new Date();
  const features = [];
  for (const s of sats) {
    const p = satPosition(s, now);
    if (!p) continue;
    features.push({ type: "Feature", geometry: { type: "Point", coordinates: p },
                    properties: { norad: s.norad, name: s.name, group: s.group,
                                  station: s.group === "stations" } });
  }
  map.getSource("sats")?.setData({ type: "FeatureCollection", features });
}

// The track crosses the antimeridian as a jump of nearly 360 degrees; split
// it there, or the line draws across the whole map.
function trackLines(pts) {
  const lines = [[]];
  for (let i = 0; i < pts.length; i += 1) {
    if (i && Math.abs(pts[i][0] - pts[i - 1][0]) > 180) lines.push([]);
    lines[lines.length - 1].push(pts[i]);
  }
  return lines.filter((l) => l.length > 1);
}

export function showTrack(map, norad) {
  const s = sats.find((x) => x.norad === norad);
  map.getSource("sat-track")?.setData(s ? {
    type: "Feature", geometry: { type: "MultiLineString", coordinates: trackLines(groundTrack(s.rec)) },
    properties: { color: s.color } } : EMPTY);
}

export async function addSats(map) {
  map.addSource("sats", { type: "geojson", data: EMPTY });
  map.addSource("sat-track", { type: "geojson", data: EMPTY });
  await Promise.all(Object.entries(G.SAT_GROUPS).map(async ([k, g]) => {
    const img = new Image();
    // Pale groups get the dark ink of the glyph's edge so they read on the
    // light map; the colour still names the group.
    img.src = G.tinted(G.satellite(), g.color);
    await img.decode();
    if (!map.hasImage(`sat-${k}`)) map.addImage(`sat-${k}`, img, { pixelRatio: 2 });
  })).catch(() => {});
  map.addLayer({ id: "sat-track", type: "line", source: "sat-track",
    layout: { visibility: "none", "line-cap": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": 0.8, "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "sats", type: "symbol", source: "sats",
    layout: { visibility: "none", "icon-image": ["concat", "sat-", ["get", "group"]],
              "icon-size": ["case", ["get", "station"], 0.62, 0.42],
              "icon-allow-overlap": true, "icon-ignore-placement": true,
              "text-field": ["case", ["get", "station"], ["get", "name"], ""],
              "text-font": ["Noto Sans Regular"], "text-size": 11, "text-offset": [0, 1.3],
              "text-anchor": "top", "text-optional": true },
    paint: { "text-color": "#0e1a24", "text-halo-color": "#ffffff", "text-halo-width": 1.5 } });
  attachHover(map, {
    layers: ["sats"], tolerance: 6, key: (p) => p.norad,
    html: (p) => {
      const s = sats.find((x) => x.norad === p.norad);
      return s ? card({ ...satCardData(s), hint: "Click for its ground track" }) : "";
    },
    onClick: (p) => showTrack(map, p.norad),
  });
}

export async function setSats(map, on, onUpdate = () => {}) {
  visible = on;
  for (const id of ["sats", "sat-track"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  }
  clearInterval(timer);
  if (!on) { map.getSource("sat-track")?.setData(EMPTY); onUpdate(); return; }
  try {
    await load();
    error = null;
  } catch (e) {
    error = String(e.message || e);
    onUpdate();
    return;
  }
  draw(map);
  timer = setInterval(() => { if (visible && !document.hidden) draw(map); }, MOVE_MS);
  onUpdate();
}
