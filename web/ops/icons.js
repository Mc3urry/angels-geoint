// Symbology for the 3D ops view.
//
// ONE RULE: SHAPE SAYS WHAT IT IS, COLOUR SAYS ONE THING ABOUT IT.
//
// The 2D viewer's complaint (2026-10-28) was that symbols were small and the
// colours ambiguous: transit vehicles were coloured by a hash of the feed
// name, so two agencies could share a colour and a bus could look like a
// work zone. Here every kind of thing has its own silhouette, readable in
// grey, and colour carries a single named variable per kind:
//
//   aircraft   plane silhouette        colour = altitude (a ramp, legend)
//   vessel     hull, bow forward       colour = what kind of vessel (5 groups)
//   transit    round badge, pictogram  colour = mode (bus / rail / ferry)
//   work zone  diamond road sign       colour = one amber, always
//   camera     square badge, camera    colour = streams or not
//   ALPR       hexagon, "LPR"          colour = one magenta, always
//   satellite  body and panels         colour = which catalogue group
//
// Glyphs are drawn white with a dark outline, and the billboard's colour
// multiplies the white: the fill takes the colour, the outline stays dark,
// so a symbol keeps its edge over bright imagery and over the dark basemap.
// Each glyph is a data: URL so that Cesium's texture atlas holds one copy
// per glyph, not one per contact.

const S = 64;                         // drawn at 2x, shown at ~32 px
const cache = new Map();

function glyph(key, draw, size = S) {
  if (cache.has(key)) return cache.get(key);
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  g.lineJoin = "round";
  g.lineCap = "round";
  draw(g, size);
  const url = c.toDataURL("image/png");
  cache.set(key, url);
  canvases.set(url, c);
  return url;
}

const canvases = new Map();

// The glyph as the globe draws it in `color`: white multiplied by the
// colour, outline kept. For legends and cards, so the key is the symbol.
export function tinted(url, color, px = 22) {
  const k = `${url.length}:${url.slice(-24)}:${color}:${px}`;
  if (cache.has(k)) return cache.get(k);
  const src = canvases.get(url);
  if (!src) return url;
  const c = document.createElement("canvas");
  c.width = c.height = src.width;
  const g = c.getContext("2d");
  g.drawImage(src, 0, 0);
  g.globalCompositeOperation = "multiply";
  g.fillStyle = color; g.fillRect(0, 0, c.width, c.height);
  g.globalCompositeOperation = "destination-in";
  g.drawImage(src, 0, 0);
  const out = c.toDataURL("image/png");
  cache.set(k, out);
  return out;
}

const INK = "rgba(6,10,14,0.92)";

function fillStroke(g, path, lw = 4) {
  g.lineWidth = lw;
  g.strokeStyle = INK;
  g.stroke(path);
  g.fillStyle = "#fff";
  g.fill(path);
}

// -- aircraft ----------------------------------------------------------------

export const plane = () => glyph("plane", (g) => {
  // Top-down airliner, nose up. Swept wings and a tailplane: an arrow has
  // neither, which is what stops it reading as a pointer.
  const p = new Path2D(
    "M32 4 C35 4 36 8 36 12 L36 24 L58 36 L58 41 L36 35 L35 50 L43 56 L43 60 " +
    "L32 57 L21 60 L21 56 L29 50 L28 35 L6 41 L6 36 L28 24 L28 12 C28 8 29 4 32 4 Z");
  fillStroke(g, p, 4);
});

// On the ground: the same plane, hollow. Taxiing is not flying.
export const planeGround = () => glyph("plane-ground", (g) => {
  const p = new Path2D(
    "M32 4 C35 4 36 8 36 12 L36 24 L58 36 L58 41 L36 35 L35 50 L43 56 L43 60 " +
    "L32 57 L21 60 L21 56 L29 50 L28 35 L6 41 L6 36 L28 24 L28 12 C28 8 29 4 32 4 Z");
  g.lineWidth = 7; g.strokeStyle = INK; g.stroke(p);
  g.lineWidth = 3.5; g.strokeStyle = "#fff"; g.stroke(p);
});

// -- vessels -----------------------------------------------------------------

