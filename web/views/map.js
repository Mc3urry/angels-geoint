// Track layer.
//
// Fetches /tracks and draws them. Colour encodes altitude, because that is the
// one dimension a flat map cannot show and it makes approach and departure
// corridors legible at a glance -- low traffic stacks over the airports, high
// traffic runs straight across.

// Same origin when uvicorn serves this page (port 8000), absolute when
// something else does -- so the old two-server setup keeps working.
const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";

export async function fetchTracks({ hours = 2, domain = "air", limit = 500 } = {}) {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 3600 * 1000);
  const qs = new URLSearchParams({
    start: start.toISOString(),
    end: end.toISOString(),
    domain,
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

  const popup = new maplibregl.Popup({
    closeButton: false, closeOnClick: false, className: "track-popup",
  });

  map.on("mousemove", "tracks-hit", (e) => {
    map.getCanvas().style.cursor = "crosshair";
    const p = e.features[0].properties;
    const mins = Math.round(p.duration_s / 60);
    const straightness = p.path_km > 0 ? (p.net_km / p.path_km) : 1;
    popup.setLngLat(e.lngLat).setHTML(`
      <strong>${p.platform_id}</strong><br>
      ${p.n_reports} reports over ${mins} min<br>
      ${p.path_km} km flown, ${p.net_km} km net<br>
      straightness ${straightness.toFixed(2)}
    `).addTo(map);
  });

  map.on("mouseleave", "tracks-hit", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });
}

// Straightness is net displacement over path length. It is here in the popup
// deliberately: a value near 1.0 is a transit, and a value near 0 is something
// that went a long way without getting anywhere. That second case is what
// core/detectors/orbits.py will look for in Phase 2, so eyeballing it now
// tells you whether the signal is even present in your box.
