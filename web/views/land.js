// Land layer: transit vehicles, declared work zones, the state's cameras,
// and the state lines.
//
// Everything here is read from what the road collectors and G3 already
// wrote. The page never asks an agency for anything; see angels/core/land.py.
//
// FOUR THINGS ON ONE MAP, AND NONE OF THEM IS THE FINDING
//
//   vehicles    transit fleets reporting their own positions (G2c), moving
//   work zones  agencies declaring where their roads are disrupted (G1)
//   cameras     where Maryland itself can see the road (CHART, via G3)
//   state lines the discontinuity the land question is about (G2b)
//
// The land finding is the gap between the first two: slowdowns the vehicles
// show that no agency declared. That is G6, and it does not exist yet.
//
// MOVING, 2026-10-27. The first version drew the ARCHIVE, which keeps only
// changed fixes and is written every five minutes, so nothing moved and a
// bus at a stop vanished. Vehicles now come from the collector's snapshot of
// its last poll, every vehicle it reported, and are dead-reckoned between
// polls with the same code as aircraft and vessels -- along the bearing at
// the speed the vehicle itself sent, never longer than HORIZON_S.land, and
// not at all when it sent no speed.

import { ageShort, attachHover, card, esc, freshness, pinHint } from "./popup.js";
import { reckonFleet } from "./reckon.js";
import * as G from "../ops/icons.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
export const VEHICLE_POLL_MS = 15000;   // the snapshot changes every 30 s
const DRAW_MS = 1000;
const STATUS_POLL_MS = 60000;
const ZONE_POLL_MS = 600000;            // WZDx is polled every 10 min
const WINDOW_MIN = 15;

const BOX = { air: [-77.7, 38.3, -76.0, 39.6], conus: [-125.0, 24.0, -66.5, 49.5] };

let visible = false;
let running = false;
let aoi = "air";
let fleet = [];
let vehicles = null, status = null, zones = null, cams = null;
let lastPoll = 0, requests = 0;

// SYMBOLS, 2026-10-28. Vehicles were dots coloured by a hash of the feed
// name: two agencies could share a colour, nothing said bus from train, and
// at 3 px a dot said nothing at all. Now a vehicle is a badge with its mode
// drawn in it, coloured by mode, and grey when it has no trip; a work zone
// is a road-sign diamond; a camera is a camera. The glyphs are the 3D ops
// view's own (ops/icons.js), so the two views share one key.
const modeOf = (feed) => G.MODE_OF_FEED[feed] || "bus";
const colourOf = (p) => (p.in_service ? G.MODES[modeOf(p.feed_id)].color : G.NO_TRIP);

async function addGlyphs(map) {
  const want = {
    "veh-bus": [G.bus(), G.MODES.bus.color], "veh-rail": [G.rail(), G.MODES.rail.color],
    "veh-ferry": [G.ferry(), G.MODES.ferry.color],
    "veh-bus-off": [G.bus(), G.NO_TRIP], "veh-rail-off": [G.rail(), G.NO_TRIP],
    "veh-ferry-off": [G.ferry(), G.NO_TRIP],
    "wz-sign": [G.workzone(), G.COL.workzone],
    "cam-live": [G.camera(), G.COL.camStream], "cam-none": [G.camera(), G.COL.camNone],
  };
  await Promise.all(Object.entries(want).map(async ([id, [url, c]]) => {
    if (map.hasImage(id)) return;
    const img = new Image();
    img.src = G.tinted(url, c);
    await img.decode();
    if (!map.hasImage(id)) map.addImage(id, img, { pixelRatio: 2 });
  }));
}

const empty = () => ({ type: "FeatureCollection", features: [] });

async function get(path) {
  requests += 1;
  let r;
  try {
    r = await fetch(`${API}${path}`);
  } catch {
    const e = new Error("offline");
    e.kind = "offline";
    e.domain = "land";
    throw e;
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const e = new Error(`HTTP ${r.status}`);
    e.status = r.status;
    e.detail = body.detail;
    e.domain = "land";
    throw e;
  }
  return r.json();
}