// The 2D viewer's hull (vessels.js HULL_PATH): fine bow, parallel sides,
// square transom. Scaled into the 64 px box.
const HULL = "M0,-21 C4.2,-14.5 7.2,-6 7.4,2 L6.6,16 L-6.6,16 L-7.4,2 C-7.2,-6 -4.2,-14.5 0,-21 Z";

export const hull = (hollow = false) => glyph(hollow ? "hull-hollow" : "hull", (g) => {
  g.translate(32, 34);
  g.scale(1.38, 1.38);
  const p = new Path2D(HULL);
  if (hollow) {
    g.lineWidth = 5.5; g.strokeStyle = INK; g.stroke(p);
    g.lineWidth = 2.6; g.strokeStyle = "#fff"; g.stroke(p);
  } else {
    fillStroke(g, p, 3.2);
    g.fillStyle = INK;                      // bridge mark: which end is aft
    g.fillRect(-3.2, 7, 6.4, 3.2);
  }
});

// A vessel making under half a knot has no meaningful heading, so it gets
// no bow: a ringed dot, like an anchorage symbol.
export const moored = (hollow = false) => glyph(hollow ? "moored-h" : "moored", (g) => {
  g.beginPath(); g.arc(32, 32, 16, 0, Math.PI * 2);
  g.lineWidth = 6; g.strokeStyle = INK; g.stroke();
  g.lineWidth = 3.5; g.strokeStyle = "#fff"; g.stroke();
  if (!hollow) {
    g.beginPath(); g.arc(32, 32, 7.5, 0, Math.PI * 2);
    g.fillStyle = "#fff"; g.fill(); g.lineWidth = 2.5; g.strokeStyle = INK; g.stroke();
  }
});

// -- transit -----------------------------------------------------------------

// Pictograms are drawn on a 64 grid and shrunk into the badge.
function inBadge(g) { g.translate(32, 34); g.scale(0.82, 0.82); g.translate(-32, -32); }

function badge(g, fill = true) {
  g.beginPath(); g.arc(32, 34, 24, 0, Math.PI * 2);
  g.lineWidth = 5; g.strokeStyle = INK; g.stroke();
  if (fill) { g.fillStyle = "#fff"; g.fill(); }
}

// A small heading tick on the badge's rim, drawn separately so the
// pictogram inside stays upright while the tick turns.
export const headingTick = () => glyph("tick", (g) => {
  const p = new Path2D("M32 1.5 L41 11.5 L23 11.5 Z");
  fillStroke(g, p, 3.5);
});

export const bus = () => glyph("bus", (g) => {
  badge(g);
  inBadge(g);
  g.fillStyle = INK;
  roundRect(g, 19, 15, 26, 30, 5); g.fill();
  g.fillStyle = "#fff";
  g.fillRect(22, 19, 20, 11);               // windscreen
  g.fillRect(22, 36, 5, 4); g.fillRect(37, 36, 5, 4);   // lamps
  g.fillStyle = INK;
  g.fillRect(21, 45, 6, 5); g.fillRect(37, 45, 6, 5);   // wheels
});

export const rail = () => glyph("rail", (g) => {
  badge(g);
  inBadge(g);
  g.fillStyle = INK;
  roundRect(g, 20, 12, 24, 32, 7); g.fill();
  g.fillStyle = "#fff";
  g.fillRect(24, 17, 16, 10);
  g.beginPath(); g.arc(26, 36, 2.6, 0, 7); g.arc(38, 36, 2.6, 0, 7); g.fill();
  g.strokeStyle = INK; g.lineWidth = 3.5;
  g.beginPath(); g.moveTo(24, 46); g.lineTo(19, 53); g.moveTo(40, 46); g.lineTo(45, 53); g.stroke();
});

export const ferry = () => glyph("ferry", (g) => {
  badge(g);
  inBadge(g);
  g.fillStyle = INK;
  g.beginPath(); g.moveTo(13, 34); g.lineTo(51, 34); g.lineTo(45, 44); g.lineTo(19, 44); g.closePath(); g.fill();
  g.fillRect(22, 24, 20, 8); g.fillRect(29, 17, 6, 7);
  g.strokeStyle = INK; g.lineWidth = 2.6;
  g.beginPath(); g.moveTo(14, 50); g.quadraticCurveTo(20, 46, 26, 50);
  g.quadraticCurveTo(32, 54, 38, 50); g.quadraticCurveTo(44, 46, 50, 50); g.stroke();
});

