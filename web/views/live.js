// Live aircraft layer.
//
// Two things happen here, and the second is what makes the map feel alive:
//
//   1. Poll /live every POLL_MS for real positions.
//   2. Between polls, DEAD RECKON each aircraft forward from its last known
//      position using heading and speed, redrawing every animation frame.
//
// That second step is the project's own namesake technique pointed at its own
// map -- estimating where something must be from its last fix plus motion,
// with no external reference. It is also the difference between icons that
// teleport every fifteen seconds and icons that fly.

// Same origin when uvicorn serves this page (port 8000), absolute when
// something else does -- so the old two-server setup keeps working.
import { reckonFleet } from "./reckon.js";
import { attachHover, card, compass, age, ageShort, crowd, esc, freshness, km,
         pinHint, squawk } from "./popup.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
// Polling our own server is free now -- the server reads the collector's
// latest poll, which changes every 30 s -- so ten seconds is ample.
export const POLL_MS = 10000;
// The national snapshot is rewritten every 10 min; a minute is ample.
export const NATIONAL_POLL_MS = 60000;
// How often the uncertainty ring is resized. See the tick for why this is
// not every frame.
const RING_MS = 1000;

let fleet = [];             // last known state per aircraft
let lastPoll = 0;
let running = false;
let requests = 0;   // upstream cost is invisible otherwise
let visible = true;
let aoi = "air";            // "air" (DC-Baltimore, 30 s) or "conus" (10 min)
let meta = null;            // properties of the last /live payload
let pollTimer = null;       // the scheduled next poll, so a switch can jump it
let lastRing = 0;           // when the uncertainty ring was last resized
let lastDraw = 0;           // when the fleet was last re-projected
let kick = null;            // poll(), hoisted out of startLive by setAoi

// Metres per pixel at zoom 0 on the equator, for 512 px tiles:
// 40,075,016.686 m / 512 px. Used to draw a circle whose radius is a real
// distance rather than a fixed number of pixels.
const M_PER_PX_Z0 = 78271.517;

// Is the server letting us project positions forward? It says so per box.
const projecting = () => meta?.dead_reckon !== false;

const reckon = (now) => reckonFleet(fleet, now, "air", {
  project: projecting(),
  // At 10-minute polls the 120 s air horizon would mark every aircraft in
  // the country stale and dim the whole map. The horizon is the oldest fix
  // the server will serve at all.
  horizon: projecting() ? undefined : (meta?.max_fix_age_s ?? 1800),
});

export const liveMeta = () => meta;
export const liveAoi = () => aoi;

// Hiding the layer STOPS THE POLL.
//
// This comment used to say the opposite -- that polling while hidden "costs
// nothing extra" because the cache is server-side. It cost exactly as much as
// polling while visible, from the same daily budget the archive depends on,
// and a tab left open overnight with aircraft hidden while the sea warmed up
// spent the day's credits and cut the collectors off for almost nine hours.
export function setVisible(map, on) {
  visible = on;
  for (const id of ["live-ring", "live-halo", "live-plane", "live-label", "live-hit"]) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    }
  }
  // Switching the layer back on must not switch the ring on over a box that
  // has no use for it, nor bring back the plane symbol over one that does.
  applyMode(map);
  // The frame loop skips a hidden layer entirely, so coming back has to be
  // allowed to draw at once rather than wait out the throttle.
  if (on) { lastDraw = 0; }
}

// --- the plane symbol -----------------------------------------------------

// The silhouette. Swept wings and a tapered nose rather than the straight
// dart it used to be: at the sizes this draws, a symbol is read by its
// OUTLINE, and a swept planform reads as an aircraft down to about eight
// pixels where a symmetrical arrow reads as an arrow. Drawn nose-up;
// icon-rotate turns it to the heading.
function planeImage(map) {
  if (map.hasImage("plane")) return;
  const s = 64, c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d");
  g.translate(s / 2, s / 2);
  g.scale(1.18, 1.18);
  g.fillStyle = "#ffffff";
  g.strokeStyle = "rgba(14,26,36,.9)";
  g.lineWidth = 2.2;
  g.lineJoin = "round";
  g.beginPath();
  g.moveTo(0, -21);              // nose
  g.quadraticCurveTo(3.4, -16, 3.8, -6);
  g.lineTo(20, 8);               // leading edge, swept back
  g.lineTo(20, 12);
  g.lineTo(3.8, 6.5);
  g.lineTo(3.2, 15);
  g.lineTo(8.5, 19.5);           // tailplane
  g.lineTo(8.5, 22);
  g.lineTo(0, 19.5);
  g.lineTo(-8.5, 22);
  g.lineTo(-8.5, 19.5);
  g.lineTo(-3.2, 15);
  g.lineTo(-3.8, 6.5);
  g.lineTo(-20, 12);
  g.lineTo(-20, 8);
  g.lineTo(-3.8, -6);
  g.quadraticCurveTo(-3.4, -16, 0, -21);
  g.closePath();
  g.stroke();
  g.fill();
  // Pass the ImageData object itself. The {width,height,data} form wants a
  // Uint8Array and silently misbehaves with the Uint8ClampedArray a canvas
  // hands you. pixelRatio 2 keeps it crisp on a HiDPI screen.
  map.addImage("plane", g.getImageData(0, 0, s, s), { pixelRatio: 2.6 });
}