export function addLand(map) {
  for (const id of ["land-states", "land-zones", "land-cams", "land-veh"]) {
    map.addSource(id, { type: "geojson", data: empty() });
  }
  addGlyphs(map).catch(() => {});
  map.addLayer({ id: "land-states-line", type: "line", source: "land-states",
    paint: { "line-color": "#1b2a36", "line-width": 2.4,
             "line-dasharray": [3, 2], "line-opacity": 0.9 },
    layout: { visibility: "none" } });
  // A casing under the zone, so amber reads on amber-ish basemap roads.
  map.addLayer({ id: "land-zones-case", type: "line", source: "land-zones",
    filter: ["!=", ["geometry-type"], "Point"],
    paint: { "line-color": "#3a1d00", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 5, 14, 10],
             "line-opacity": 0.55 },
    layout: { visibility: "none", "line-cap": "round" } });
  map.addLayer({ id: "land-zones-line", type: "line", source: "land-zones",
    filter: ["!=", ["geometry-type"], "Point"],
    paint: { "line-color": "#ff9f1a", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3, 14, 6],
             "line-opacity": 0.95 },
    layout: { visibility: "none", "line-cap": "round" } });
  map.addLayer({ id: "land-zones-point", type: "symbol", source: "land-zones",
    filter: ["==", ["geometry-type"], "Point"],
    layout: { visibility: "none", "icon-image": "wz-sign",
              "icon-size": ["interpolate", ["linear"], ["zoom"], 3, 0.32, 8, 0.6, 13, 0.9],
              "icon-allow-overlap": true, "icon-ignore-placement": true } });
  map.addLayer({ id: "land-cams", type: "symbol", source: "land-cams",
    layout: { visibility: "none",
              "icon-image": ["case", ["get", "has_stream"], "cam-live", "cam-none"],
              "icon-size": ["interpolate", ["linear"], ["zoom"], 6, 0.38, 10, 0.6, 14, 0.9],
              "icon-allow-overlap": true, "icon-ignore-placement": true } });
  map.addLayer({ id: "land-veh-dot", type: "symbol", source: "land-veh",
    layout: {
      visibility: "none",
      "icon-image": ["concat", "veh-", ["get", "mode"], ["case", ["get", "in_service"], "", "-off"]],
      "icon-size": ["interpolate", ["linear"], ["zoom"], 3, 0.3, 8, 0.5, 12, 0.75, 15, 0.95],
      "icon-allow-overlap": true, "icon-ignore-placement": true,
      // In-service vehicles draw over parked ones.
      "symbol-sort-key": ["case", ["get", "in_service"], 1, 0],
    },
    paint: {
      // In service: solid. No trip (usually parked): faint. Stale: fainter.
      "icon-opacity": ["case", ["get", "stale"], 0.3, ["get", "in_service"], 1, 0.55],
    } });

  // The same hover and pin as aircraft and vessels (popup.js), with a few
  // pixels of tolerance because these are small dots with no hit layer.
  attachHover(map, {
    layers: ["land-veh-dot"], tolerance: 6,
    key: (p) => `${p.feed_id}|${p.label}|${p.route_id}|${p.fix_t}`,
    html: (p, { others, pinnable }) => card({
      title: p.provider || p.feed_id,
      chip: p.c,
      sub: [p.label ? `vehicle ${esc(p.label)}` : null,
            p.route_id ? `route ${esc(p.route_id)}` : null].filter(Boolean).join(" &middot; "),
      age: ageShort(p.age_s), ageState: freshness(p.age_s, 60, 300),
      rows: [
        ["Service", p.in_service ? "on a trip" : "no trip: usually parked"],
        ["Clock", p.age_from === "vehicle" ? "the vehicle's own" : "our poll's; the feed sent none"],
        ["Motion", p.motion === "derived" ? "derived from the previous poll"
          : p.motion === "none" ? "none sent: shown where reported" : "the vehicle's own"],
      ],
      foot: [others ? `${others} other vehicle${others === 1 ? "" : "s"} here; zoom in to separate them.` : null],
      evidence: "Self-reported by the agency's vehicle. Nothing independent saw it.",
      hint: pinHint(pinnable),
    }),
  });
  attachHover(map, {
    layers: ["land-zones-line", "land-zones-point"], tolerance: 5,
    key: (p) => `${p.feed}|${p.road}|${p.start}|${p.end}|${p.direction}`,
    html: (p, { pinnable }) => card({
      title: "Work zone", chip: "#e8590c",
      sub: [esc(p.road), esc(p.direction)].filter(Boolean).join(" &middot; "),
      rows: [["Feed", esc(p.feed)], ["Impact", esc(p.impact)],
             ["From", esc((p.start || "").slice(0, 10))],
             ["To", esc((p.end || "").slice(0, 10)) || "open"]],
      evidence: "Declared by the agency. Not observed.",
      hint: pinHint(pinnable),
    }),
  });
  attachHover(map, {
    layers: ["land-cams"], tolerance: 6, key: (p) => p.id,
    html: (p) => card({
      title: p.location || "CHART camera", chip: "#1c3d7a",
      sub: esc(p.county),
      rows: [["Status", p.status ? esc(String(p.status).toLowerCase().replace("_", " ")) : null],
             ["Video", p.has_stream ? "click to watch in CHART's player" : "no stream offered"]],
      evidence: "Where Maryland itself can see the road.",
    }),
    onClick: (p) => {
      if (!sub.cameras) setLandSub(map, "cameras", true);
      openCamera(map, p.id, p.location, p.county);
    },
  });
}

