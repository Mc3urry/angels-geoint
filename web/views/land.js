// Land layer: transit across the US, intercity rail, DOT snowplows, declared
// work zones, the state's traffic cameras, road speeds, ALPR locations and
// the state lines.
//
// FROM THE SAME API AS THE 3D VIEW, 2026-11-02. Land in this viewer used to
// read only what the border collectors wrote (/land/vehicles, and CHART's
// cameras through /land/cameras), while the 3D view had the whole country.
// Joshua asked that every source work in both, so this layer now reads the
// routes the 3D view reads, and each is its own toggle while Land is on:
//
//   Transit   /vehicles/us      every keyless and keyed US feed, the 38
//                               research border feeds among them (marked)
//   Rail      /vehicles/amtrak  Amtrak, VIA, Brightline
//   Plows     /vehicles/plows   state DOT fleets, while they work
//   Cameras   /cameras/us       every state's traffic cameras
//   Speeds    /traffic/flow     TomTom flow tiles: aggregate, no vehicle in it
//   ALPR      /context/alpr     mapped plate-reader locations: context only
//
// The keys stay on the server (angels/api/routes/*): nothing here holds one.
// None of it changes an analysis artefact. The research question reads the
// 38 border feeds the collector archives; everything else is the viewer-only
// scope exception (README), and a card says which a vehicle is.
//
// THE FINDING IS NOT ON THIS MAP. The land finding is the gap between the
// vehicles and the declared work zones: slowdowns the vehicles show that no
// agency declared. That is G6, and it does not exist yet.
//
// MOVING, 2026-10-27. Vehicles come from the last poll of every feed and are
// dead-reckoned between polls with the same code as aircraft and vessels --
// along the bearing at the speed the vehicle itself sent, never longer than
// HORIZON_S.land, and not at all when it sent no speed.

import { ageShort, attachHover, card, compass, esc, freshness, pinHint } from "./popup.js";
import { reckonFleet } from "./reckon.js";
import * as G from "../ops/icons.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
const ORIGIN = API || location.origin;
export const VEHICLE_POLL_MS = 15000;   // the API polls the feeds every 30 s
const DRAW_MS = 1000;
const STATUS_POLL_MS = 60000;
const ZONE_POLL_MS = 600000;            // WZDx is polled every 10 min
const RAIL_POLL_MS = 30000;
const PLOW_POLL_MS = 60000;
const CAM_LOADING_MS = 8000;            // while the API's camera sources load
const CAM_POLL_MS = 900000;

const BOX = { air: [-77.7, 38.3, -76.0, 39.6], conus: [-125.0, 24.0, -66.5, 49.5] };

let visible = false;
let running = false;
let aoi = "air";
let fleet = [], rail = [], plows = [];
let vehicles = null, status = null, zones = null, cams = null, trains = null, plowDoc = null,
    speeds = null, alpr = null;
let lastPoll = 0, requests = 0;
const failed = {};                      // sub-layer -> message

// SYMBOLS, 2026-10-28. A vehicle is a badge with its mode drawn in it,
// coloured by mode, and grey when it has no trip; a work zone is a road-sign
// diamond; a camera is a camera. The glyphs are the 3D view's own
// (ops/icons.js), so the two views share one key.
const modeOf = (feed) => G.MODE_OF_FEED[feed] || "bus";
const colourOf = (p) => (p.in_service ? G.MODES[modeOf(p.feed_id)].color : G.NO_TRIP);

