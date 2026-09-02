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
const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
export const POLL_MS = 8000;       // upstream cache is 6s; see live.py on quota
const R = 6371008.8;

let fleet = [];             // last known state per aircraft
let lastPoll = 0;
let running = false;
let requests = 0;   // upstream cost is invisible otherwise

// --- dead reckoning -------------------------------------------------------

function project(lat, lon, bearingDeg, distM) {
  const d = distM / R;
  const b = (bearingDeg * Math.PI) / 180;
  const p1 = (lat * Math.PI) / 180;
  const l1 = (lon * Math.PI) / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1),
                             Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [((l2 * 180) / Math.PI + 540) % 360 - 180, (p2 * 180) / Math.PI];
}

function reckon(now) {
  return {
    type: "FeatureCollection",
    features: fleet.map((a) => {
      let coords = a.coords;
      // Only extrapolate when we actually know how it is moving, and stop
      // after 120s -- past that the guess is worse than admitting we do not
      // know, and a stale aircraft should not keep flying across the map.
      const age = (now - a.t) / 1000;
      if (a.speed_mps && a.heading != null && age > 0 && age < 120) {
        coords = project(a.coords[1], a.coords[0], a.heading, a.speed_mps * age);
      }
      return {
        type: "Feature",
        geometry: { type: "Point", coordinates: coords },
        properties: { ...a.props, stale: age > 120 },
      };
    }),
  };
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

  const poll = async () => {
    try { await pollLive(map, onUpdate); }
    catch (e) { onError?.(e); }
    setTimeout(poll, POLL_MS);
  };
  poll();
}

export const liveInfo = () => ({ n: fleet.length, lastPoll, requests });