// WHICH LAYERS EACH SUB-LAYER SHOWS, 2026-10-28. Cameras were their own
// layer so the map was not too full (Joshua); then both were wanted at once.
// So Vehicles and Cameras are independent toggles, either or both, and the
// state lines show with either.
const TAB_LAYERS = {
  vehicles: ["land-states-line", "land-zones-case", "land-zones-line", "land-zones-point", "land-veh-dot"],
  cameras: ["land-states-line", "land-cams"],
};
const sub = { vehicles: true, cameras: false };
const ALL_LAYERS = ["land-states-line", "land-zones-case", "land-zones-line", "land-zones-point",
                    "land-cams", "land-veh-dot"];

function applyLayers(map) {
  const want = new Set(visible
    ? Object.keys(sub).filter((k) => sub[k]).flatMap((k) => TAB_LAYERS[k]) : []);
  for (const id of ALL_LAYERS) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", want.has(id) ? "visible" : "none");
    }
  }
}

export function setVisible(map, on) {
  visible = on;
  applyLayers(map);
}

function draw(map) {
  const src = map.getSource("land-veh");
  if (!src || !fleet.length) return;
  src.setData(reckonFleet(fleet, Date.now(), "land"));
}

async function pollVehicles(map) {
  if (!sub.vehicles && vehicles) return;    // vehicles hidden: keep the last
  const fc = await get(`/land/vehicles?minutes=${WINDOW_MIN}`);
  vehicles = fc;
  lastPoll = Date.now();
  fleet = fc.features.map((f) => {
    const p = f.properties;
    return {
      coords: f.geometry.coordinates,
      // The fix's own time, so the projection runs from when the vehicle
      // was there and not from when we happened to ask.
      t: (p.fix_t ? p.fix_t * 1000 : Date.now() - p.age_s * 1000),
      heading: p.bearing,
      speed_mps: p.speed_mps,
      props: { ...p, c: colourOf(p), mode: modeOf(p.feed_id) },
    };
  });
  draw(map);
}

async function pollStatus() { status = await get("/land/status"); }

async function pollZones(map) {
  const box = BOX[aoi].join(",");
  const [z, s] = await Promise.all([get(`/land/workzones?bbox=${box}`),
                                    get(`/land/states?bbox=${box}`)]);
  zones = z;
  map.getSource("land-zones")?.setData(z);
  map.getSource("land-states")?.setData(s);
}