async function addGlyphs(map) {
  const want = {
    "veh-bus": [G.bus(), G.MODES.bus.color], "veh-rail": [G.rail(), G.MODES.rail.color],
    "veh-ferry": [G.ferry(), G.MODES.ferry.color],
    "veh-bus-off": [G.bus(), G.NO_TRIP], "veh-rail-off": [G.rail(), G.NO_TRIP],
    "veh-ferry-off": [G.ferry(), G.NO_TRIP],
    "intercity": [G.rail(), G.COL.rail], "plow": [G.plow(), G.COL.plow],
    "wz-sign": [G.workzone(), G.COL.workzone],
    "cam-live": [G.camera(), G.COL.camStream], "cam-none": [G.camera(), G.COL.camNone],
    "alpr": [G.alpr(), G.COL.alpr], "alpr-cone": [G.facingCone(), G.COL.alpr],
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
    const e = new Error(typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`);
    e.status = r.status;
    e.detail = body.detail;
    e.domain = "land";
    throw e;
  }
  return r.json();
}

const sizeBy = (lo, mid, hi) => ["interpolate", ["linear"], ["zoom"], 3, lo, 8, mid, 13, hi];
const symbol = (id, source, image, size, extra = {}) => ({
  id, type: "symbol", source, ...extra,
  layout: { visibility: "none", "icon-image": image, "icon-size": size,
            "icon-allow-overlap": true, "icon-ignore-placement": true, ...(extra.layout || {}) },
});

export function addLand(map) {
  for (const id of ["land-states", "land-zones", "land-cams", "land-veh", "land-rail",
                    "land-plow", "land-alpr", "land-alpr-cone"]) {
    map.addSource(id, { type: "geojson", data: empty() });
  }
  addGlyphs(map).catch(() => {});
  // Road speeds sit under everything else on land; the source is added
  // when the layer is first switched on and the API says a key is set.
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
  map.addLayer(symbol("land-zones-point", "land-zones", "wz-sign", sizeBy(0.32, 0.6, 0.9),
    { filter: ["==", ["geometry-type"], "Point"] }));
  // ALPR appear from zoom 6, as in the 3D view (below 1,500 km): tens of
  // thousands of hexagons over a continent say nothing a count does not.
  // Which way each one faces, from zoom 10, under its hexagon.
  map.addLayer(symbol("land-alpr-cone", "land-alpr-cone", "alpr-cone",
    ["interpolate", ["linear"], ["zoom"], 10, 1.1, 16, 1.9],
    { minzoom: 10, layout: { "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map",
                             "icon-pitch-alignment": "map" } }));
  map.addLayer(symbol("land-alpr", "land-alpr", "alpr", sizeBy(0.3, 0.42, 0.75), { minzoom: 6 }));
  map.addLayer(symbol("land-cams", "land-cams", ["case", ["get", "live"], "cam-live", "cam-none"],
    sizeBy(0.3, 0.5, 0.85)));
  map.addLayer(symbol("land-plow", "land-plow", "plow", sizeBy(0.35, 0.6, 0.9),
    { paint: { "icon-opacity": ["case", ["get", "stale"], 0.35, 1] } }));
  map.addLayer(symbol("land-veh-dot", "land-veh",
    ["concat", "veh-", ["get", "mode"], ["case", ["get", "in_service"], "", "-off"]],
    ["interpolate", ["linear"], ["zoom"], 3, 0.3, 8, 0.5, 12, 0.75, 15, 0.95],
    { layout: { "symbol-sort-key": ["case", ["get", "in_service"], 1, 0] },
      // In service: solid. No trip (usually parked): faint. Stale: fainter.
      paint: { "icon-opacity": ["case", ["get", "stale"], 0.3, ["get", "in_service"], 1, 0.55] } }));
  map.addLayer(symbol("land-rail", "land-rail", "intercity", sizeBy(0.4, 0.62, 0.9),
    { paint: { "icon-opacity": ["case", ["get", "stale"], 0.35, 1] } }));

  // The same hover and pin as aircraft and vessels (popup.js), with a few
  // pixels of tolerance because these are small symbols with no hit layer.
  attachHover(map, {
    layers: ["land-veh-dot"], tolerance: 6,
    key: (p) => `${p.feed_id}|${p.vehicle_key}|${p.fix_t}`,
    html: (p, { others, pinnable }) => card({
      title: p.provider || p.feed_id,
      chip: p.c,
      sub: [p.label ? `vehicle ${esc(p.label)}` : null,
            p.route_id ? `route ${esc(p.route_id)}` : null].filter(Boolean).join(" &middot; "),
      age: ageShort(p.age_s), ageState: freshness(p.age_s, 60, 300),
      rows: [
        ["Service", p.in_service ? "on a trip" : "no trip: usually parked"],
        ["Feed", p.research ? "<strong>research</strong> &middot; a G2b border feed"
          : "viewer only &middot; not in the analysis"],
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
    layers: ["land-rail"], tolerance: 6, key: (p) => p.id,
    html: (p, { pinnable }) => card({
      title: `${p.route || p.provider} ${p.number || ""}`.trim(), chip: G.COL.rail,
      sub: [esc(p.provider), p.origin && p.destination ? `${esc(p.origin)} to ${esc(p.destination)}` : null]
        .filter(Boolean).join(" &middot; "),
      age: ageShort(p.age_s), ageState: freshness(p.age_s, 300, 900),
      rows: [["Speed", p.speed_mps != null ? `${Math.round(p.speed_mps / 0.44704)} mph` : null],
             ["Train", esc(p.id)]],
      evidence: "Self-reported by the railway's own train tracking (via Amtraker). Viewer only.",
      hint: pinHint(pinnable),
    }),
  });
  attachHover(map, {
    layers: ["land-plow"], tolerance: 6, key: (p) => p.id,
    html: (p, { pinnable }) => card({
      title: `${p.agency || ""} ${p.label || ""}`.trim() || "Snowplow", chip: G.COL.plow,
      sub: `${esc(p.state)} &middot; winter operations`,
      age: ageShort(p.age_s), ageState: freshness(p.age_s, 180, 600),
      rows: [["Speed", p.speed_mps != null ? `${Math.round(p.speed_mps / 0.44704)} mph` : null]],
      evidence: "Self-reported by the state's own fleet tracking. Viewer only.",
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
      title: p.name || "Traffic camera", chip: p.live ? G.COL.camStream : G.COL.camNone,
      sub: [esc(p.src), esc(p.state), p.note ? esc(p.note) : null].filter(Boolean).join(" &middot; "),
      rows: [["Picture", p.kind === "hls" ? "live video stream" : p.kind === "player"
        ? "the publisher's live player" : "still frame, refreshed by the publisher"],
             ["Watch", p.media ? "click to open it here" : "no picture offered"]],
      evidence: `Where the state can see the road. Played from ${esc(p.src)}'s own servers; nothing is recorded here.`,
    }),
    onClick: (p) => {
      if (!sub.cameras) setLandSub(map, "cameras", true);
      openCamera(map, p);
    },
  });
  attachHover(map, {
    layers: ["land-alpr"], tolerance: 6, key: (p) => p.osm,
    html: (p) => card({
      title: "Licence-plate camera", chip: G.COL.alpr, sub: esc(p.operator || "operator not mapped"),
      rows: [["Maker", p.maker ? esc(p.maker) : null],
             ["Facing", p.facing ? (G.facingDegrees(p.facing).map(compass).join(", ")
               || `${esc(p.facing)} <span class="dim">(as mapped)</span>`) : "not mapped"],
             ["OSM node", esc(p.osm)]],
      evidence: "Context, not evidence. Where a volunteer mapped one, from public OpenStreetMap tags. Never what it reads.",
    }),
  });
}

