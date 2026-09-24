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
import { attachHover, card, compass, age as ageText, ageShort, crowd, esc, flagOf,
         freshness, mmsiKind, pinHint } from "./popup.js";
import { Wake } from "./wake.js";

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
// A HULL, not a spear. The old glyph was a straight-sided triangle, which
// reads as an arrowhead; this one has the three things that make a shape say
// "ship" rather than "pointer" -- a fine bow, parallel sides amidships and a
// SQUARE TRANSOM. The flat stern is what does most of the work: an arrowhead
// is symmetric front to back at small sizes and a transom never is.
export const HULL_PATH =
  "M0,-21 C4.2,-14.5 7.2,-6 7.4,2 L6.6,16 L-6.6,16 L-7.4,2 C-7.2,-6 -4.2,-14.5 0,-21 Z";

// Beam and length per group, applied to the one hull. Colour alone carries
// the group today, which fails for a colour-blind reader and fails again in
// a printed figure; a trawler that is visibly stubby and a cutter that is
// visibly lean survive both. The proportions are not decoration -- they are
// roughly the real ones.
const HULL_SHAPE = {
  commercial: [1.0, 1.0],
  fishing:    [1.22, 0.78],    // beamy and short
  government: [0.84, 1.1],     // lean and long
  other:      [1.0, 1.0],
  unknown:    [1.0, 1.0],
};

function hullImages(map) {
  for (const g of GROUPS) {
    const id = `hull-${g.key}`;
    if (map.hasImage(id)) continue;
    const s = 64, c = document.createElement("canvas");
    c.width = c.height = s;
    const x = c.getContext("2d");
    x.translate(s / 2, s / 2 + 1);
    const [beam, len] = HULL_SHAPE[g.key] ?? [1, 1];
    x.scale(beam, len);
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
    map.addImage(id, x.getImageData(0, 0, s, s), { pixelRatio: 2.4 });
  }
}

const LAYERS = ["vessel-wake", "vessels-leader", "vessels-dot", "vessels-hull",
                "vessels-label", "vessels-hit"];

export function addVessels(map) {
  hullImages(map);
  const empty = { type: "FeatureCollection", features: [] };
  map.addSource("vessel-wakes", { type: "geojson", data: empty });
  map.addSource("vessels", { type: "geojson", data: empty });

  // Added before every vessel layer, so a wake always passes UNDER the hulls
  // rather than over them. Thin, and in the group's own colour so a wake can
  // be followed back to the vessel that made it in a crowded approach.
  map.addLayer({
    id: "vessel-wake",
    type: "line",
    source: "vessel-wakes",
    layout: { "line-cap": "round", "line-join": "round", visibility: "none" },
    paint: {
      "line-color": FILL,
      "line-width": ["interpolate", ["linear"], ["zoom"], 5, 1.0, 12, 2.2],
      "line-opacity": 0.55,
    },
  });
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

  // See live.js: an invisible circle, generously sized, so that hovering a
  // three-pixel hull at national zoom is an interaction rather than a test
  // of mouse control.
  map.addLayer({
    id: "vessels-hit", type: "circle", source: "vessels",
    paint: {
      "circle-opacity": 0,
      // SIZED BY MEASUREMENT, not by guess. The first version used 9-16px,
      // and probing queryRenderedFeatures at increasing offsets showed it
      // matched the drawn symbol's own reach almost exactly -- the layer
      // existed, cost a layer, and extended the target by nothing.
      //
      // 18px at working zoom is about a 36px target, which is a comfortable
      // mouse target and still small enough to pick one contact out of a
      // cluster. Where it cannot, the card says how many others it is
      // covering rather than pretending it chose.
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 14, 8, 18, 12, 22],
    },
  }, "vessels-dot");

  attachHover(map, {
    layers: ["vessels-hit", "vessels-hull", "vessels-dot"],
    key: (p) => p.mmsi ?? null,
    html: vesselCard,
  });
}