async function loadCameras(map) {
  if (cams) return;
  cams = await get("/land/cameras");
  map.getSource("land-cams")?.setData(cams);
}

// Polling only while the layer is on: a land view nobody is looking at
// should not read the archive every few seconds.
export function startLand(map, onUpdate, onError) {
  if (running) return;
  running = true;
  const loop = (fn, ms) => {
    const go = async () => {
      if (visible && !document.hidden) {
        try { await fn(map); onUpdate(); } catch (e) { onError(e); }
      }
      setTimeout(go, ms);
    };
    go();
  };
  loop(pollVehicles, VEHICLE_POLL_MS);
  loop(pollStatus, STATUS_POLL_MS);
  loop(pollZones, ZONE_POLL_MS);
  setInterval(() => { if (visible && !document.hidden) draw(map); }, DRAW_MS);
}

export function setLandAoi(map, name) {
  aoi = name === "conus" ? "conus" : "air";
  zones = null;
  if (visible) pollZones(map).catch(() => {});
}

// Called when the layer is switched on, so it does not wait out a timer.
export function refreshLand(map, onUpdate, onError) {
  Promise.all([pollVehicles(map), pollStatus(), pollZones(map), loadCameras(map)])
    .then(onUpdate, onError);
}

export const landInfo = () => ({ vehicles, status, zones, cams, lastPoll,
                                 requests, visible, aoi });

// --- the Cameras sub-tab -----------------------------------------------------
//
// A browser of Maryland's CHART cameras, and a viewer that plays one IN
// CHART'S OWN PLAYER, from CHART's servers. Nothing is recorded, stored or
// re-served here: the page asks the API for the link (kept on this machine,
// validated to a Maryland government host by G3), and sets it as an iframe's
// src. The publisher's own embed markup is never inserted into the page.


function camEl() {
  let d = document.getElementById("cam-drawer");
  if (d) return d;
  d = document.createElement("div");
  d.id = "cam-drawer";
  d.hidden = true;
  d.innerHTML = `
    <div class="head"><div class="t">CHART CAMERAS &middot; MARYLAND</div>
      <input type="search" placeholder="Search road, place or county" aria-label="Search cameras">
      <div class="meta" id="cam-count" style="margin:6px 0 0"></div></div>
    <div class="list"></div>
    <div class="foot">Video by Maryland CHART, played from CHART's own servers.
      This project does not record, store or republish it. Cameras are the
      state's own observation points, separate from the vehicles and the
      declared work zones.</div>`;
  document.body.appendChild(d);
  const v = document.createElement("div");
  v.id = "cam-viewer";
  v.hidden = true;
  document.body.appendChild(v);
  return d;
}

function renderCamList(map, q = "") {
  const d = camEl();
  const list = d.querySelector(".list");
  const feats = (cams?.features || []).slice()
    .sort((a, b) => (a.properties.county || "").localeCompare(b.properties.county || "")
      || (a.properties.location || "").localeCompare(b.properties.location || ""));
  const needle = q.trim().toLowerCase();
  const shown = needle ? feats.filter((f) => `${f.properties.location} ${f.properties.county}`
    .toLowerCase().includes(needle)) : feats;
  d.querySelector("#cam-count").textContent = cams
    ? `${shown.length} of ${feats.length} cameras; ${cams.properties.n_with_stream ?? 0} offer a stream`
    : "loading…";
  list.replaceChildren(...shown.map((f) => {
    const p = f.properties;
    const b = document.createElement("button");
    b.className = "cam" + (p.has_stream ? "" : " nostream");
    b.dataset.id = p.id;
    const name = document.createElement("span");
    name.textContent = p.location || "(unnamed)";
    const c = document.createElement("span");
    c.className = "c";
    c.textContent = `${p.county || ""}${p.has_stream ? "" : " · no stream"}${
      p.status && p.status !== "OK" ? " · " + p.status.toLowerCase().replace("_", " ") : ""}`;
    b.append(name, c);
    b.onclick = () => {
      // Centred in the part of the map the drawer and the player leave open.
      map.easeTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), 12),
                   padding: { left: 330, bottom: 360, top: 20, right: 20 }, duration: 800 });
      openCamera(map, p.id, p.location, p.county);
    };
    return b;
  }));
}