// A DOT truck with a plough blade: snowplows and service vehicles.
export const plow = () => glyph("plow", (g) => {
  badge(g);
  inBadge(g);
  g.fillStyle = INK;
  roundRect(g, 22, 20, 22, 20, 3); g.fill();                    // body
  g.fillRect(36, 14, 8, 8);                                      // cab
  g.beginPath(); g.moveTo(12, 42); g.lineTo(26, 36); g.lineTo(26, 46); g.closePath(); g.fill(); // blade
  g.fillStyle = "#fff"; g.fillRect(38, 16, 4, 4);
  g.fillStyle = INK;
  g.beginPath(); g.arc(28, 44, 3.4, 0, 7); g.arc(40, 44, 3.4, 0, 7); g.fill();
});

// -- road context ------------------------------------------------------------

export const workzone = () => glyph("wz", (g) => {
  const p = new Path2D("M32 4 L60 32 L32 60 L4 32 Z");
  fillStroke(g, p, 5);
  g.fillStyle = INK;
  g.fillRect(29, 17, 6, 20);
  g.beginPath(); g.arc(32, 45, 3.6, 0, 7); g.fill();
});

export const camera = () => glyph("cam", (g) => {
  g.fillStyle = "#fff"; g.strokeStyle = INK; g.lineWidth = 5;
  roundRect(g, 6, 6, 52, 52, 11); g.stroke(); g.fill();
  g.fillStyle = INK;
  roundRect(g, 13, 22, 28, 22, 4); g.fill();
  g.beginPath(); g.moveTo(41, 29); g.lineTo(52, 22); g.lineTo(52, 44); g.lineTo(41, 37); g.closePath(); g.fill();
  g.fillStyle = "#fff"; g.beginPath(); g.arc(20, 29, 3, 0, 7); g.fill();
});

export const alpr = () => glyph("alpr", (g) => {
  const p = new Path2D();
  for (let i = 0; i < 6; i += 1) {
    const a = Math.PI / 6 + (i * Math.PI) / 3;
    const x = 32 + 28 * Math.cos(a), y = 32 + 28 * Math.sin(a);
    if (i) p.lineTo(x, y); else p.moveTo(x, y);
  }
  p.closePath();
  fillStroke(g, p, 5);
  g.fillStyle = INK;
  g.font = "800 17px system-ui, sans-serif";
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText("LPR", 32, 33);
});

// Which way an ALPR faces: a wedge from the camera's spot, pointing north
// until it is turned to the mapped bearing. Drawn white, so the layer's
// colour tints it, and half-transparent, so it reads as a direction and not
// as a second symbol. The hexagon sits on top of its point.
export const facingCone = () => glyph("cone", (g) => {
  const p = new Path2D("M32 32 L20 3 Q32 -2 44 3 Z");
  g.globalAlpha = 0.55;
  g.fillStyle = "#fff"; g.fill(p);
  g.globalAlpha = 0.9;
  g.lineWidth = 2; g.strokeStyle = INK; g.stroke(p);
});

// OpenStreetMap's camera:direction, as bearings. Mappers write degrees
// ("90"), compass points ("NE", "north"), ranges ("45-135", read as their
// middle) or several at once ("90;270"). "forward" and "backward" are
// relative to a road this point does not know, so they give nothing.
const POINT_DEG = { N: 0, NNE: 22.5, NE: 45, ENE: 67.5, E: 90, ESE: 112.5, SE: 135, SSE: 157.5,
  S: 180, SSW: 202.5, SW: 225, WSW: 247.5, W: 270, WNW: 292.5, NW: 315, NNW: 337.5,
  NORTH: 0, EAST: 90, SOUTH: 180, WEST: 270, NORTHEAST: 45, NORTHWEST: 315, SOUTHEAST: 135,
  SOUTHWEST: 225 };