// Altitude ramp, shared with the track lines so the two read as one system.
const ALT_COLOR = [
  "interpolate", ["linear"], ["coalesce", ["get", "alt_m"], 0],
  0, "#a8175c", 1500, "#c2703f", 4000, "#9a8f2a", 8000, "#3d8f6a", 12000, "#10635f",
];

// THE UNCERTAINTY RING, in real metres.
//
// MapLibre sizes a circle in SCREEN PIXELS, so a fixed radius would mean
// 50 km at one zoom and 500 at another -- useless for a quantity whose whole
// point is being a distance. The conversion is
//
//     radius_px = radius_m * 2^zoom / (78271.517 * cos(latitude))
//
// and `["zoom"]` may only appear as the input to a top-level interpolate, so
// it is expressed as an exponential-base-2 interpolation between zoom 0 and
// zoom 22 with the endpoints scaled by 2^0 and 2^22. That is exact at every
// zoom in between, which is the trick.
//
// Two values are baked into each feature at poll time so this expression has
// only arithmetic to do:
//
//     k     speed_mps / (78271.517 * cos lat)   -- constant per aircraft
//     t_s   last_contact, unix seconds
//
// and `now` is injected as a literal, so the ring inflates by updating ONE
// paint property per second rather than rewriting seven thousand features.
export function ringRadius(nowS, maxAgeS = 1800) {
  const age = ["min", maxAgeS, ["max", 0, ["-", nowS, ["get", "t_s"]]]];
  const at = (scale) => ["*", ["get", "k"], age, scale];
  return ["interpolate", ["exponential", 2], ["zoom"],
          0, at(1), 22, at(4194304)];
}

export function addLive(map) {
  planeImage(map);
  map.addSource("live", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

  // Drawn FIRST, so it sits under the aircraft it belongs to. Hidden until a
  // box that cannot be dead-reckoned is selected.
  map.addLayer({
    id: "live-ring",
    type: "circle",
    source: "live",
    paint: {
      "circle-radius": ringRadius(Date.now() / 1000),
      "circle-color": "rgba(0,0,0,0)",
      "circle-opacity": 0,
      // ONE NEUTRAL COLOUR, not the altitude ramp. The first version stroked
      // these in ALT_COLOR and they read as data -- a second, brighter
      // rendering of the same aircraft -- when what they represent is the
      // absence of data. Uncertainty has no altitude.
      "circle-stroke-color": "#7d8f9b",
      "circle-stroke-width": 0.8,
      // Seven thousand rings of 140 km at national zoom is a net that hides
      // the aircraft it belongs to. Measured against the real feed at four
      // zoom levels: barely a haze wide out, legible once the count on
      // screen is small. The panel's key disc carries the same number
      // legibly at every zoom, so this layer does not have to shout.
      "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"],
                                3, 0.05, 6, 0.12, 9, 0.3, 11, 0.5],
    },
  });

  map.addLayer({
    id: "live-halo",
    type: "circle",
    source: "live",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 7, 9, 12, 16],
      "circle-color": ALT_COLOR,
      "circle-opacity": ["case", ["get", "stale"], 0.15, 0.4],
    },
  });

  map.addLayer({
    id: "live-plane",
    type: "symbol",
    source: "live",
    layout: {
      "icon-image": "plane",
      "icon-size": ["interpolate", ["linear"], ["zoom"], 7, 0.5, 12, 0.95],
      "icon-rotate": ["coalesce", ["get", "heading"], 0],
      "icon-rotation-alignment": "map",
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
    paint: {
      "icon-opacity": ["case", ["get", "stale"], 0.4, 1],
    },
  });

  // Labels live on their OWN layer, deliberately. A fontstack the style does
  // not ship makes MapLibre drop the entire layer it is declared on -- put the
  // text next to the icon and a missing font takes the aircraft down with it.
  // Adding it in a try/catch means the worst case is unlabelled planes.
  try {
    map.addLayer({
      id: "live-label",
      type: "symbol",
      source: "live",
      minzoom: 8.5,
      layout: {
        "text-field": ["get", "callsign"],
        "text-size": 11,
        "text-offset": [0, 1.6],
        "text-anchor": "top",
        "text-allow-overlap": false,
        "text-optional": true,
      },
      paint: {
        "text-color": "#0e1a24",
        "text-halo-color": "rgba(255,255,255,.95)",
        "text-halo-width": 1.6,
        "text-opacity": ["case", ["get", "stale"], 0.4, 1],
      },
    });
  } catch (e) {
    console.warn("[angels] labels unavailable (font missing in style):", e.message);
  }

  // AN INVISIBLE TARGET BIGGER THAN THE SYMBOL.
  //
  // At national zoom an aircraft is a dot a few pixels across, and hovering
  // one was a game of skill rather than an interaction. This circle is
  // painted at zero opacity and exists only to be hit -- the same trick
  // map.js has used for the archive trails since they were built, which the
  // live layers never got. Added BEFORE the symbols so it never draws over
  // them even by accident.
  map.addLayer({
    id: "live-hit", type: "circle", source: "live",
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
  }, "live-halo");

  attachHover(map, {
    // The hit circle first: queryRenderedFeatures returns topmost-first and
    // the card should describe whatever is nearest the cursor, not whichever
    // layer happens to sit highest in the style.
    layers: ["live-hit", "live-plane", "live-halo"],
    key: (p) => p.icao24 ?? p.callsign ?? null,
    html: aircraftCard,
  });
}