// WHICH MAP LAYERS EACH TOGGLE SHOWS. Any combination; the state lines show
// with any of them. Work zones go with Transit, as before: the land question
// is the gap between those two.
const TAB_LAYERS = {
  vehicles: ["land-zones-case", "land-zones-line", "land-zones-point", "land-veh-dot"],
  rail: ["land-rail"],
  plows: ["land-plow"],
  cameras: ["land-cams"],
  speeds: ["land-speed"],
  alpr: ["land-alpr-cone", "land-alpr"],
};
const sub = { vehicles: true, rail: false, plows: false, cameras: false, speeds: false, alpr: false };
const ALL_LAYERS = ["land-states-line", ...Object.values(TAB_LAYERS).flat()];

function applyLayers(map) {
  const want = new Set(visible
    ? ["land-states-line", ...Object.keys(sub).filter((k) => sub[k]).flatMap((k) => TAB_LAYERS[k])] : []);
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
  if (fleet.length) map.getSource("land-veh")?.setData(reckonFleet(fleet, Date.now(), "land"));
  if (rail.length) map.getSource("land-rail")?.setData(reckonFleet(rail, Date.now(), "land", { horizon: 300 }));
  if (plows.length) map.getSource("land-plow")?.setData(reckonFleet(plows, Date.now(), "land", { horizon: 180 }));
}

// Compact rows to objects.
const objects = (cols, rows) => rows.map((r) => Object.fromEntries(cols.map((c, i) => [c, r[i]])));

