// Track layer.
//
// Fetches /tracks and draws them. Colour encodes altitude, because that is the
// one dimension a flat map cannot show and it makes approach and departure
// corridors legible at a glance -- low traffic stacks over the airports, high
// traffic runs straight across.

// Same origin when uvicorn serves this page (port 8000), absolute when
// something else does -- so the old two-server setup keeps working.
import { attachHover, card, crowd, pinHint } from "./popup.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";

export async function fetchTracks({ hours = 2, domain = "air", aoi = "air",
                                    limit = 500 } = {}) {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 3600 * 1000);
  const qs = new URLSearchParams({
    start: start.toISOString(),
    end: end.toISOString(),
    domain,
    // Which archive. The two air collectors write separate ones at
    // completely different cadences; see the route.
    aoi,
    limit: String(limit),
  });
  const r = await fetch(`${API}/tracks?${qs}`);
  if (!r.ok) throw new Error(`/tracks ${r.status}`);
  return r.json();
}

export function addTracks(map, data) {
  if (map.getSource("tracks")) {
    map.getSource("tracks").setData(data);
    return;
  }

  map.addSource("tracks", { type: "geojson", data });

  // A pale casing under every trail. Thin coloured lines vanish against a
  // light basemap -- the casing gives them an edge to read against, which is
  // the difference between visible and technically-present.
  map.addLayer({
    id: "tracks-casing",
    type: "line",
    source: "tracks",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 3.0, 12, 6.0],
      "line-color": "#ffffff",
      "line-opacity": 0.55,
    },
  });

  map.addLayer({
    id: "tracks-line",
    type: "line",
    source: "tracks",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.4, 12, 3.2],
      "line-opacity": 0.9,
      "line-color": [
        "interpolate", ["linear"], ["coalesce", ["get", "max_alt_m"], 0],
        0,     "#a8175c",   // on the deck: chart magenta
        1500,  "#c2703f",
        4000,  "#9a8f2a",
        8000,  "#3d8f6a",
        12000, "#10635f",   // cruise: teal
      ],
    },
  });

  // Wider transparent line so hovering does not demand pixel precision.
  map.addLayer({
    id: "tracks-hit",
    type: "line",
    source: "tracks",
    paint: { "line-width": 12, "line-opacity": 0 },
  });

  attachHover(map, {
    layers: ["tracks-hit"],
    key: (p) => p.platform_id ?? null,
    cursor: "crosshair",
    html: trackCard,
  });
}

// Straightness is net displacement over path length. It is here in the popup
// deliberately: a value near 1.0 is a transit, and a value near 0 is something
// that went a long way without getting anywhere. That second case is what
// core/detectors/orbits.py will look for in Phase 2, so eyeballing it now
// tells you whether the signal is even present in your box.

// --- the archive track card -----------------------------------------------
//
// Not a position: a whole recorded path, which is a different kind of object
// and should not be hovered in the same words as a live contact. The card
// says so in its subline rather than leaving the reader to notice that this
// one has no age on it.

// STRAIGHTNESS IS THE ONE NUMBER HERE THAT IS AN INFERENCE, so it is the one
// that gets translated. Net displacement over path length: near 1.0 is a
// transit, near 0 is something that went a long way without getting
// anywhere. The second case is what core/detectors/orbits.py will look for,
// and reading it off a hover card tells you whether the signal is even
// present in your box before a line of that detector is written.
function shape(r) {
  if (r >= 0.9) return "a straight transit";
  if (r >= 0.6) return "a transit with manoeuvring";
  if (r >= 0.3) return "wandering \u2014 went far, got nowhere";
  return "orbiting or loitering";
}

export function trackCard(p, { others = 0, pinnable = false } = {}) {
  const mins = p.duration_s != null ? Math.round(p.duration_s / 60) : null;
  const r = p.path_km > 0 ? p.net_km / p.path_km : 1;

  return card({
    title: p.platform_id ?? "track",
    sub: "Recorded path \u2014 archive, not a live position",
    foot: [crowd(others)],
    rows: [
      ["Duration", mins != null
        ? `${mins}&nbsp;min <span class="dim">(${p.n_reports} reports)</span>` : null],
      ["Distance", p.path_km != null
        ? `${p.path_km}&nbsp;km flown <span class="dim">(${p.net_km}&nbsp;km net)</span>`
        : null],
      ["Straightness", p.path_km > 0
        ? `${r.toFixed(2)} <span class="dim">&middot; ${shape(r)}</span>` : null],
    ],
    evidence: `<b>Cooperative.</b> Every fix in this path was broadcast by
      the platform itself, and the gaps between them are gaps in what it
      chose to send \u2014 not in where it went.`,
    hint: pinHint(pinnable),
  });
}
