// The observed layer: radar detections no AIS report explained.
//
// THIS LAYER IS NOT LIVE AND MUST NOT LOOK LIVE.
//
// Everything else on this map is a position something broadcast in the last
// few seconds. These are unexplained Sentinel-1 returns from passes hours to
// days old, twelve days apart. Drawn as another dot among the vessels, they
// would read as "ships the AIS feed missed, right now" -- which is three
// wrong claims in one glance: not ships, not now, not missed.
//
// So the encoding is deliberately foreign to the live layers:
//
//   hollow ring + cross   nothing else on the map is hollow or crossed. A
//                         filled shape says "a thing is here"; a ring says
//                         "a return was here, at a time, and was not
//                         explained".
//   one desaturated hue   the live layers own colour. This layer gets a
//                         single slate tone so it never competes with the
//                         vessel groups or the altitude ramp.
//   its own timestamp     every candidate carries the date of the pass that
//                         saw it, printed on hover and summarised in the
//                         panel. "As of" is per feature, not per map.
//
// Fixed sites are drawn too, in a fainter grey, because they were REMOVED
// from the candidate list. A removal you cannot see is a removal you have to
// take on trust; a wind farm visible as furniture is a removal the reader can
// check.

import { attachHover, card, crowd, esc, pinHint } from "./popup.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";

const CAND = "#2f4858";          // slate. Not a vessel group, not the ramp.
const SITE = "#9aa7ad";

const EMPTY = { type: "FeatureCollection", features: [] };

let meta = null;                 // properties of the last candidates payload
let siteMeta = null;
let error = null;
let loaded = false;

export function discrepancyInfo() {
  return { meta, siteMeta, error, loaded };
}

// --- fetch ----------------------------------------------------------------

async function get(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) {
    let detail = null;
    try { detail = (await r.json()).detail; } catch { /* not JSON */ }
    const e = new Error(`${path} ${r.status}`);
    e.status = r.status;
    e.detail = detail;
    throw e;
  }
  return r.json();
}

// --- layers ---------------------------------------------------------------

// A 13x13 RGBA plus sign, built by hand. No canvas, no font, no network.
function crossIcon(size = 13, arm = 5, thick = 1) {
  const data = new Uint8Array(size * size * 4);
  const c = (size - 1) / 2;
  const [r, g, b] = [0x2f, 0x48, 0x58];          // CAND
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const onH = Math.abs(y - c) <= thick - 1 && Math.abs(x - c) <= arm;
      const onV = Math.abs(x - c) <= thick - 1 && Math.abs(y - c) <= arm;
      if (!onH && !onV) continue;
      const i = 4 * (y * size + x);
      data[i] = r; data[i + 1] = g; data[i + 2] = b; data[i + 3] = 255;
    }
  }
  return { width: size, height: size, data };
}

export function addDiscrepancy(map) {
  if (map.getSource("candidates")) return;

  map.addSource("sites", { type: "geojson", data: EMPTY });
  map.addSource("candidates", { type: "geojson", data: EMPTY });

  // Fixed structures first, so an unexplained return always draws above the
  // furniture it was separated from.
  map.addLayer({
    id: "sites-dot",
    type: "circle",
    source: "sites",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 2.2, 12, 4.5],
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-color": SITE,
      "circle-stroke-width": 1.2,
      "circle-opacity": 0,
      "circle-stroke-opacity": 0.75,
    },
  });

  // The ring.
  map.addLayer({
    id: "cand-ring",
    type: "circle",
    source: "candidates",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 3.4, 12, 8],
      "circle-color": "rgba(0,0,0,0)",
      "circle-opacity": 0,
      "circle-stroke-color": CAND,
      "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 5, 1.4, 12, 2.2],
      // Older passes fade. The layer is retrospective, and how retrospective
      // varies by twelve days across it.
      "circle-stroke-opacity": [
        "interpolate", ["linear"], ["coalesce", ["get", "age_days"], 0],
        0, 0.95, 60, 0.75, 240, 0.45,
      ],
    },
  });

  // The cross inside the ring. Drawn as a generated image rather than a text
  // glyph: a "+" in a text layer needs the basemap style to serve a font, and
  // when it does not, the layer fails silently and the ring loses the mark
  // that distinguishes a candidate from a fixed structure. An image has no
  // such dependency.
  if (!map.hasImage("cand-cross-icon")) map.addImage("cand-cross-icon", crossIcon());
  map.addLayer({
    id: "cand-cross",
    type: "symbol",
    source: "candidates",
    layout: {
      "icon-image": "cand-cross-icon",
      "icon-size": ["interpolate", ["linear"], ["zoom"], 5, 0.5, 12, 1.0],
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
    paint: { "icon-opacity": 0.85 },
  });

  // One interaction for the whole map: bigger-than-the-symbol hit targets,
  // the card follows the contact under the cursor rather than the first one
  // entered, and click keeps it open. See attachHover in popup.js.
  attachHover(map, {
    layers: ["cand-ring", "cand-cross", "sites-dot"],
    key: (p) => p.id ?? `${p.date ?? ""}:${p.lon ?? ""}:${p.lat ?? ""}`,
    cursor: "help",
    html: (p, ctx) =>
      (ctx.layer === "sites-dot" ? siteCard : candidateCard)(p, ctx),
  });

  setVisible(map, false);
}

export function setVisible(map, want) {
  for (const id of ["cand-ring", "cand-cross", "sites-dot"]) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", want ? "visible" : "none");
    }
  }
}

// --- load -----------------------------------------------------------------

