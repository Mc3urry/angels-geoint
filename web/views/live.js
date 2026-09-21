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

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
// Polling our own server is free now -- the server reads the collector's
// latest poll, which changes every 30 s -- so ten seconds is ample.
export const POLL_MS = 10000;

let fleet = [];             // last known state per aircraft
let lastPoll = 0;
let running = false;
let requests = 0;   // upstream cost is invisible otherwise
let visible = true;

const reckon = (now) => reckonFleet(fleet, now, "air");

// Hiding the layer STOPS THE POLL.
//
// This comment used to say the opposite -- that polling while hidden "costs
// nothing extra" because the cache is server-side. It cost exactly as much as
// polling while visible, from the same daily budget the archive depends on,
// and a tab left open overnight with aircraft hidden while the sea warmed up
// spent the day's credits and cut the collectors off for almost nine hours.
export function setVisible(map, on) {
  visible = on;
  for (const id of ["live-halo", "live-plane", "live-label"]) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    }
  }
}

// --- the plane symbol -----------------------------------------------------

function planeImage(map) {
  if (map.hasImage("plane")) return;
  const s = 48, c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d");
  g.translate(s / 2, s / 2);
  g.fillStyle = "#ffffff";
  g.strokeStyle = "rgba(14,26,36,.85)";
  g.lineWidth = 2.5;
  g.beginPath();                 // nose up; icon-rotate turns it to heading
  g.moveTo(0, -19);
  g.lineTo(4.5, -4);
  g.lineTo(20, 7);
  g.lineTo(20, 11);
  g.lineTo(4.5, 7);
  g.lineTo(3.5, 15);
  g.lineTo(9, 19.5);
  g.lineTo(9, 22);
  g.lineTo(0, 19);
  g.lineTo(-9, 22);
  g.lineTo(-9, 19.5);
  g.lineTo(-3.5, 15);
  g.lineTo(-4.5, 7);
  g.lineTo(-20, 11);
  g.lineTo(-20, 7);
  g.lineTo(-4.5, -4);
  g.closePath();
  g.stroke();
  g.fill();
  // Pass the ImageData object itself. The {width,height,data} form wants a
  // Uint8Array and silently misbehaves with the Uint8ClampedArray a canvas
  // hands you. pixelRatio 2 keeps it crisp on a HiDPI screen.
  map.addImage("plane", g.getImageData(0, 0, s, s), { pixelRatio: 2 });
}

// Altitude ramp, shared with the track lines so the two read as one system.
const ALT_COLOR = [
  "interpolate", ["linear"], ["coalesce", ["get", "alt_m"], 0],
  0, "#a8175c", 1500, "#c2703f", 4000, "#9a8f2a", 8000, "#3d8f6a", 12000, "#10635f",
];

export function addLive(map) {
  planeImage(map);
  map.addSource("live", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

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

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "track-popup" });
  map.on("mouseenter", "live-plane", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = e.features[0].properties;
    const kt = p.speed_mps ? Math.round(p.speed_mps * 1.94384) : "?";
    const ft = p.alt_m != null ? Math.round(p.alt_m * 3.28084 / 100) * 100 : "?";
    const vs = p.vertical_rate;
    const trend = vs > 1 ? "climbing" : vs < -1 ? "descending" : "level";
    popup.setLngLat(e.lngLat).setHTML(
      `<strong>${p.callsign}</strong>  <span style="opacity:.6">${p.icao24}</span><br>` +
      `${ft} ft &middot; ${trend}<br>${kt} kt &middot; hdg ${Math.round(p.heading ?? 0)}&deg;` +
      (p.squawk ? `<br>squawk ${p.squawk}` : "")
    ).addTo(map);
  });
  map.on("mouseleave", "live-plane", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });
}

export async function pollLive(map, onUpdate) {
  let r;
  try {
    r = await fetch(`${API}/live`);
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

  fleet = data.features.map((f) => ({
    coords: f.geometry.coordinates,
    t: f.properties.t * 1000,
    speed_mps: f.properties.speed_mps,
    heading: f.properties.heading,
    props: f.properties,
  }));
  lastPoll = Date.now();
  requests += 1;
  onUpdate?.(data);
  return data;
}

export function startLive(map, onUpdate, onError) {
  if (running) return;
  running = true;

  const tick = () => {
    if (map.getSource("live")) map.getSource("live").setData(reckon(Date.now()));
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  let backoff = 0;
  const poll = async () => {
    if (!visible || document.hidden) {
      // Checked every second so switching Air back on, or returning to the
      // browser tab, brings the aircraft back promptly.
      setTimeout(poll, 1000);
      return;
    }
    try {
      await pollLive(map, onUpdate);
      backoff = 0;
    } catch (e) {
      // After a 429 the server will not ask OpenSky again until the stated
      // retry time, so hammering it gains nothing; slow right down.
      backoff = e.status === 429 ? 60000 : Math.min(60000, (backoff || POLL_MS) * 2);
      onError?.(e);
    }
    setTimeout(poll, Math.max(POLL_MS, backoff));
  };
  poll();
}

export const liveInfo = () => ({ n: fleet.length, lastPoll, requests, visible });