// --- the aircraft card ----------------------------------------------------

// The ramp stops the live-halo layer paints with, read in JS so the hover
// card cannot drift away from the map. Nearest stop rather than an
// interpolation: the card is showing WHICH BAND, not a precise colour.
const ALT_STOPS = [[0, "#a8175c"], [1500, "#c2703f"], [4000, "#9a8f2a"],
                   [8000, "#3d8f6a"], [12000, "#10635f"]];

function rampColour(m) {
  if (m == null || Number.isNaN(Number(m))) return "#6b7c89";
  let hit = ALT_STOPS[0][1];
  for (const [alt, col] of ALT_STOPS) if (Number(m) >= alt) hit = col;
  return hit;
}

export function aircraftCard(p, { others = 0, pinnable = false } = {}) {
  const kt = p.speed_mps != null ? Math.round(p.speed_mps * 1.94384) : null;
  const ft = p.alt_m != null ? Math.round(p.alt_m * 3.28084 / 100) * 100 : null;
  const fpm = p.vertical_rate != null
    ? Math.round(p.vertical_rate * 196.85 / 50) * 50 : null;
  // Above the transition altitude an altitude is a FLIGHT LEVEL, which is
  // what the crew and the controller are both saying. Showing 35,000 ft for
  // FL350 is not wrong, but it is not what anyone involved would call it.
  const level = ft != null && ft >= 18000 ? `FL${String(ft / 100).padStart(3, "0")}`
              : ft != null ? `${ft.toLocaleString()}&nbsp;ft` : null;
  const trend = p.on_ground ? "on the ground"
    : fpm == null || Math.abs(fpm) < 100 ? "level"
    : fpm > 0 ? `climbing ${fpm.toLocaleString()}&nbsp;ft/min`
              : `descending ${Math.abs(fpm).toLocaleString()}&nbsp;ft/min`;

  const sq = squawk(p.squawk);
  const fixAge = p.age_s != null ? Number(p.age_s) : null;
  // What the position is WORTH. Either it was projected forward from the
  // last fix, or it is the last fix with a radius of ignorance around it,
  // and the card says which rather than showing a dot either way.
  const unc = Number(p.uncertainty_m || 0);

  // SPEED AND TRACK ARE ONE FACT, so they are one row. Two rows for a
  // velocity is two chances to read half of it.
  const vel = [
    kt != null ? `${kt}&nbsp;kt` : null,
    compass(p.heading),
  ].filter(Boolean).join(" &middot; ");

  // Fresh against the box's own cadence: the DC box polls every 30 s, so a
  // minute is late and five minutes is a different kind of claim.
  return card({
    title: p.callsign || p.icao24,
    id: p.icao24,
    // The colour the symbol is wearing on the map, carried onto the card,
    // so the dot and the card are visibly the same aircraft. Read off the
    // same ramp stops the layer paints with -- one table, not two.
    chip: p.on_ground ? "#6b7c89" : rampColour(p.alt_m),
    // The identity and where it is registered, on one line, because they
    // are the same kind of fact and neither earns a row of its own.
    sub: [esc(p.icao24), p.country ? esc(p.country) : null]
      .filter(Boolean).join(" &middot; "),
    age: ageShort(fixAge),
    ageState: freshness(fixAge, 60, 300),
    rows: [
      ["Altitude", level ? `${level} &middot; ${trend}` : trend],
      ["Velocity", vel || null],
      ["Squawk", sq ? (sq.meaning
          ? `${sq.code} &middot; ${sq.meaning}`
          : sq.code) : null,
        { alarm: !!(sq && sq.urgent) }],
      ["Within", unc > 0 ? `${km(unc)} of this point` : null,
        { strong: true }],
    ],
    foot: [
      crowd(others),
      unc > 0
        ? `Drawn where it was last heard. At ${kt ?? "?"} kt it has had
           ${age(fixAge)?.replace(" ago", "")} to move.`
        : "Position projected from the last fix along its own heading and speed.",
    ],
    evidence: `<b>Cooperative.</b> The aircraft broadcast all of this,
      including its identity and its position. Nothing here was
      independently observed, and no civilian channel could check it.`,
    hint: pinHint(pinnable),
  });
}