async function pollVehicles(map) {
  if (!sub.vehicles && vehicles) return;    // hidden: keep the last
  const d = await get("/vehicles/us");
  // An API from before 2026-11-02 does not say which feeds are research.
  const research = Array.isArray(d.border_feeds) ? new Set(d.border_feeds) : null;
  const now = d.time;
  let inService = 0;
  fleet = objects(d.cols, d.vehicles).filter((v) => Number.isFinite(v.lon) && Number.isFinite(v.lat))
    .map((v) => {
      const fix = v.vehicle_ts || now;
      const p = {
        feed_id: v.feed_id, vehicle_key: v.vehicle_key, label: v.label || "", route_id: v.route_id || "",
        provider: d.providers[v.feed_id] || "", in_service: !!v.in_service, motion: v.motion || "feed",
        age_from: v.vehicle_ts ? "vehicle" : "poll", fix_t: fix, research: !!research?.has(v.feed_id),
      };
      if (p.in_service) inService += 1;
      p.c = colourOf(p);
      p.mode = modeOf(p.feed_id);
      // The fix's own time, so the projection runs from when the vehicle
      // was there and not from when we happened to ask.
      return { coords: [v.lon, v.lat], t: fix * 1000, heading: v.bearing, speed_mps: v.speed, props: p };
    });
  vehicles = { ...d, vehicles: undefined, n_in_service: inService,
               n_research: research ? fleet.filter((f) => f.props.research).length : null };
  lastPoll = Date.now();
  draw(map);
}

async function pollRail(map) {
  if (!sub.rail) return;
  const d = await get("/vehicles/amtrak");
  trains = d;
  rail = objects(d.cols, d.rows).map((p) => ({
    coords: [p.lon, p.lat], t: (p.ts || d.time) * 1000, heading: p.heading, speed_mps: p.speed_mps, props: p }));
  draw(map);
}

async function pollPlows(map) {
  if (!sub.plows) return;
  const d = await get("/vehicles/plows");
  plowDoc = d;
  plows = objects(d.cols, d.rows).map((p) => ({
    coords: [p.lon, p.lat], t: (p.ts || d.time || Date.now() / 1000) * 1000, heading: p.heading,
    speed_mps: p.speed_mps, props: p }));
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

const CAM_COLS = ["id", "lon", "lat", "name", "src", "kind", "media", "page", "state", "note"];

let camAt = 0;
async function loadCameras(map, force = false) {
  const wait = cams?.loading ? CAM_LOADING_MS : CAM_POLL_MS;
  if (cams && !force && Date.now() - camAt < wait) return;
  const d = await get("/cameras/us");
  camAt = Date.now();
  const rows = objects(d.cols || CAM_COLS, d.rows);
  cams = { ...d, rows, n: rows.length };
  map.getSource("land-cams")?.setData({
    type: "FeatureCollection",
    features: rows.map((r) => ({ type: "Feature", geometry: { type: "Point", coordinates: [r.lon, r.lat] },
                                 properties: { ...r, live: r.kind !== "image" } })),
  });
  if (!document.getElementById("cam-drawer")?.hidden) renderCamList(map, camQuery);
}

async function loadSpeeds(map) {
  if (speeds) return;
  const s = await get("/traffic/status");
  if (!s.flow) throw Object.assign(new Error("no TomTom key: add TOMTOM_API_KEY to .env and restart the API"), { domain: "land" });
  if (!map.getSource("land-speed")) {
    map.addSource("land-speed", { type: "raster", tileSize: 256, maxzoom: 18,
      tiles: [`${ORIGIN}/traffic/flow/{z}/{x}/{y}.png`],
      attribution: "Traffic flow &copy; TomTom" });
    map.addLayer({ id: "land-speed", type: "raster", source: "land-speed",
      layout: { visibility: "none" }, paint: { "raster-opacity": 0.9 } }, "land-states-line");
  }
  speeds = s;
}

// The API fetches the country in the background and answers `loading`
// until it has it (a few minutes the first time); the loop asks again.
async function loadAlpr(map) {
  if (alpr && !alpr.loading) return;
  const d = await get("/context/alpr");
  alpr = d;
  if (d.loading) return;
  map.getSource("land-alpr")?.setData({
    type: "FeatureCollection",
    features: d.rows.map(([lon, lat, osm, operator, maker, facing]) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] },
      properties: { osm, operator, maker, facing } })),
  });
  map.getSource("land-alpr-cone")?.setData({
    type: "FeatureCollection",
    features: d.rows.flatMap(([lon, lat, , , , facing]) => G.facingDegrees(facing).map((rot) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: { rot } }))),
  });
}