const wrap = (d) => ((d % 360) + 360) % 360;
export function facingDegrees(v) {
  if (v == null || v === "") return [];
  return String(v).split(/[;,]/).map((s) => s.trim().toUpperCase()).map((s) => {
    if (/^-?\d+(\.\d+)?$/.test(s)) return wrap(Number(s));
    const r = s.match(/^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$/);
    if (r) { const a = Number(r[1]); let b = Number(r[2]); if (b < a) b += 360; return wrap((a + b) / 2); }
    return POINT_DEG[s.replace(/[^A-Z]/g, "")] ?? null;
  }).filter((d) => d != null).slice(0, 4);
}

// -- observed (Sentinel-1) ---------------------------------------------------
//
// The 2D view's foreign encoding, kept: a HOLLOW ring and a cross in one
// slate tone for an unexplained return, a fainter ring for a fixed structure.
// Nothing else on either map is hollow. Drawn in their colours with a pale
// halo, so they read on imagery too; the billboard is not tinted.
export const OBS = { cand: "#2f4858", site: "#9aa7ad" };

export const candRing = () => glyph("cand", (g) => {
  g.beginPath(); g.arc(32, 32, 20, 0, Math.PI * 2);
  g.lineWidth = 9; g.strokeStyle = "rgba(255,255,255,.8)"; g.stroke();
  g.lineWidth = 4.5; g.strokeStyle = OBS.cand; g.stroke();
  g.beginPath(); g.moveTo(32, 21); g.lineTo(32, 43); g.moveTo(21, 32); g.lineTo(43, 32);
  g.lineWidth = 6; g.strokeStyle = "rgba(255,255,255,.8)"; g.stroke();
  g.lineWidth = 3; g.strokeStyle = OBS.cand; g.stroke();
});

export const siteRing = () => glyph("site", (g) => {
  g.beginPath(); g.arc(32, 32, 18, 0, Math.PI * 2);
  g.lineWidth = 7; g.strokeStyle = "rgba(255,255,255,.7)"; g.stroke();
  g.lineWidth = 3.5; g.strokeStyle = OBS.site; g.stroke();
});

// A searched point (the coverage probe) and a found place: a pin.
export const pin = (color = "#0e1a24") => glyph(`pin${color}`, (g) => {
  const p = new Path2D("M32 60 C26 46 14 38 14 25 A18 18 0 0 1 50 25 C50 38 38 46 32 60 Z");
  g.lineWidth = 4; g.strokeStyle = "#fff"; g.stroke(p);
  g.fillStyle = color; g.fill(p);
  g.beginPath(); g.arc(32, 25, 7, 0, Math.PI * 2); g.fillStyle = "#fff"; g.fill();
});

// Where the browser says the viewer is: the platform convention, a blue dot
// with a white rim.
export const here = () => glyph("here", (g) => {
  g.beginPath(); g.arc(32, 32, 14, 0, Math.PI * 2);
  g.fillStyle = "#1a73e8"; g.fill();
  g.lineWidth = 5; g.strokeStyle = "#fff"; g.stroke();
});

// -- context -----------------------------------------------------------------

export const satellite = () => glyph("sat", (g) => {
  g.translate(32, 32); g.rotate(Math.PI / 4);
  const body = new Path2D("M-7 -7 H7 V7 H-7 Z");
  const w1 = new Path2D("M-27 -5 H-11 V5 H-27 Z");
  const w2 = new Path2D("M11 -5 H27 V5 H11 Z");
  for (const p of [w1, w2, body]) fillStroke(g, p, 4);
  g.strokeStyle = INK; g.lineWidth = 3;
  g.beginPath(); g.moveTo(-11, 0); g.lineTo(-7, 0); g.moveTo(7, 0); g.lineTo(11, 0); g.stroke();
});

// Selection: four corner brackets, the way a sensor boxes a target.
export const reticle = () => glyph("reticle", (g) => {
  const L = 16, a = 5, b = 59;
  const p = new Path2D(
    `M${a} ${a + L} V${a} H${a + L} M${b - L} ${a} H${b} V${a + L} ` +
    `M${b} ${b - L} V${b} H${b - L} M${a + L} ${b} H${a} V${b - L}`);
  g.lineWidth = 8; g.strokeStyle = INK; g.stroke(p);
  g.lineWidth = 4; g.strokeStyle = "#fff"; g.stroke(p);
}, 64);

