// Live vessel layer. AIS, via /live?domain=sea.
//
// Structurally the twin of live.js -- same poll, same dead reckoning, same
// stale handling -- but the encoding is its own, and every channel on it
// carries one thing:
//
//   COLOUR  what kind of vessel, in FIVE groups, not eleven.
//   SHAPE   under way (a hull pointing along its course) or stopped (a dot).
//   SIZE    declared length.
//   LINE    where it will be in six minutes at its current course and speed.
//   FADE    not heard for six minutes or more.
//
// WHY FIVE COLOURS AND NOT ELEVEN
//
// The first version gave each of eleven AIS categories its own hue. On a map
// that fails by construction: any two vessels can sit side by side, so every
// PAIR of colours has to stay distinguishable, including under colour-vision
// deficiency, and that is a far stricter test than a legend of stripes. The
// palette validator passes at most three categorical hues on an all-pairs
// display. Eleven was not a design, it was noise.
//
// So colour picks out the three groups a dark-vessel question actually turns
// on, and everything else is context:
//
//   commercial   cargo, tanker, passenger. Big, Class A by law, and big
//                enough for Sentinel-1 to see -- the population the SAR
//                subtraction is about.
//   fishing      the classic case of a vessel going quiet on purpose.
//   government   military and law enforcement, which broadcast irregularly
//                BY DESIGN. The confounder, made visible rather than buried.
//   other        tugs, service craft, pleasure boats, declared 'other'. Grey.
//   undeclared   told us nothing about its type. Hollow -- an absence of
//                information, drawn as one.
//
// Hues validated with the dataviz validator against the basemap's actual
// water colour (#c2c8ca), all pairs: worst CVD separation 13.0, worst
// normal-vision separation 16.3, both passing. Contrast against grey water is
// below 3:1 for two of them, which is why every mark wears a white ring.

import { reckonFleet, project } from "./reckon.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
export const POLL_MS = 10000;

// A vessel swinging at anchor reports 0.1-0.4 kn of drift. Below this it is
// drawn stopped, with no course and no leader: a heading on a moored ship is
// noise, and an arrow on it would claim a direction it does not have.
const MOVING_KN = 0.5;

// How far ahead the course leader reaches. Six minutes is the radar
// convention -- a tenth of an hour, so a leader's length in nautical miles
// read off the scale bar is the speed in knots divided by ten.
const LEADER_S = 360;

export const GROUPS = [
  { key: "commercial", label: "Commercial", color: "#2a78d6",
    subtypes: ["cargo", "tanker", "passenger", "highspeed"] },
  { key: "fishing", label: "Fishing", color: "#eb6834",
    subtypes: ["fishing"] },
  { key: "government", label: "Gov / military", color: "#4a3aa7",
    subtypes: ["government"] },
  { key: "other", label: "Other traffic", color: "#6b7c89",
    subtypes: ["tug", "service", "pleasure", "other"] },
  { key: "unknown", label: "Undeclared", color: null,
    subtypes: ["unknown"] },
];

const HOLLOW = "#6b7c89";          // stroke of an undeclared vessel
export const SUBTYPE_LABEL = {
  cargo: "cargo", tanker: "tanker", passenger: "passenger",
  highspeed: "high-speed", fishing: "fishing", government: "gov/military",
  tug: "tug", service: "service", pleasure: "pleasure", other: "other",
  unknown: "undeclared",
};

export const GROUP_OF = Object.fromEntries(
  GROUPS.flatMap((g) => g.subtypes.map((s) => [s, g.key])));
const GROUP_LABEL = Object.fromEntries(GROUPS.map((g) => [g.key, g.label]));

// Draw order: undeclared at the bottom, grey context above it, the three
// coloured groups on top. In a crowded anchorage the vessels that answer the
// question should not be buried under the ones that do not.
const RANK = { unknown: 0, other: 1, commercial: 2, fishing: 3, government: 4 };

const FILL = ["match", ["get", "group"],
  ...GROUPS.filter((g) => g.color).flatMap((g) => [g.key, g.color]),
  "#ffffff"];
const RING = ["match", ["get", "group"], "unknown", HOLLOW, "#ffffff"];

// Size carries declared LENGTH. A 300 m container ship and a 12 m skiff are
// the two ends of every question this project asks, and a map that draws
// them the same size has thrown that away. Undeclared length falls back to a
// middling 50 m rather than the smallest size, so an unidentified vessel is
// not made visually negligible.
const LEN = ["coalesce", ["get", "length_m"], 50];
const byLength = (small, mid, large) =>
  ["interpolate", ["linear"], LEN, 10, small, 100, mid, 350, large];

// --- symbols --------------------------------------------------------------

// A spear, not a paddle. The first hull was an ogive -- rounded bow, square
// transom -- and at map scale the broad end read as the front, so every
// vessel appeared to be steaming backwards with its course line trailing from
// the stern. Rendered and looked at, it fooled its own author. This one is
// widest near the stern and tapers to a point, so the silhouette says
// "forward" at eight pixels.
export const HULL_PATH =
  "M0,-21 L7.5,9 L6,17 L-6,17 L-7.5,9 Z";