// Each toggle's first load. A failure belongs to its toggle, not to Land:
// a missing TomTom key must not say the vehicles are down.
const LOADERS = {
  vehicles: pollVehicles, rail: pollRail, plows: pollPlows, cameras: (m) => loadCameras(m),
  speeds: loadSpeeds, alpr: loadAlpr,
};

async function loadSub(map, name) {
  try {
    await LOADERS[name](map);
    delete failed[name];
  } catch (e) {
    failed[name] = e.kind === "offline" ? "API not reachable"
      : e.status === 502 ? "the source did not answer" : String(e.message || e);
    if (name === "vehicles") throw e;
  }
  applyLayers(map);
}

// Polling only while the layer is on: a land view nobody is looking at
// should not ask the API for anything.
export function startLand(map, onUpdate, onError) {
  if (running) return;
  running = true;
  const loop = (fn, ms, when = () => true) => {
    const go = async () => {
      if (visible && !document.hidden && when()) {
        try { await fn(map); onUpdate(); } catch (e) { onError(e); }
      }
      setTimeout(go, typeof ms === "function" ? ms() : ms);
    };
    go();
  };
  loop(pollVehicles, VEHICLE_POLL_MS, () => sub.vehicles);
  loop(pollStatus, STATUS_POLL_MS);
  loop(pollZones, ZONE_POLL_MS);
  loop((m) => loadSub(m, "rail"), RAIL_POLL_MS, () => sub.rail);
  loop((m) => loadSub(m, "plows"), PLOW_POLL_MS, () => sub.plows);
  loop((m) => loadSub(m, "cameras"), () => (cams?.loading ? CAM_LOADING_MS : 60000), () => sub.cameras);
  loop((m) => loadSub(m, "alpr"), 10000, () => sub.alpr && (!alpr || alpr.loading));
  setInterval(() => { if (visible && !document.hidden) draw(map); }, DRAW_MS);
}

export function setLandAoi(map, name) {
  aoi = name === "conus" ? "conus" : "air";
  zones = null;
  if (visible) pollZones(map).catch(() => {});
}

// Called when the layer is switched on, so it does not wait out a timer.
export function refreshLand(map, onUpdate, onError) {
  Promise.all([pollVehicles(map), pollStatus(), pollZones(map),
               ...Object.keys(sub).filter((k) => sub[k] && k !== "vehicles").map((k) => loadSub(map, k))])
    .then(onUpdate, onError);
}

export const landInfo = () => ({ vehicles, status, zones, cams, trains, plows: plowDoc, speeds, alpr,
                                 failed: { ...failed }, sub: { ...sub }, lastPoll, requests, visible, aoi });

// --- cameras: the drawer and the player ---------------------------------------
//
// A list of the cameras in view (the whole country is tens of thousands),
// and a player for the one chosen, from ITS PUBLISHER'S OWN SERVERS. Three
// kinds of picture, as in the 3D view:
//   player  Maryland CHART's own player, in a sandboxed frame
//   hls     a live stream (DelDOT), played with hls.js where the browser
//           cannot play HLS itself
//   image   a still frame the publisher refreshes; asked for again every
//           ten seconds while the player is open
// Nothing is recorded, stored or re-served here.

const LIST_MAX = 300;
let camQuery = "";

function camEl(map) {
  let d = document.getElementById("cam-drawer");
  if (d) return d;
  d = document.createElement("div");
  d.id = "cam-drawer";
  d.hidden = true;
  d.innerHTML = `
    <div class="head"><div class="t">Traffic cameras &middot; in view</div>
      <input type="search" placeholder="Search road, place or state" aria-label="Search cameras">
      <div class="meta" id="cam-count" style="margin:6px 0 0"></div></div>
    <div class="list"></div>
    <div class="foot">Played from each publisher's own servers. This project
      does not record, store or republish any picture. Cameras are the
      state's own observation points, separate from the vehicles and the
      declared work zones.</div>`;
  document.body.appendChild(d);
  const v = document.createElement("div");
  v.id = "cam-viewer";
  v.hidden = true;
  document.body.appendChild(v);
  d.querySelector("input").oninput = (e) => { camQuery = e.target.value; renderCamList(map, camQuery); };
  map.on("moveend", () => { if (!d.hidden && !camQuery) renderCamList(map, ""); });
  return d;
}