// Emergency squawk: a ring that the view pulses.
export const ring = () => glyph("ring", (g) => {
  g.beginPath(); g.arc(32, 32, 26, 0, Math.PI * 2);
  g.lineWidth = 8; g.strokeStyle = INK; g.stroke();
  g.lineWidth = 4.5; g.strokeStyle = "#fff"; g.stroke();
});

function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y); g.lineTo(x + w - r, y); g.quadraticCurveTo(x + w, y, x + w, y + r);
  g.lineTo(x + w, y + h - r); g.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  g.lineTo(x + r, y + h); g.quadraticCurveTo(x, y + h, x, y + h - r);
  g.lineTo(x, y + r); g.quadraticCurveTo(x, y, x + r, y);
  g.closePath();
}

// -- colour ------------------------------------------------------------------

// ONE PALETTE FOR BOTH VIEWS (2026-10-28). The 3D view first had its own,
// brighter colours; Joshua asked for it to match the 2D viewer, so the
// ramps and group colours below are the 2D viewer's own (views/live.js
// ALT_COLOR, views/vessels.js GROUPS) and a contact is the same colour in
// both. Aircraft are drawn the 2D way too: a white plane on a solid disc
// coloured by altitude, with a white rim.

// Altitude in metres, as views/live.js ALT_COLOR.
export const ALT_STOPS = [
  [0, "#a8175c"], [1500, "#c2703f"], [4000, "#9a8f2a"], [8000, "#3d8f6a"], [12000, "#10635f"],
];
export const GROUND = "#6b7c89";
export const EMERGENCY = "#9b2c1f";

const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

// Quantised to 500 m, so the globe's texture atlas holds 25 discs, not one
// per aircraft.
export function altColour(altM, onGround) {
  if (onGround) return GROUND;
  const m = Math.min(12000, Math.round(Math.max(0, altM ?? 0) / 500) * 500);
  for (let i = 1; i < ALT_STOPS.length; i += 1) {
    if (m <= ALT_STOPS[i][0]) {
      const [f0, c0] = ALT_STOPS[i - 1], [f1, c1] = ALT_STOPS[i];
      const t = (m - f0) / (f1 - f0);
      const a = hex(c0), b = hex(c1);
      return "#" + a.map((v, k) => Math.round(v + (b[k] - v) * t)
        .toString(16).padStart(2, "0")).join("");
    }
  }
  return ALT_STOPS[ALT_STOPS.length - 1][1];
}

const PLANE = new Path2D(
  "M32 4 C35 4 36 8 36 12 L36 24 L58 36 L58 41 L36 35 L35 50 L43 56 L43 60 " +
  "L32 57 L21 60 L21 56 L29 50 L28 35 L6 41 L6 36 L28 24 L28 12 C28 8 29 4 32 4 Z");

// The 2D symbol: a white plane on a disc in the altitude colour.
export const planeDisc = (color) => glyph(`pd${color}`, (g) => {
  g.beginPath(); g.arc(32, 32, 28.5, 0, Math.PI * 2);
  g.fillStyle = color; g.fill();
  g.lineWidth = 3; g.strokeStyle = "#ffffff"; g.stroke();
  g.save(); g.translate(32, 32); g.scale(0.68, 0.68); g.translate(-32, -32);
  g.lineWidth = 3; g.strokeStyle = "rgba(14,26,36,.45)"; g.stroke(PLANE);
  g.fillStyle = "#ffffff"; g.fill(PLANE);
  g.restore();
});

// The 2D hull: filled in the group colour with a white edge; undeclared is
// hollow with a slate edge.
export const hullFilled = (color, hollow = false) => glyph(`hf${color}${hollow}`, (g) => {
  g.translate(32, 34); g.scale(1.38, 1.38);
  const p = new Path2D(HULL);
  g.lineWidth = 2.6; g.strokeStyle = hollow ? HOLLOW : "#ffffff"; g.stroke(p);
  g.fillStyle = hollow ? "rgba(255,255,255,.85)" : color; g.fill(p);
  if (hollow) { g.lineWidth = 1.8; g.stroke(p); }
});