// `reception` defaults to "heard" on the API side: only water where AIS is
// demonstrably received supports a dark-vessel reading. Passing "all" is
// allowed and the panel then says what the extra points are.
export async function loadDiscrepancy(map, { reception = "heard" } = {}) {
  error = null;
  try {
    const cand = await get(`/events/candidates?reception=${encodeURIComponent(reception)}`);
    const now = Date.now();
    for (const f of cand.features) {
      const d = f.properties.date;
      f.properties.age_days = d
        ? (now - Date.parse(`${d}T00:00:00Z`)) / 86400000
        : null;
    }
    meta = cand.properties;
    map.getSource("candidates")?.setData(cand);
    loaded = true;
  } catch (e) {
    error = e;
    map.getSource("candidates")?.setData(EMPTY);
  }

  // Sites are context, not the product: their failure never blanks the layer.
  try {
    const s = await get("/events/sites");
    siteMeta = s.properties;
    map.getSource("sites")?.setData(s);
  } catch { /* the candidate layer stands on its own */ }

  return { meta, siteMeta, error };
}

// --- coverage at a point --------------------------------------------------
//
// The question a live map cannot answer, and the reason this view is worth
// clicking rather than only looking at: WHEN DID THE RADAR LAST LOOK HERE?
// Without it a viewer reading empty water has no way to tell "searched, and
// nothing was there" from "never searched at all", and those are opposite
// claims that look identical.

export async function coverageAt(lon, lat) {
  return get(`/coverage?lon=${lon.toFixed(5)}&lat=${lat.toFixed(5)}`);
}

export async function coverageSummary() {
  try {
    return await get("/coverage/summary");
  } catch {
    return null;
  }
}

// --- hover cards ----------------------------------------------------------
//
// THE ONLY TWO CARDS ON THIS MAP THAT ARE NOT COOPERATIVE REPORTS, and the
// reason a hover card is worth building at all.
//
// Every live card ends with "Cooperative -- the contact broadcast all of
// this". These two end with the opposite, because Sentinel-1 recorded them
// without anybody's participation or consent. That inversion is the whole
// argument of the project, and it was being carried by one italic clause at
// the end of a run of <br> tags, in a popup that looked like a debug print,
// while the cooperative cards it is meant to be contrasted against got the
// designed treatment. A reader comparing the two was shown the argument in
// the wrong direction.

// How strong the return was, in the terms the detector works in: SNR is what
// decides whether it survived thresholding, pixels is roughly how big it is.
function strength(p) {
  const snr = p.snr != null ? `SNR ${Number(p.snr).toFixed(0)}` : null;
  const px = p.pixels != null ? `${p.pixels}&nbsp;px` : null;
  return [snr, px].filter(Boolean).join(" &middot; ") || null;
}

// THE LOAD-BEARING QUALIFIER, and the one most likely to be skipped.
//
// A radar return with no AIS beside it means nothing in water where AIS is
// not received: the silence there is ours, not the vessel's. Only "heard"
// water supports a dark-vessel reading, and the card now says which kind
// this is in words rather than printing a class name and leaving the reader
// to assume the favourable one.
const RECEPTION = {
  heard: ["AIS is received here", false],
  intermittent: ["patchy here \u2014 a weaker claim", true],
  thin: ["thin here \u2014 not a dark-vessel claim", true],
  unheard: ["none received here \u2014 the silence is OURS", true],
};

export function candidateCard(p, { others = 0, pinnable = false } = {}) {
  const rec = RECEPTION[String(p.reception || "").toLowerCase()];

  return card({
    title: "UNEXPLAINED RETURN",
    chip: CAND,
    // The age in days, with no freshness band. A pill reading "stale" would
    // be nonsense on a source that is retrospective by design -- Sentinel-1
    // passes when it passes, and days old is the normal condition here, not
    // a fault to flag.
    age: p.age_days != null ? `${Math.round(p.age_days)}d` : null,
    sub: p.date ? `Sentinel-1 pass &middot; ${esc(p.date)}` : "Sentinel-1 pass",
    rows: [
      ["Strength", strength(p)],
      ["AIS here", rec ? rec[0] : (p.reception ? esc(p.reception) : "not measured"),
        { warn: rec ? rec[1] : true }],
    ],
    foot: [
      crowd(others),
      !rec
        ? `<b>Reception here was not measured.</b> Without it, this is a
           return with no AIS beside it \u2014 not a vessel that went dark.`
        : null,
    ],
    evidence: `<b>Observed, not reported.</b> Sentinel-1 recorded this
      without the object's participation or consent \u2014 the inverse of
      every other contact on this map. Retrospective by days, and
      <b>not a confirmed vessel</b>: a radar return that nothing
      cooperative accounts for.`,
    hint: pinHint(pinnable),
  });
}

export function siteCard(p, { pinnable = false } = {}) {
  const n = p.n_dates ?? null;
  return card({
    title: "FIXED STRUCTURE",
    chip: SITE,
    sub: "Excluded from the candidate list",
    rows: [
      ["Seen on", n != null ? `${esc(n)} separate passes` : "repeated passes"],
      ["AIS", "never matched, on any pass"],
    ],
    foot: [
      `Something returning radar from the same coordinates across months
       that never transmits is a platform, a wreck, a buoy or a turbine
       \u2014 not a vessel going dark.`,
    ],
    evidence: `<b>Excluded on purpose, and drawn anyway.</b> An exclusion
      you cannot see is one you have to take on trust.`,
    hint: pinHint(pinnable),
  });
}