function renderCamList(map, q = "") {
  const d = camEl(map);
  const list = d.querySelector(".list");
  const rows = cams?.rows || [];
  const needle = q.trim().toLowerCase();
  const b = map.getBounds();
  // Searching looks across the country; otherwise the list is what is on
  // screen, so it matches the map.
  const shown = (needle
    ? rows.filter((r) => `${r.name} ${r.state} ${r.src}`.toLowerCase().includes(needle))
    : rows.filter((r) => b.contains([r.lon, r.lat])))
    .sort((x, y) => (x.state || "").localeCompare(y.state || "") || (x.name || "").localeCompare(y.name || ""));
  const nLive = rows.filter((r) => r.kind !== "image").length;
  d.querySelector("#cam-count").textContent = cams
    ? `${shown.length.toLocaleString()} ${needle ? "match" : "in view"}${shown.length > LIST_MAX ? `, first ${LIST_MAX}` : ""}`
      + ` · ${rows.length.toLocaleString()} in the US, ${nLive.toLocaleString()} with video${cams.loading ? " · still loading" : ""}`
    : failed.cameras ? `Not loaded: ${failed.cameras}` : "loading…";
  list.replaceChildren(...shown.slice(0, LIST_MAX).map((r) => {
    const el = document.createElement("button");
    el.className = "cam" + (r.media ? "" : " nostream");
    el.dataset.id = r.id;
    const name = document.createElement("span");
    name.textContent = r.name || "(unnamed)";
    const c = document.createElement("span");
    c.className = "c";
    c.textContent = `${r.state || ""} · ${r.src}${r.kind === "image" ? " · still" : " · video"}`;
    el.append(name, c);
    el.onclick = () => {
      // Centred in the part of the map the drawer and the player leave open.
      map.easeTo({ center: [r.lon, r.lat], zoom: Math.max(map.getZoom(), 12),
                   padding: { left: 330, bottom: 360, top: 20, right: 20 }, duration: 800 });
      openCamera(map, r);
    };
    return el;
  }));
}

let frameTimer = 0, hlsPlayer = null;

function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  return new Promise((ok, no) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js";
    s.onload = () => ok(window.Hls);
    s.onerror = () => no(new Error("hls.js did not load"));
    document.head.append(s);
  });
}

function stopPicture() {
  clearInterval(frameTimer);
  if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
}

const note = (text) => Object.assign(document.createElement("div"), { className: "msg", textContent: text });

// A stream is asked about before it is played, so one that wants a
// password never puts the publisher's sign-in box in front of anyone
// (DriveNC did, 2026-11-02). If it will not play, its still frame is shown.
async function playable(c) {
  if (c.kind !== "hls") return { c, why: "" };
  const pr = await get(`/cameras/probe?id=${encodeURIComponent(c.id)}`)
    .catch(() => ({ ok: false, why: "the API could not check the stream" }));
  if (pr.ok) return { c, why: "" };
  return { c: c.still ? { ...c, kind: "image", media: c.still } : { ...c, media: null }, why: pr.why };
}

let openSeq = 0;