export async function openCamera(map, id, location, county) {
  camEl();
  const v = document.getElementById("cam-viewer");
  document.querySelectorAll("#cam-drawer .cam").forEach(
    (b) => b.classList.toggle("on", b.dataset.id === String(id)));
  v.hidden = false;
  const bar = document.createElement("div");
  bar.className = "bar";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = `${location || "Camera"}${county ? " · " + county : ""}`;
  const close = document.createElement("button");
  close.textContent = "×";
  close.title = "Close";
  close.onclick = () => { v.hidden = true; v.replaceChildren(); };
  bar.append(name);
  v.replaceChildren(bar);
  let s = null;
  try { s = await get(`/land/cameras/${encodeURIComponent(id)}/stream`); } catch { s = null; }
  if (s?.page) {
    const a = document.createElement("a");
    a.href = s.page; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.textContent = "open on CHART";
    bar.append(a);
  }
  bar.append(close);
  if (s?.embed && s.embeddable !== false) {
    const f = document.createElement("iframe");
    // CHART's player, from CHART. Scripts are allowed because a player needs
    // them; the page cannot reach this one (sandboxed, cross-origin).
    f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups");
    f.setAttribute("allow", "autoplay; fullscreen");
    f.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
    f.src = s.embed;
    v.append(f);
  } else {
    const m = document.createElement("div");
    m.className = "msg";
    m.textContent = s?.page
      ? (s.embeddable === false
        ? "CHART does not allow its player inside other pages (checked from its own headers at the last G3 fetch). Use “open on CHART”."
        : "CHART offers a page for this camera but no embeddable player. Use “open on CHART”.")
      : "No stream link for this camera. If none show, run: python scripts/fetch_road_mask.py --layer cameras";
    v.append(m);
  }
  const src = document.createElement("div");
  src.className = "src";
  src.textContent = "Video: Maryland CHART, from CHART's servers. Not recorded or stored here.";
  v.append(src);
}

const MARYLAND = [-79.5, 37.9, -75.0, 39.75];

// Moves the map ONLY if none of `box` is in view. Switching a layer on used
// to fly the map somewhere every time, which threw away a view the user had
// chosen. If what was asked for is already partly on screen, the view stays.
export function ensureInView(map, box, padding = 40) {
  const b = map.getBounds();
  const overlap = !(box[2] < b.getWest() || box[0] > b.getEast()
                    || box[3] < b.getSouth() || box[1] > b.getNorth());
  if (!overlap) map.fitBounds(box, { padding, duration: 900 });
}

// Vehicles and Cameras, each on or off. While Land is on, switching off the
// last one does nothing, as with the domains; `force` is for Land itself
// going off, which closes the cameras whatever else is on.
export async function setLandSub(map, name, on, { force = false } = {}) {
  if (!(name in sub)) return;
  const other = name === "vehicles" ? "cameras" : "vehicles";
  if (!on && !sub[other]) {
    if (!force) return;
    sub[other] = true;
  }
  sub[name] = on;
  document.querySelectorAll("#landtabs button").forEach(
    (b) => b.classList.toggle("on", !!sub[b.dataset.landtab]));
  applyLayers(map);
  const d = camEl();
  d.hidden = !sub.cameras;
  if (!sub.cameras) {
    const v = document.getElementById("cam-viewer");
    v.hidden = true; v.replaceChildren();
    return;
  }
  if (!cams) { try { await loadCameras(map); } catch { /* the list says loading */ } }
  renderCamList(map);
  d.querySelector("input").oninput = (e) => renderCamList(map, e.target.value);
  ensureInView(map, MARYLAND);
}

export const landSub = () => ({ ...sub });