function hullImages(map) {
  for (const g of GROUPS) {
    const id = `hull-${g.key}`;
    if (map.hasImage(id)) continue;
    const s = 56, c = document.createElement("canvas");
    c.width = c.height = s;
    const x = c.getContext("2d");
    x.translate(s / 2, s / 2 + 1);
    // Bow up; the icon rotates to course, and course is half of what it is
    // for.
    const p = new Path2D(HULL_PATH);
    x.lineJoin = "round";
    // 1.5 css px of white ring. Thicker and the grey hulls of 'other traffic'
    // read as white slivers, because the spear is narrow.
    x.lineWidth = 3;
    x.strokeStyle = g.color ? "#ffffff" : HOLLOW;
    x.stroke(p);
    x.fillStyle = g.color ?? "rgba(255,255,255,.85)";
    x.fill(p);
    if (!g.color) { x.lineWidth = 2; x.stroke(p); }
    map.addImage(id, x.getImageData(0, 0, s, s), { pixelRatio: 2 });
  }
}

const LAYERS = ["vessels-leader", "vessels-dot", "vessels-hull", "vessels-label"];

export function addVessels(map) {
  hullImages(map);
  const empty = { type: "FeatureCollection", features: [] };
  map.addSource("vessels", { type: "geojson", data: empty });
  map.addSource("vessel-leaders", { type: "geojson", data: empty });

  map.addLayer({
    id: "vessels-leader",
    type: "line",
    source: "vessel-leaders",
    minzoom: 7,
    layout: { visibility: "none", "line-cap": "round" },
    paint: {
      "line-color": ["match", ["get", "group"],
        ...GROUPS.filter((g) => g.color).flatMap((g) => [g.key, g.color]),
        HOLLOW],
      "line-width": 1.5,
      "line-opacity": 0.75,
    },
  });

  map.addLayer({
    id: "vessels-dot",
    type: "circle",
    source: "vessels",
    filter: ["!=", ["get", "moving"], true],
    layout: { visibility: "none", "circle-sort-key": ["get", "rank"] },
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"],
        6, byLength(2.5, 3.5, 5.5),
        13, byLength(4, 7, 12)],
      "circle-color": FILL,
      // Fade is applied to fill and ring together, so a stale vessel reads
      // as one ghosted mark rather than a bright ring round a pale centre.
      "circle-opacity": ["case", ["get", "stale"], 0.3,
        ["==", ["get", "group"], "unknown"], 0.7, 1],
      "circle-stroke-color": RING,
      "circle-stroke-width": ["case", ["==", ["get", "group"], "unknown"], 1.5, 1.2],
      "circle-stroke-opacity": ["case", ["get", "stale"], 0.35, 1],
    },
  });
  map.addLayer({
    id: "vessels-hull",
    type: "symbol",
    source: "vessels",
    filter: ["==", ["get", "moving"], true],
    layout: {
      visibility: "none",
      "icon-image": ["concat", "hull-", ["get", "group"]],
      "icon-size": ["interpolate", ["linear"], ["zoom"],
        6, byLength(0.34, 0.46, 0.7),
        13, byLength(0.55, 0.9, 1.5)],
      "icon-rotate": ["coalesce", ["get", "heading"], 0],
      "icon-rotation-alignment": "map",
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
      "symbol-sort-key": ["get", "rank"],
    },
    paint: { "icon-opacity": ["case", ["get", "stale"], 0.35, 1] },
  });

  // Own layer, in a try/catch, for the same reason as the aircraft labels: a
  // fontstack the basemap does not ship makes MapLibre drop the whole layer
  // it is declared on, and the worst case must be unlabelled vessels rather
  // than no vessels.
  try {
    map.addLayer({
      id: "vessels-label",
      type: "symbol",
      source: "vessels",
      minzoom: 10,
      layout: {
        visibility: "none",
        "text-field": ["get", "label"],
        "text-size": 10.5,
        "text-offset": [0, 1.3],
        "text-anchor": "top",
        "text-allow-overlap": false,
        "text-optional": true,
        "symbol-sort-key": ["-", 0, ["get", "rank"]],
      },
      paint: {
        "text-color": "#0e1a24",
        "text-halo-color": "rgba(255,255,255,.95)",
        "text-halo-width": 1.6,
        "text-opacity": ["case", ["get", "stale"], 0.4, 1],
      },
    });
  } catch (e) {
    console.warn("[angels] vessel labels unavailable:", e.message);
  }

  const popup = new maplibregl.Popup({
    closeButton: false, closeOnClick: false, className: "track-popup",
  });
  const show = (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = e.features[0].properties;
    const kn = p.sog_kn != null ? `${Number(p.sog_kn).toFixed(1)} kn` : "speed n/a";
    const dims = p.length_m ? `${Math.round(p.length_m)} m` : "length undeclared";
    const age = p.age_s != null ? `${Math.round(p.age_s)}s ago` : "";
    const motion = p.moving
      ? `${kn} &middot; course ${p.heading != null ? Math.round(p.heading) + "&deg;" : "n/a"}`
      : `stopped${p.sog_kn != null ? ` (${Number(p.sog_kn).toFixed(1)} kn)` : ""}`;
    popup.setLngLat(e.lngLat).setHTML(
      `<strong>${p.label}</strong> <span style="opacity:.6">${p.mmsi}</span><br>` +
      `${GROUP_LABEL[p.group]}${p.group === "commercial" || p.group === "other"
        ? ` &middot; ${SUBTYPE_LABEL[p.subtype] ?? p.subtype}` : ""} &middot; ${dims}<br>` +
      motion +
      (p.destination ? `<br>for ${p.destination}` : "") +
      `<br><span style="opacity:.6">class ${p.ais_class} &middot; heard ${age}</span>`
    ).addTo(map);
  };
  const hide = () => { map.getCanvas().style.cursor = ""; popup.remove(); };
  for (const id of ["vessels-dot", "vessels-hull"]) {
    map.on("mouseenter", id, show);
    map.on("mouseleave", id, hide);
  }
}