export async function openCamera(map, c0) {
  camEl(map);
  stopPicture();
  const seq = ++openSeq;
  const { c, why } = await playable(c0);
  if (seq !== openSeq) return;          // another camera was chosen meanwhile
  const v = document.getElementById("cam-viewer");
  document.querySelectorAll("#cam-drawer .cam").forEach(
    (b) => b.classList.toggle("on", b.dataset.id === String(c.id)));
  v.hidden = false;
  const bar = document.createElement("div");
  bar.className = "bar";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = `${c.name || "Camera"}${c.state ? " · " + c.state : ""}`;
  const close = document.createElement("button");
  close.textContent = "×";
  close.title = "Close";
  close.onclick = () => { stopPicture(); v.hidden = true; v.replaceChildren(); };
  bar.append(name);
  const link = (href, text) => {
    const a = document.createElement("a");
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = text;
    bar.append(a);
  };
  if (c.page) link(c.page, "publisher's page");
  v.replaceChildren(bar);
  const src = document.createElement("div");
  src.className = "src";
  src.textContent = `${why ? `Video not shown: ${why}. ` : ""}Picture: ${c.src}, from its own servers. Not recorded or stored here.`;
  if (!c.media) {
    bar.append(close);
    v.append(note(why ? `The live video will not play here: ${why}. Use the publisher's page.`
      : "This camera offers no picture to play here."), src);
    return;
  }
  if (c.kind === "player") {
    let s = null;
    try { s = await get(c.media); } catch { s = null; }
    if (s?.page && !c.page) link(s.page, "publisher's page");
    bar.append(close);
    if (s?.embed && s.embeddable !== false) {
      const f = document.createElement("iframe");
      // The publisher's player, from the publisher. Scripts are allowed
      // because a player needs them; the page cannot reach this one
      // (sandboxed, cross-origin).
      f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups");
      f.setAttribute("allow", "autoplay; fullscreen");
      f.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
      f.src = s.embed;
      v.append(f);
    } else {
      v.append(note(s?.page
        ? "The publisher does not allow its player inside other pages. Use its page."
        : "No stream link on this machine. Run: python scripts/fetch_road_mask.py --layer cameras"));
    }
  } else if (c.kind === "hls") {
    bar.append(close);
    const vid = document.createElement("video");
    vid.muted = true; vid.autoplay = true; vid.playsInline = true; vid.controls = true;
    v.append(vid);
    if (vid.canPlayType("application/vnd.apple.mpegurl")) vid.src = c.media;
    else {
      try {
        const Hls = await loadHls();
        hlsPlayer = new Hls({ lowLatencyMode: true });
        hlsPlayer.loadSource(c.media);
        hlsPlayer.attachMedia(vid);
      } catch (e) { vid.replaceWith(note(String(e.message))); }
    }
  } else {
    bar.append(close);
    const img = document.createElement("img");
    img.alt = c.name || "camera frame";
    img.referrerPolicy = "no-referrer";
    const url = c.media.startsWith("/") ? `${API}${c.media}` : c.media;
    const show = () => { img.src = `${url}${url.includes("?") ? "&" : "?"}t=${Math.floor(Date.now() / 10e3)}`; };
    img.onerror = () => { clearInterval(frameTimer); img.replaceWith(note("The publisher did not send a frame.")); };
    show();
    v.append(img);
    frameTimer = setInterval(show, 10e3);
  }
  v.append(src);
}

// Moves the map ONLY if none of `box` is in view. Switching a layer on used
// to fly the map somewhere every time, which threw away a view the user had
// chosen. If what was asked for is already partly on screen, the view stays.
export function ensureInView(map, box, padding = 40) {
  const b = map.getBounds();
  const overlap = !(box[2] < b.getWest() || box[0] > b.getEast()
                    || box[3] < b.getSouth() || box[1] > b.getNorth());
  if (!overlap) map.fitBounds(box, { padding, duration: 900 });
}

// Each toggle on or off. While Land is on, switching off the last one does
// nothing, as with the domains; `force` is for Land itself going off, which
// closes the cameras whatever else is on.
export async function setLandSub(map, name, on, { force = false } = {}) {
  if (!(name in sub)) return;
  if (!on && !force && Object.keys(sub).every((k) => k === name || !sub[k])) return;
  sub[name] = on;
  if (force && !Object.values(sub).some(Boolean)) sub.vehicles = true;
  document.querySelectorAll("#landtabs button").forEach(
    (b) => b.classList.toggle("on", !!sub[b.dataset.landtab]));
  applyLayers(map);
  if (on && visible && name !== "cameras") await loadSub(map, name).catch(() => {});
  if (name !== "cameras") return;
  const d = camEl(map);
  d.hidden = !sub.cameras;
  if (!sub.cameras) {
    stopPicture();
    const v = document.getElementById("cam-viewer");
    v.hidden = true; v.replaceChildren();
    return;
  }
  renderCamList(map, camQuery);
  await loadSub(map, "cameras");
  renderCamList(map, camQuery);
}

export const landSub = () => ({ ...sub });

// What each Land toggle is drawing now, for the search box (finder.js).
export const landFleets = () => {
  const off = !visible;
  return { transit: off || !sub.vehicles ? [] : fleet, rail: off || !sub.rail ? [] : rail,
           plows: off || !sub.plows ? [] : plows, cams: off || !sub.cameras ? [] : (cams?.rows || []) };
};