// A stopped vessel: the 2D dot, no bow.
export const dotFilled = (color, hollow = false) => glyph(`df${color}${hollow}`, (g) => {
  g.beginPath(); g.arc(32, 32, 15, 0, Math.PI * 2);
  g.fillStyle = hollow ? "rgba(255,255,255,.75)" : color; g.fill();
  g.lineWidth = hollow ? 4 : 3.5; g.strokeStyle = hollow ? HOLLOW : "#ffffff"; g.stroke();
});

const HOLLOW = "#6b7c89";

// Far from the camera every contact is this: a dot in its colour with a
// white rim, drawn at twice the size it is shown, so it stays crisp where a
// shrunk pictogram turned to noise (2026-11-02). 2D draws the same at low
// zoom.
export const dot = (color, hollow = false) => glyph(`dot${color}${hollow}`, (g, n) => {
  const r = n / 2;
  g.beginPath(); g.arc(r, r, r - 2.5, 0, Math.PI * 2);
  g.fillStyle = hollow ? "rgba(255,255,255,.85)" : color; g.fill();
  g.lineWidth = 2.5; g.strokeStyle = hollow ? HOLLOW : "#ffffff"; g.stroke();
  g.beginPath(); g.arc(r, r, r - 0.6, 0, Math.PI * 2);
  g.lineWidth = 1.2; g.strokeStyle = "rgba(14,26,36,.55)"; g.stroke();
}, 20);

// As views/vessels.js GROUPS.
export const SEA_GROUPS = [
  { key: "commercial", label: "Commercial", color: "#2a78d6",
    subtypes: ["cargo", "tanker", "passenger", "highspeed"] },
  { key: "fishing", label: "Fishing", color: "#eb6834", subtypes: ["fishing"] },
  { key: "government", label: "Gov / military", color: "#4a3aa7", subtypes: ["government"] },
  { key: "other", label: "Other traffic", color: "#6b7c89",
    subtypes: ["tug", "service", "pleasure", "other"] },
  { key: "unknown", label: "Undeclared", color: "#6b7c89", hollow: true, subtypes: ["unknown"] },
];
export const SEA_GROUP_OF = Object.fromEntries(
  SEA_GROUPS.flatMap((g) => g.subtypes.map((s) => [s, g])));

// Mode by feed. GTFS-RT positions do not say what they are, and the
// selected border feeds that are not buses are few and known (G2b).
export const MODE_OF_FEED = {
  "tld-4451-vp": "rail", "tld-1127-vp": "rail", "mdb-1853": "rail", "mdb-3060": "rail",
  "tld-4337-vp": "ferry", "mdb-3199": "ferry",
};
export const MODES = {
  // Most feeds do not say which mode a vehicle is; unknown is drawn as a bus.
  bus: { label: "Bus / other", color: "#12a35a", glyph: bus },
  rail: { label: "Rail", color: "#c99400", glyph: rail },
  ferry: { label: "Ferry", color: "#0f97a8", glyph: ferry },
};
export const NO_TRIP = "#8d96a0";

export const COL = {
  workzone: "#f08c00",
  camStream: "#1f6fe5",
  camNone: "#7b8794",
  alpr: "#c2185b",
  plow: "#d9480f",
  rail: "#1d4f91",            // intercity rail: Amtrak, VIA, Brightline
  state: "#1b2a36",
  fire: "#e8590c",
  select: "#ffffff",          // the reticle: white, its outline dark
  trail: "#a8175c",           // the followed contact's reported fixes: the 2D accent
};

export const SAT_GROUPS = {
  stations: { label: "Space stations", color: "#ffffff" },
  visual: { label: "Brightest", color: "#8fb8de" },
  "gps-ops": { label: "GPS", color: "#3d8f6a" },
};

// Quakes: size is magnitude, colour is depth (shallow ones are felt).
export const QUAKE_DEPTHS = [
  [10, "#fff15a", "< 10 km"], [70, "#ffa22e", "10–70 km"],
  [300, "#ff4f2e", "70–300 km"], [Infinity, "#c2185b", "> 300 km"],
];
export const quakeColour = (d) => (QUAKE_DEPTHS.find(([m]) => (d ?? 0) < m) || QUAKE_DEPTHS[3])[1];