let fleet = [];
let lastPoll = 0;
let running = false;
let requests = 0;
let lastMeta = null;
let visible = false;
let wanted = null;            // null = every group
let seaAoi = "air";           // which box; the socket widens, never narrows
let seaKick = null;           // poll(), so a box switch does not wait it out
let seaTimer = null;
let wakeOn = false;
let lastDraw = 0;

// HOW OFTEN THE SEA IS REDRAWN, AND WHY IT IS NOT EVERY FRAME.
//
// This layer used to call draw() from requestAnimationFrame unconditionally,
// which meant, sixty times a second: filter the fleet, build a brand-new
// FeatureCollection of every vessel, setData it, then build the leader lines
// and setData those too.
//
// On the Chesapeake box, four hundred vessels, that is wasteful. On the
// national box it crashed the tab -- "Aw, Snap! Out of Memory". The reason
// is not garbage collection falling behind, though five thousand fresh
// feature objects a frame is about 150 MB/s of it. It is that a GeoJSON
// source's setData ships the whole collection to MapLibre's worker to be
// re-indexed, and at 60 Hz the work arrives faster than the worker can
// finish it. The queue grows without bound, each entry holding another copy
// of five thousand features, and the renderer dies holding all of them.
//
// live.js learned this already -- read the comment on its ring animation,
// which says in as many words "ONCE A SECOND, NOT SIXTY TIMES". The sea
// layer is described at the top of this file as "structurally the twin of
// live.js", and it was, except for the one part that had been fixed. The
// same asymmetry as _process_alive: a principle established on one side and
// never carried to the other.
//
// So: animate only when animating is cheap AND there is something to
// animate. Above this many vessels, gliding is a luxury that costs the tab,
// and a once-a-second redraw of positions that are minutes old loses
// nothing a viewer could see.
const SMOOTH_MAX = 1200;
const STILL_MS = 1000;

// AND A FLOOR ON THE GAP EVEN WHEN IT IS CHEAP, because the first version of
// this fix budgeted per FRAME and the real cost is per SECOND.
//
// Measured in the browser on 2026-09-24: the DC sea box was rebuilding and
// re-uploading 601 vessels 123 TIMES A SECOND. The loop is driven by
// requestAnimationFrame, which runs at the DISPLAY's refresh rate, and this
// machine's display is 120 Hz. Every "sixty times a second" in these
// comments was an assumption about somebody else's monitor.
//
// So the national box crash was 6,451 vessels x 123 = about 790,000 features
// re-indexed per second, twice over -- and the DC box, nominally fine, was
// still doing 74,000. Within one order of magnitude of the thing that killed
// the tab, on the box that was working.
//
// 30 Hz is the fastest a rebuild is ever worth: MapLibre paints on vsync
// regardless, so rebuilding faster than this hands the worker frames the map
// will never draw. Nothing on screen moves more than a pixel between them.
const SMOOTH_MS = 33;
const wake = new Wake();

export const wakeStats = () => wake.stats;

// The session wake is drawn from the REPORTED positions, never the
// dead-reckoned ones. A wake made of extrapolations would be a picture of
// the browser's arithmetic rather than of anything the vessel broadcast, and
// it would keep drawing a confident line for a vessel that had stopped
// transmitting -- which is precisely the inference this project refuses.
export function setWake(map, on) {
  wakeOn = on;
  if (!on) wake.clear();
  if (map.getLayer("vessel-wake")) {
    map.setLayoutProperty("vessel-wake", "visibility",
                          on && visible ? "visible" : "none");
  }
  if (!on) map.getSource("vessel-wakes")?.setData(
    { type: "FeatureCollection", features: [] });
}

// Switch which water the vessel layer shows. Unlike the air box this does
// NOT throw away the table on the way back: the server's subscription only
// ever grows, so DC after CONUS is a filter over a socket that is already
// warm. Going the other way costs a cold start, and the panel says so.
export function setSeaAoi(map, name) {
  if (name === seaAoi) return;
  seaAoi = name;
  fleet = [];
  // The wake belongs to the box it was drawn in. Carrying Chesapeake wakes
  // onto a national view would leave lines with no vessels at the end of
  // them, which reads as contacts that vanished.
  wake.clear();
  if (map.getSource("vessels")) {
    map.getSource("vessels").setData({ type: "FeatureCollection", features: [] });
  }
  map.getSource("vessel-wakes")?.setData(
    { type: "FeatureCollection", features: [] });
  // Ask NOW rather than serving out the rest of the current sleep. Widening
  // the subscription also restarts the server's table, so the sooner the
  // first request lands the sooner the warm-up starts.
  if (seaKick) {
    clearTimeout(seaTimer);
    seaTimer = setTimeout(seaKick, 0);
  }
}

