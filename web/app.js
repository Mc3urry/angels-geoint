// ANGELS front end. Phase 1: a basemap over the AOI and nothing else.
//
// Deliberately no framework and no build step. At this size vanilla is fine,
// and a toolchain you have to maintain is a toolchain that breaks in March.
//
// Add deck.gl only when plain MapLibre actually stutters -- you will know.

const API = "http://127.0.0.1:8000";

// (min_lon, min_lat, max_lon, max_lat) -- must match angels/config.py AOI
export const AOI = [-77.2, 38.7, -76.0, 39.8];

export const map = new maplibregl.Map({
  container: "map",
  // OpenFreeMap needs no API key. Swap for Carto or MapTiler if you prefer.
  style: "https://tiles.openfreemap.org/styles/positron",
  bounds: AOI,
  fitBoundsOptions: { padding: 40 },
});

map.addControl(new maplibregl.NavigationControl(), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "nautical" }));

map.on("load", async () => {
  try {
    const r = await fetch(`${API}/health`);
    const h = await r.json();
    console.log("[angels] api healthy", h);
  } catch {
    console.warn("[angels] api unreachable -- start it with `make serve`");
  }

  // PHASE 1: import { addTracks } from "./views/map.js";
  // PHASE 2: import { addQueue }  from "./views/queue.js";
});