export async function pollLive(map, onUpdate) {
  let r;
  try {
    r = await fetch(`${API}/live?aoi=${encodeURIComponent(aoi)}`);
  } catch (e) {
    // fetch() only rejects when the request never reached a server at all.
    // Distinguishing that from an HTTP error matters: one means the API is
    // not running, the other means it is running and unhappy.
    const err = new Error("The API is not reachable.");
    err.kind = "offline";
    throw err;
  }

  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail ?? ""; } catch { /* not json */ }
    const err = new Error(detail || `HTTP ${r.status}`);
    err.kind = "http";
    err.status = r.status;
    err.detail = detail;
    throw err;
  }

  const data = await r.json();
  meta = data.properties;

  fleet = data.features.map((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const cos = Math.max(0.05, Math.cos((lat * Math.PI) / 180));
    return {
      coords: f.geometry.coordinates,
      t: f.properties.t * 1000,
      speed_mps: f.properties.speed_mps,
      heading: f.properties.heading,
      props: {
        ...f.properties,
        // Everything the ring expression needs, computed once per poll
        // rather than per frame. See ringRadius().
        t_s: f.properties.t,
        k: (f.properties.speed_mps || 0) / (M_PER_PX_Z0 * cos),
      },
    };
  });
  lastPoll = Date.now();
  requests += 1;
  onUpdate?.(data);
  return data;
}

// Switch which box the aircraft layer is showing. The fleet is dropped
// rather than kept: the two boxes are different sets of aircraft, and
// carrying the old one over would draw a hundred stale DC contacts on top of
// the nation until the first national poll landed.
// Above this many contacts, dead reckoning stops being worth a frame.
//
// The DC box holds about a hundred aircraft and glides. The national box
// does not dead-reckon at all -- the server says so, because a ten-minute-old
// fix must not be animated as though it were live -- so this cap is a
// backstop rather than the usual path. It exists because the sea layer's
// identical loop, with no cap, crashed the tab: see SMOOTH_MAX in
// vessels.js. A limit that is never reached costs nothing; the one that was
// missing cost the whole page.
const SMOOTH_MAX = 1200;
const STILL_MS = 1000;
// See the note on SMOOTH_MS in vessels.js. requestAnimationFrame runs at the
// DISPLAY's refresh rate, not at 60 Hz, and the machine this was measured on
// runs at 123. A floor on the gap is the only form of this budget that does
// not silently change meaning with the monitor.
const SMOOTH_MS = 33;

export function setAoi(map, name) {
  if (name === aoi) return;
  aoi = name;
  fleet = [];
  meta = null;
  lastPoll = 0;
  if (map.getSource("live")) {
    map.getSource("live").setData({ type: "FeatureCollection", features: [] });
  }
  applyMode(map);
  // POLL NOW, do not wait out the current sleep. Without this, switching
  // boxes left the map empty for up to the previous box's interval -- ten
  // seconds coming from the DC box, a full minute going back to it -- with
  // no indication that anything was on its way.
  if (kick) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(kick, 0);
  }
}