export const seaAoiOf = () => seaAoi;

export function setVisible(map, on) {
  visible = on;
  for (const id of LAYERS) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    }
  }
  // The wake is behind its own toggle as well as the domain's, so turning
  // Sea back on must not switch a layer the user had switched off.
  if (map.getLayer("vessel-wake")) {
    map.setLayoutProperty("vessel-wake", "visibility",
                          on && wakeOn ? "visible" : "none");
  }
  // The tick skips hidden layers, so coming back must redraw at once rather
  // than leave the map empty for up to a second.
  if (on) { lastDraw = 0; }
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

function drawWake(map) {
  if (!wakeOn) return;
  const shown = wanted ? fleet.filter((v) => wanted.has(v.props.group)) : fleet;
  // REPORTED positions, from the fleet itself -- not the reckoned frame.
  wake.feed(shown.map((v) => ({
    geometry: { type: "Point", coordinates: v.coords },
    properties: v.props,
  })), "mmsi");
  map.getSource("vessel-wakes")?.setData(
    wake.toGeoJSON((props) => ({ group: props.group })));
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
    r = await fetch(`${API}/live?domain=sea&aoi=${encodeURIComponent(seaAoi)}`);
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
    // Scheduled FIRST. If draw() ever throws -- a malformed feature, a
    // source removed mid-frame -- the loop must not die with it and leave
    // a map frozen at whatever was on screen, silently.
    requestAnimationFrame(tick);

    // Nothing to draw on, or nobody looking. Chrome already throttles rAF
    // in a background tab, but a hidden LAYER still ran the whole rebuild
    // sixty times a second into a source nobody could see.
    if (!visible || document.hidden || !map.getSource("vessels")) return;

    // One rule, two speeds: a small fleet glides at 30 Hz, a big one is
    // repositioned once a second. Both are a floor on the GAP, so neither
    // depends on how fast the display happens to be.
    const now = Date.now();
    const gap = fleet.length <= SMOOTH_MAX ? SMOOTH_MS : STILL_MS;
    if (now - lastDraw >= gap) {
      lastDraw = now;
      draw(map, now);
    }
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
    seaTimer = null;
    try {
      await pollVessels(onUpdate);
      drawWake(map);
      fails = 0;
    } catch (e) {
      fails += 1;
      onError?.(e);
    }
    seaTimer = setTimeout(poll, Math.min(POLL_MS * Math.max(1, fails), 120000));
  };
  seaKick = poll;
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


// --- the vessel card ------------------------------------------------------
//
// AIS carries far more than the aircraft feed does, and almost all of it is
// TYPED IN BY THE CREW: the name, the destination, the draught, the
// navigational status. That makes the card richer and the caveat sharper --
// an aircraft's callsign comes from a flight plan, but a vessel's
// destination is whatever somebody entered at the last port, and it is
// routinely stale, blank, or a joke. The card shows it and says so.

// Status 1 and 5 mean the vessel is DECLARING it is not under command or
// moored. A vessel showing way while declaring "at anchor" is a discrepancy
// inside the cooperative record -- the aviation half of this project looks
// for exactly that kind of internal contradiction -- so the card flags it
// rather than printing two fields and hoping someone compares them.
const NAV_STATUS = {
  0: "under way using engine", 1: "at anchor", 2: "not under command",
  3: "restricted manoeuvrability", 4: "constrained by draught",
  5: "moored", 6: "aground", 7: "engaged in fishing",
  8: "under way sailing", 9: "reserved (HSC)", 10: "reserved (WIG)",
  11: "power-driven vessel towing astern", 12: "power-driven pushing ahead",
  13: "reserved", 14: "AIS-SART / MOB / EPIRB",
  15: "undefined",
};
const STATIONARY = new Set([1, 5, 6]);