let fleet = [];
let lastPoll = 0;
let running = false;
let requests = 0;
let lastMeta = null;
let visible = false;
let wanted = null;            // null = every group

export function setVisible(map, on) {
  visible = on;
  for (const id of LAYERS) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    }
  }
}

// Filtering happens in the BROWSER, not in the query.
//
// The server counts everything it heard whatever the filter says, and this
// keeps the panel's totals stable while the map narrows. Ask the server to
// filter instead and hiding "Commercial" makes its count vanish, which reads
// as the traffic disappearing rather than as a view being narrowed.
export function setGroups(map, groups) {
  wanted = groups && groups.length ? new Set(groups) : null;
  draw(map, Date.now());
}

function draw(map, now) {
  const shown = wanted ? fleet.filter((v) => wanted.has(v.props.group)) : fleet;
  const fc = reckonFleet(shown, now, "sea");
  map.getSource("vessels")?.setData(fc);

  // Leaders are drawn from the RECKONED position, so the line and the hull
  // move together between polls rather than the hull sliding off its own
  // course line.
  const leaders = [];
  for (const f of fc.features) {
    const p = f.properties;
    if (!p.moving || p.stale || p.heading == null || !p.speed_mps) continue;
    const [lon, lat] = f.geometry.coordinates;
    leaders.push({
      type: "Feature",
      geometry: { type: "LineString",
        coordinates: [[lon, lat], project(lat, lon, p.heading, p.speed_mps * LEADER_S)] },
      properties: { group: p.group },
    });
  }
  map.getSource("vessel-leaders")?.setData({ type: "FeatureCollection", features: leaders });
}

// --- polling --------------------------------------------------------------

export async function pollVessels(onUpdate) {
  let r;
  try {
    r = await fetch(`${API}/live?domain=sea`);
  } catch {
    const err = new Error("The API is not reachable.");
    err.kind = "offline";
    err.domain = "sea";
    throw err;
  }
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail ?? ""; } catch { /* not json */ }
    const err = new Error(detail || `HTTP ${r.status}`);
    err.kind = "http";
    err.status = r.status;
    err.detail = detail;
    err.domain = "sea";
    throw err;
  }
  const data = await r.json();

  fleet = data.features.map((f) => {
    const p = f.properties;
    const group = GROUP_OF[p.subtype] ?? "other";
    const moving = p.sog_kn != null && p.sog_kn >= MOVING_KN;
    return {
      coords: f.geometry.coordinates,
      t: p.t * 1000,
      // A stopped vessel is given no speed at all, so the reckoner cannot
      // drift it across the berth on a knot of anchor swing.
      speed_mps: moving ? p.speed_mps : 0,
      heading: p.heading,
      props: { ...p, group, moving, rank: RANK[group] ?? 1 },
    };
  });
  lastPoll = Date.now();
  lastMeta = data.properties;
  requests += 1;
  onUpdate?.(data);
  return data;
}

export function startVessels(map, onUpdate, onError) {
  if (running) return;
  running = true;

  const tick = () => {
    draw(map, Date.now());
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  // Polling starts immediately even when the sea layer is hidden, and that
  // is the point: the first request is what opens the server's socket, and
  // the socket needs several minutes of listening before its table means
  // anything. Waiting for the user to click "Sea" would hand them a warming
  // bar instead of a harbour.
  //
  // Backoff on repeated failure, though. A missing AIS key on a session that
  // never looks at the sea should not generate a request every ten seconds
  // until the browser is closed.
  let fails = 0;
  const poll = async () => {
    try {
      await pollVessels(onUpdate);
      fails = 0;
    } catch (e) {
      fails += 1;
      onError?.(e);
    }
    setTimeout(poll, Math.min(POLL_MS * Math.max(1, fails), 120000));
  };
  poll();
}

export const vesselInfo = () => ({
  n: fleet.length,
  shown: wanted ? fleet.filter((v) => wanted.has(v.props.group)).length
                : fleet.length,
  moving: fleet.filter((v) => v.props.moving).length,
  lastPoll,
  requests,
  meta: lastMeta,
  visible,
  wanted: wanted ? [...wanted] : null,
});