// Which of the two truths the map is telling, applied to the layers.
function applyMode(map) {
  if (!map.getLayer("live-ring")) return;
  const rings = !projecting();
  map.setLayoutProperty("live-ring", "visibility",
                        rings && visible ? "visible" : "none");
  // Zoomed out over the whole country, a heading arrow on a fix that may be
  // ten minutes old reads as "it is going that way, NOW". The plane symbol
  // is replaced by a plain dot, which makes no claim about direction, and
  // the ring carries the uncertainty. The label goes too: seven thousand
  // callsigns is not a map.
  for (const id of ["live-plane", "live-label"]) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility",
                            visible && !rings ? "visible" : "none");
    }
  }
  map.setPaintProperty("live-halo", "circle-radius",
    rings ? ["interpolate", ["linear"], ["zoom"], 3, 2.2, 9, 7]
          : ["interpolate", ["linear"], ["zoom"], 7, 9, 12, 16]);
  map.setPaintProperty("live-halo", "circle-opacity",
    rings ? 0.85 : ["case", ["get", "stale"], 0.15, 0.4]);
}

export function startLive(map, onUpdate, onError) {
  if (running) return;
  running = true;

  const tick = () => {
    // Scheduled first, so a throw inside the frame cannot kill the loop and
    // freeze the map at its last good paint with no error anyone would see.
    requestAnimationFrame(tick);

    // A hidden layer was still rebuilding and re-uploading the fleet sixty
    // times a second into a source nobody could look at. See the note on
    // SMOOTH_MAX in vessels.js: this is the layer that cost a browser tab.
    if (!visible || document.hidden) return;

    if (map.getSource("live")) {
      if (projecting()) {
        // Sixty times a second, because the whole point is that the symbols
        // glide rather than jump -- but only while that is affordable.
        //
        // ABOVE THE CAP THIS SLOWS DOWN, IT DOES NOT STOP. Letting the cap
        // fall through to the ring branch instead would be silent death:
        // poll() deliberately skips its own setData while projecting, so
        // this loop is the ONLY thing that draws, and a cap that skipped
        // the frame would freeze the map at the last fleet it managed to
        // paint while cheerfully continuing to poll.
        const now = Date.now();
        const gap = fleet.length <= SMOOTH_MAX ? SMOOTH_MS : STILL_MS;
        if (now - lastDraw >= gap) {
          lastDraw = now;
          map.getSource("live").setData(reckon(now));
        }
      } else if (map.getLayer("live-ring")) {
        // Nothing MOVES here -- the positions are where they were heard. The
        // only thing changing is how much we do not know, so one paint
        // property carries the whole animation.
        //
        // ONCE A SECOND, NOT SIXTY TIMES. The first version did this every
        // animation frame, and setting a paint property re-validates the
        // style expression and repaints all seven thousand circles: it
        // pinned a core, and the basemap tiles and the fitBounds animation
        // were then competing with it for the main thread, which is what
        // made switching boxes feel like it had hung.
        //
        // The ring grows at the aircraft's speed -- 250 m a second, which at
        // national zoom is a third of a pixel. Redrawing it faster than this
        // cannot show anything a viewer could see.
        const now = Date.now();
        if (now - lastRing >= RING_MS) {
          lastRing = now;
          map.setPaintProperty("live-ring", "circle-radius",
            ringRadius(now / 1000, meta?.max_fix_age_s ?? 1800));
        }
      }
    }
  };
  requestAnimationFrame(tick);

  let backoff = 0;
  const poll = async () => {
    pollTimer = null;
    if (!visible || document.hidden) {
      // Checked every second so switching Air back on, or returning to the
      // browser tab, brings the aircraft back promptly.
      pollTimer = setTimeout(poll, 1000);
      return;
    }
    try {
      await pollLive(map, onUpdate);
      if (!projecting() && map.getSource("live")) {
        map.getSource("live").setData(reckon(Date.now()));
      }
      applyMode(map);
      backoff = 0;
    } catch (e) {
      // After a 429 the server will not ask OpenSky again until the stated
      // retry time, so hammering it gains nothing; slow right down.
      backoff = e.status === 429 ? 60000 : Math.min(60000, (backoff || POLL_MS) * 2);
      onError?.(e);
    }
    // The national snapshot only changes every ten minutes; asking for
    // 1.1 MB of it every ten seconds would be seven thousand features
    // re-parsed for nothing.
    const every = projecting() ? POLL_MS : NATIONAL_POLL_MS;
    pollTimer = setTimeout(poll, Math.max(every, backoff));
  };
  kick = poll;
  poll();
}

export const liveInfo = () => ({ n: fleet.length, lastPoll, requests, visible });