export function vesselCard(p, { others = 0, pinnable = false } = {}) {
  const kn = p.sog_kn != null ? Number(p.sog_kn) : null;
  const { kind } = mmsiKind(p.mmsi);
  const flag = flagOf(p.mmsi);
  const status = p.nav_status != null ? NAV_STATUS[Number(p.nav_status)] : null;
  const contradiction = status && kn != null
    && STATIONARY.has(Number(p.nav_status)) && kn > 1.0;

  // Non-breaking inside each measurement: "14.5 m" and "draught" may wrap
  // apart, but "14.5" and "m" may not.
  const size = [
    p.length_m ? `${Math.round(p.length_m)}&nbsp;m` : null,
    p.width_m ? `${Math.round(p.width_m)}&nbsp;m beam` : null,
    p.draught_m ? `${Number(p.draught_m).toFixed(1)}&nbsp;m draught` : null,
  ].filter(Boolean).join(" &middot; ");

  const type = [
    GROUP_LABEL[p.group],
    (p.group === "commercial" || p.group === "other")
      ? (SUBTYPE_LABEL[p.subtype] ?? p.subtype) : null,
  ].filter(Boolean).join(" &middot; ");

  // Speed and course are one fact. A stopped vessel gets no course, because
  // a heading on something making 0.2 kn is anchor swing dressed as intent.
  const vel = kn == null ? null
    : kn <= 0.5 ? `${kn.toFixed(1)}&nbsp;kn <span class="dim">(stopped)</span>`
    : [`${kn.toFixed(1)}&nbsp;kn`, compass(p.heading)].filter(Boolean).join(" &middot; ");

  return card({
    title: p.name || p.label || p.mmsi,
    id: p.mmsi,
    // The colour of the dot that was hovered, carried onto the card, so the
    // two are visibly the same contact.
    chip: GROUPS.find((g) => g.key === p.group)?.color,
    // Identity and its provenance on one line: the number, the call sign it
    // broadcast, and the administration that issued the number. None of the
    // three earns a row.
    sub: [esc(p.mmsi), p.call_sign ? esc(p.call_sign) : null,
          flag ? `${esc(flag)} (MID ${String(p.mmsi).slice(0, 3)})` : null]
      .filter(Boolean).join(" &middot; "),
    age: ageShort(p.age_s),
    // Class A reports every few seconds under way and every 3 min at
    // anchor, so three minutes is normal and ten is a question.
    ageState: freshness(p.age_s, 180, 600),
    rows: [
      // Only when it is NOT an ordinary ship. A row saying "ship" on every
      // ship is noise; a row saying "navigation aid" stops a buoy being
      // read as a vessel that went quiet.
      ["Station", kind === "ship" ? null : kind, { strong: true }],
      ["Type", type],
      ["Size", size || "dimensions undeclared"],
      ["Status", status, { warn: contradiction }],
      ["Velocity", vel],
      ["Bound for", p.destination],
    ],
    foot: [
      crowd(others),
      contradiction
        ? `<b>Declared "${status}" while making ${kn.toFixed(1)} kn.</b>
           A contradiction inside its own report \u2014 usually a status the
           crew forgot to change, which is why a status field cannot be
           trusted on its own.`
        : null,
      p.destination
        ? `Destination is free text typed by the crew, not a filed plan \u2014
           often stale, sometimes fictional.`
        : null,
      p.ais_class
        ? `AIS class ${p.ais_class}${p.ais_class === "B"
            ? " \u2014 lower power and less often, so gaps are normal"
            : ""}. ${p.n_positions ?? 0} position report${
            Number(p.n_positions) === 1 ? "" : "s"} this session.`
        : null,
    ],
    evidence: `<b>Cooperative.</b> Every field above was broadcast by the
      vessel itself. Sentinel-1 observes this water without consent, but
      days later \u2014 nothing here has been checked against it.`,
    hint: pinHint(pinnable),
  });
}
