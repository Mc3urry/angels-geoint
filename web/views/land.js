// Land layer: transit vehicles, declared work zones, and the state lines.
//
// Everything here is read from what the road collectors already wrote. The
// page never asks an agency for anything; see angels/core/land.py.
//
// THREE THINGS ON ONE MAP, AND NONE OF THEM IS THE FINDING
//
//   vehicles    transit fleets reporting their own positions (G2c)
//   work zones  agencies declaring where their roads are disrupted (G1)
//   state lines the discontinuity the land question is about (G2b)
//
// The land finding is the gap between the first two: slowdowns the vehicles
// show that no agency declared. That is G6, and it does not exist yet. So
// this view shows the ingredients side by side and says, every render, that
// the comparison is not on the map.

import { esc } from "./popup.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
export const VEHICLE_POLL_MS = 30000;   // the collector polls every 30 s
const STATUS_POLL_MS = 60000;
const ZONE_POLL_MS = 600000;            // WZDx is polled every 10 min
const WINDOW_MIN = 15;

const BOX = { air: [-77.7, 38.3, -76.0, 39.6], conus: [-125.0, 24.0, -66.5, 49.5] };
const PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#8c564b",
                 "#e377c2", "#17becf", "#bcbd22", "#ff7f0e", "#7f7f7f"];

let visible = false;
let running = false;
let aoi = "air";
let vehicles = null, status = null, zones = null;
let lastPoll = 0, requests = 0;

const colourOf = (feed) => {
  let h = 0;
  for (const ch of String(feed)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
};

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
  map.addSource("land-states", { type: "geojson", data: empty() });
  map.addSource("land-zones", { type: "geojson", data: empty() });
  map.addSource("land-veh", { type: "geojson", data: empty() });

  map.addLayer({ id: "land-states-line", type: "line", source: "land-states",
    paint: { "line-color": "#3b3b3b", "line-width": 1.6,
             "line-dasharray": [3, 2], "line-opacity": 0.8 },
    layout: { visibility: "none" } });
  map.addLayer({ id: "land-zones-line", type: "line", source: "land-zones",
    filter: ["!=", ["geometry-type"], "Point"],
    paint: { "line-color": "#e8590c", "line-width": 4, "line-opacity": 0.75 },
    layout: { visibility: "none", "line-cap": "round" } });
  map.addLayer({ id: "land-zones-point", type: "circle", source: "land-zones",
    filter: ["==", ["geometry-type"], "Point"],
    paint: { "circle-radius": 4, "circle-color": "#e8590c",
             "circle-stroke-color": "#fff", "circle-stroke-width": 1 },
    layout: { visibility: "none" } });
  map.addLayer({ id: "land-veh-dot", type: "circle", source: "land-veh",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 2, 10, 4, 14, 7],
      "circle-color": ["get", "c"],
      // Faded with age: a fix twelve minutes old is drawn as one.
      "circle-opacity": ["interpolate", ["linear"], ["get", "age_s"],
                         0, 0.95, 600, 0.35],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 0.8 },
    layout: { visibility: "none" } });

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
  const show = (html) => (e) => {
    map.getCanvas().style.cursor = "pointer";
    popup.setLngLat(e.lngLat).setHTML(html(e.features[0].properties)).addTo(map);
  };
  const hide = () => { map.getCanvas().style.cursor = ""; popup.remove(); };
  map.on("mousemove", "land-veh-dot", show((p) => `
    <div class="card"><strong>${esc(p.provider || p.feed_id)}</strong>
    ${p.label ? ` &middot; vehicle ${esc(p.label)}` : ""}
    ${p.route_id ? `<br>route ${esc(p.route_id)}` : ""}
    <br>fix ${Math.round(p.age_s / 60)} min old (${p.age_from === "vehicle"
      ? "the vehicle's own clock" : "our poll's clock; the feed sent none"})
    <br><em>Self-reported by the agency's vehicle. Nothing independent saw it.</em></div>`));
  map.on("mouseleave", "land-veh-dot", hide);
  for (const id of ["land-zones-line", "land-zones-point"]) {
    map.on("mousemove", id, show((p) => `
      <div class="card"><strong>Work zone</strong> &middot; ${esc(p.feed)}
      ${p.road ? `<br>${esc(p.road)} ${esc(p.direction)}` : ""}
      ${p.impact ? `<br>${esc(p.impact)}` : ""}
      <br>${esc((p.start || "").slice(0, 10))} to ${esc((p.end || "open").slice(0, 10))}
      <br><em>Declared by the agency. Not observed.</em></div>`));
    map.on("mouseleave", id, hide);
  }
}

export function setVisible(map, on) {
  visible = on;
  for (const id of ["land-states-line", "land-zones-line", "land-zones-point",
                    "land-veh-dot"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  }
}

async function pollVehicles(map) {
  const fc = await get(`/land/vehicles?minutes=${WINDOW_MIN}`);
  for (const f of fc.features) f.properties.c = colourOf(f.properties.feed_id);
  vehicles = fc;
  lastPoll = Date.now();
  map.getSource("land-veh")?.setData(fc);
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

// Polling only while the layer is on: a land view nobody is looking at
// should not read parquet every thirty seconds.
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
}

export function setLandAoi(map, name) {
  aoi = name === "conus" ? "conus" : "air";
  zones = null;
  if (visible) pollZones(map).catch(() => {});
}

// Called when the layer is switched on, so it does not wait out a timer.
export function refreshLand(map, onUpdate, onError) {
  Promise.all([pollVehicles(map), pollStatus(), pollZones(map)])
    .then(onUpdate, onError);
}

export const landInfo = () => ({ vehicles, status, zones, lastPoll, requests,
                                 visible, aoi });
