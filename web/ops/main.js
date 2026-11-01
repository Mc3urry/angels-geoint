// ANGELS 3D view: a globe beside the 2D research viewer, in its interface.
//
// After God's Eye View (github.com/bilawalsidhu/gods-eye-view), with the
// project's rules kept:
//
//   * Evidence and context are separate groups, and every card says which
//     it is. Cooperative positions are self-reported.
//   * The camera moves only when asked: a region, "Fly to", "Follow", or a
//     search hit. Switching a layer on never moves the map.
//   * Nothing is stored. Trails are the fixes this tab has heard, and they
//     are gone when it is closed.
//   * Polling only while a layer is on and the tab is visible.
//
// 2026-10-28, second pass, at Joshua's request: the same look as the 2D
// viewer (its stylesheet, its card, its colours), the simulated sensor
// looks removed, and a layer list cut to what is used.

import { API, get, esc, requests } from "./api.js";
import * as I from "./icons.js";
import { REGIONS, makeAir, makeSea, makeLand, makeRail, makePlows, WorkZones, Cameras, States, ageText, RAIL_COLOR } from "./layers.js";
import { Satellites, Alpr } from "./context.js";
import { card as cardHtml } from "../views/popup.js";

const Cesium = window.Cesium;
const $ = (s) => document.querySelector(s);

// -- the globe ----------------------------------------------------------------

// THE LIGHT MAP. CARTO's Positron is the 2D viewer's style, but its tiles
// need a key since 2026, and an unkeyed tile is a watermark. The key stays
// on the server (routes/basemap.py adds it), so the page only asks the API
// whether one is set: if it is, tiles come through /basemap/carto; if not,
// Esri's light grey, which needs no key, stands in.
let carto = false;
const BASEMAPS = {
  map: () => carto
    ? [new Cesium.UrlTemplateImageryProvider({
        url: `${API}/basemap/carto/light_all/{z}/{x}/{y}.png`, maximumLevel: 19,
        credit: "© OpenStreetMap contributors © CARTO" })]
    : [new Cesium.UrlTemplateImageryProvider({
        url: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        maximumLevel: 16, credit: "Esri, HERE, Garmin, © OpenStreetMap contributors" }),
       new Cesium.UrlTemplateImageryProvider({
        url: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
        maximumLevel: 16, credit: "Labels: Esri" })],
  imagery: () => [
    new Cesium.UrlTemplateImageryProvider({
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      maximumLevel: 19, credit: "Imagery: Esri, Maxar, Earthstar Geographics" }),
    new Cesium.UrlTemplateImageryProvider({
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
      maximumLevel: 19, credit: "Labels: Esri" }),
  ],
};

const viewer = new Cesium.Viewer("map", {
  baseLayer: false,
  baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
  navigationHelpButton: false, animation: false, timeline: false, fullscreenButton: false,
  infoBox: false, selectionIndicator: false,
  terrainProvider: new Cesium.EllipsoidTerrainProvider(),
  msaaSamples: 4,
});
const scene = viewer.scene;
scene.globe.baseColor = Cesium.Color.fromCssColorString("#eef2f5");
scene.fog.enabled = false;
scene.postProcessStages.fxaa.enabled = true;
viewer.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
viewer.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_CLICK);

let baseLayers = [];
let baseKey = "map";
function setBasemap(key) {
  baseKey = key;
  for (const l of baseLayers) viewer.imageryLayers.remove(l, true);
  baseLayers = BASEMAPS[key]().map((p, i) => {
    const l = viewer.imageryLayers.addImageryProvider(p, i);
    if (key === "imagery" && i === 1) l.alpha = 0.85;
    return l;
  });
  scene.globe.baseColor = Cesium.Color.fromCssColorString(key === "map" ? "#eef2f5" : "#0e1a24");
  segOn("#basemaps", "base", key);
}

const segOn = (sel, attr, val) => document.querySelectorAll(`${sel} button`)
  .forEach((b) => b.classList.toggle("on", b.dataset[attr] === val));

// -- layers -------------------------------------------------------------------

const ev = {
  air: makeAir(viewer),
  sea: makeSea(viewer),
  land: makeLand(viewer),
  rail: makeRail(viewer),
  plow: makePlows(viewer),
  wz: new WorkZones(viewer),
  cam: new Cameras(viewer),
  states: new States(viewer),
};
const ctx = {
  sat: new Satellites(viewer),
  alpr: new Alpr(viewer),
};
const fleets = [ev.air, ev.sea, ev.land, ev.rail, ev.plow];

let region = "air";
const state = { labels: true, trails: true, seaGroups: new Set(I.SEA_GROUPS.map((g) => g.key)),
                inServiceOnly: false };
const on = { air: true, sea: true, land: false, rail: false, plow: false, speed: false, wz: false,
             cam: false, states: true, sat: false, alpr: false };

// ROAD SPEEDS: TomTom's flow tiles through our API (routes/traffic.py),
// which adds the key. Aggregate speed per road; no vehicle is in it.
const speed = {
  key: "speed", on: false, layer: null,
  async load() {
    const s = await get("/traffic/status");
    if (!s.flow) {
      const e = new Error("add TOMTOM_API_KEY to .env, then restart the API");
      throw e;
    }
    if (!this.layer) {
      this.layer = viewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
        url: `${API}/traffic/flow/{z}/{x}/{y}.png`, maximumLevel: 18, minimumLevel: 0,
        credit: "Traffic flow © TomTom" }));
      this.layer.alpha = 0.9;
    }
    this.layer.show = this.on;
    setStatus("speed", "speed against free flow, every road with probe data");
  },
  setOn(v) { this.on = v; if (this.layer) this.layer.show = v; },
  get count() { return null; },
};
ev.speed = speed;

const missing = (keys) => Object.entries(keys || {}).filter(([, v]) => !v).map(([k]) => k);
const status = {};       // key -> {text, kind}

function setStatus(key, text, kind = "ok") { status[key] = { text, kind }; }

const fails = (key) => (e) => {
  const msg = e.kind === "offline" ? "API not reachable; is .\\app.ps1 running"
    : e.status === 502 ? "the source did not answer"
    : String(e.message || e).split(". ")[0];
  setStatus(key, msg, "err");
  renderSide();
};

// -- polling ------------------------------------------------------------------

const pollers = {
  air: { ms: () => REGIONS[region].airMs, run: async () => {
    const fc = await get(`/live?aoi=${region}`);
    ev.air.ingest(fc);
    const m = fc.properties || {};
    setStatus("air", `${m.source === "collector" ? "the collector" : "direct"}, ${
      m.dead_reckon === false ? "shown where last heard" : "projected between polls"}`,
      m.snapshot_age_s > 120 && region === "air" ? "warn" : "ok");
  } },
  sea: { ms: () => REGIONS[region].seaMs, run: async () => {
    const fc = await get(`/live?domain=sea&aoi=${region}`);
    ev.sea.ingest(fc);
    const m = fc.properties || {};
    setStatus("sea", m.warming ? `warming up, listening ${Math.round(m.listening_s || 0)} s`
      : `${m.source}, ${m.n} in the box`, m.warming ? "warn" : "ok");
  } },
  // Transit across the US. The API polls the national feeds only while
  // this asks, so polling here is what keeps them running.
  land: { ms: () => 15e3, run: async () => {
    const d = await get("/vehicles/us");
    ev.land.ingest(transitFeatures(d));
    const kind = d.warming ? "warn" : d.error ? "err" : "ok";
    const gaps = missing(d.keys);
    setStatus("land", d.warming
      ? `warming up: the API is asking every US feed (${d.n.toLocaleString()} so far)`
      : `${d.n_answered} of ${d.n_feeds} feeds answered${d.border.includes("not running") ? "; border collector not running" : ""}${
        gaps.length ? `. No key yet: ${gaps.join(", ")}` : ""}`,
      kind);
  } },
  plow: { ms: () => 60e3, run: async () => {
    const d = await get("/vehicles/plows");
    ev.plow.ingest(rows2fc(d.cols, d.rows, (p) => ({ ...p, bearing: p.heading })));
    const st = Object.entries(d.status).map(([k, v]) => `${k} ${v}`).join(", ");
    setStatus("plow", d.n ? st : `${st || "none"}; plows report only while working`, d.n ? "ok" : "warn");
  } },
  speed: { ms: () => 3600e3, run: () => speed.load() },
  rail: { ms: () => 30e3, run: async () => {
    const d = await get("/vehicles/amtrak");
    ev.rail.ingest(rows2fc(d.cols, d.rows, (p) => ({ ...p, bearing: p.heading })));
    setStatus("rail", `${d.n} trains running${d.stale_s ? `, ${Math.round(d.stale_s / 60)} min old` : ""}`,
      d.stale_s ? "warn" : "ok");
  } },
  wz: { ms: () => 600e3, run: async () => {
    await ev.wz.load(region);
    const m = ev.wz.meta || {};
    setStatus("wz", `${m.n_in_view ?? 0} declared in ${REGIONS[region].label}${m.as_points ? ", as points" : ""}`,
      m.truncated ? "warn" : "ok");
  } },
  // Every few seconds while the API's sources are still loading, then
  // every fifteen minutes.
  cam: { ms: () => (ev.cam.meta?.loading ? 8e3 : 900e3), run: async () => {
    const d = await ev.cam.load();
    const failed = Object.entries(d.sources).filter(([, b]) => b.failed.length && !b.n).map(([k]) => k);
    const gaps = missing(d.keys);
    setStatus("cam", d.loading ? `loading: ${d.n.toLocaleString()} so far`
      : `${Object.keys(d.sources).length - failed.length} publishers${failed.length ? `; did not answer: ${failed.join(", ")}` : ""}${
        gaps.length ? `. Via LiveTrafficCam until keyed: ${gaps.join(" ")}` : ""}`,
      d.loading || failed.length ? "warn" : "ok");
  } },
  states: { ms: () => 3600e3, run: async () => { await ev.states.load(region); setStatus("states", "the discontinuity"); } },
  sat: { ms: () => 6 * 3600e3, run: () => ctx.sat.load() },
  alpr: { ms: () => 1e9, run: () => ctx.alpr.load() },
};
const timers = {};

// Compact rows from the API to the features a Fleet reads.
function rows2fc(cols, rows, extra = (p) => p) {
  return {
    type: "FeatureCollection",
    features: rows.map((r) => {
      const p = {};
      cols.forEach((c, i) => { p[c] = r[i]; });
      return { type: "Feature", geometry: { type: "Point", coordinates: [p.lon, p.lat] }, properties: extra(p) };
    }),
  };
}

function transitFeatures(d) {
  const now = d.time;
  return rows2fc(d.cols, d.vehicles, (p) => {
    const fix = p.vehicle_ts || now;
    return { feed_id: p.feed_id, vehicle_key: p.vehicle_key, label: p.label || "", route_id: p.route_id || "",
             provider: d.providers[p.feed_id] || "", bearing: p.bearing, speed_mps: p.speed,
             fix_t: fix, age_s: Math.max(0, now - fix), age_from: p.vehicle_ts ? "vehicle" : "poll",
             in_service: !!p.in_service, motion: p.motion || "feed" };
  });
}

function schedule(key, now = false) {
  clearTimeout(timers[key]);
  if (!on[key]) return;
  const go = async () => {
    if (!on[key]) return;
    if (!document.hidden) {
      try { await pollers[key].run(); } catch (e) { fails(key)(e); }
      renderSide();
    }
    timers[key] = setTimeout(go, pollers[key].ms());
  };
  timers[key] = setTimeout(go, now ? 0 : pollers[key].ms());
}

function setLayer(key, value) {
  on[key] = value;
  const target = ev[key] || ctx[key];
  if (target.setShown) target.setShown(value);
  else target.setOn(value);
  if (value) schedule(key, true);
  else { clearTimeout(timers[key]); status[key] = null; }
  if (!value && sel && sel.layer === target) deselect();
  renderSide();
}

// -- the layer list -------------------------------------------------------------

const GROUPS = [
  { title: "Cooperative", note: "self-reported", keys: ["air", "sea", "land", "rail", "plow", "wz"] },
  { title: "Aggregate", note: "no vehicle in it", keys: ["speed"] },
  { title: "The state's own", keys: ["cam", "states"] },
  { title: "Context", note: "not evidence", keys: ["sat", "alpr"] },
];

const dot = (c) => `<span class="gl"><i style="width:12px;height:12px;border-radius:50%;background:${c};border:1.5px solid #fff;box-shadow:0 0 0 1px #0e1a24"></i></span>`;

const ROW = {
  air: { name: "Aircraft", icon: () => I.planeDisc(I.ALT_STOPS[2][1]),
    key: () => `<span class="ramp"></span><span class="ends"><span>ground</span><span>40,000 ft</span></span>` },
  sea: { name: "Vessels", icon: () => I.hullFilled(I.SEA_GROUPS[0].color),
    key: () => I.SEA_GROUPS.map((g) => `<span class="btn ${state.seaGroups.has(g.key) ? "" : "off"}"
      data-seagroup="${g.key}" title="Show or hide ${g.label}"><img src="${I.hullFilled(g.color, !!g.hollow)}" alt="">${g.label}</span>`).join("") },
  land: { name: "Transit, all US", icon: () => I.tinted(I.bus(), I.MODES.bus.color),
    key: () => Object.values(I.MODES).map((m) => `<span><img src="${I.tinted(m.glyph(), m.color)}" alt="">${m.label}</span>`).join("")
      + `<span class="btn ${state.inServiceOnly ? "off" : ""}" data-notrip title="Show or hide vehicles with no trip"><img src="${I.tinted(I.bus(), I.NO_TRIP)}" alt="">no trip</span>` },
  wz: { name: "Work zones", icon: () => I.tinted(I.workzone(), I.COL.workzone) },
  rail: { name: "Intercity rail", icon: () => I.tinted(I.rail(), RAIL_COLOR),
    key: () => `<span>Amtrak, VIA, Brightline</span>` },
  plow: { name: "DOT snowplows", icon: () => I.tinted(I.plow(), I.COL.plow),
    key: () => `<span>seasonal; Iowa DOT for now</span>` },
  speed: { name: "Road speeds", gl: `<span class="gl"><i style="width:20px;height:5px;border-radius:2px;background:linear-gradient(90deg,#3ec46d,#f7c948,#e8590c,#8b1a1a)"></i></span>`,
    key: () => `<span class="ramp" style="background:linear-gradient(90deg,#3ec46d,#f7c948,#e8590c,#8b1a1a)"></span><span class="ends"><span>free flow</span><span>stopped</span></span>` },
  cam: { name: "Traffic cameras", icon: () => I.tinted(I.camera(), I.COL.camStream),
    key: () => `<span><img src="${I.tinted(I.camera(), I.COL.camStream)}" alt="">live video</span>
      <span><img src="${I.tinted(I.camera(), I.COL.camNone)}" alt="">still frame</span>` },
  states: { name: "State lines", gl: `<span class="gl"><i style="width:18px;border-top:2.5px dashed #1b2a36"></i></span>` },
  sat: { name: "Satellites", icon: () => I.tinted(I.satellite(), "#8fb8de") },
  alpr: { name: "ALPR cameras", icon: () => I.tinted(I.alpr(), I.COL.alpr),
    key: () => `<span>locations only; shown below 1,500 km</span>` },
};

function countOf(key) {
  if (!on[key]) return "";
  const t = ev[key] || ctx[key];
  if ("items" in t) return t.countVisible().toLocaleString();
  const c = t.count;
  return c == null ? "" : Number(c).toLocaleString();
}

function statusOf(key) {
  const t = ctx[key];
  if (t && t.status && t.status !== "off") return { text: t.status, kind: t.statusKind || "ok" };
  return status[key];
}

let sideHtml = "";
function renderSide() {
  const html = GROUPS.map((g, gi) => `<div class="sec">${g.title}${
    g.note ? ` <span>&middot; ${g.note}</span>` : ""}${
    gi === 0 ? `<button class="hide" data-hide title="Hide the layer list">&lsaquo;</button>` : ""}</div>${
    g.keys.map((k) => {
      const r = ROW[k];
      const st = on[k] ? statusOf(k) : null;
      return `<button class="lyr ${on[k] ? "on" : ""}" data-toggle="${k}" aria-pressed="${on[k]}"
          title="${st ? esc(st.text) : on[k] ? "on" : "off"}">
          ${r.icon ? `<img src="${r.icon()}" alt="">` : r.gl}
          <span>${r.name}</span><span class="ct">${countOf(k)}</span>
          <span class="dot ${st ? st.kind : ""}"></span></button>
        ${st && st.kind === "err" ? `<div class="err1">${esc(st.text)}</div>` : ""}
        ${on[k] && r.key ? `<div class="key">${r.key()}</div>` : ""}`;
    }).join("")}`).join("")
    + `<div class="foot">
        <div class="seg" id="opts">
          <button data-opt="labels" class="${state.labels ? "on" : ""}" title="Labels (L)">Labels</button>
          <button data-opt="trails" class="${state.trails ? "on" : ""}" title="Trails of reported fixes (T)">Trails</button>
        </div>
        <p>Click a symbol to pin its card, double-click to follow it. Nothing here is stored.</p>
      </div>`;
  if (html !== sideHtml) { $("#side").innerHTML = html; sideHtml = html; }
}

$("#side").addEventListener("click", (e) => {
  const sg = e.target.closest("[data-seagroup]");
  if (sg) {
    const k = sg.dataset.seagroup;
    if (state.seaGroups.has(k)) state.seaGroups.delete(k); else state.seaGroups.add(k);
    ev.sea.filter = (p) => state.seaGroups.has(ev.sea.groupOf(p));
    ev.sea.tick(); renderSide(); return;
  }
  if (e.target.closest("[data-notrip]")) {
    state.inServiceOnly = !state.inServiceOnly;
    ev.land.filter = state.inServiceOnly ? (p) => p.in_service : null;
    ev.land.tick(); renderSide(); return;
  }
  if (e.target.closest("[data-hide]")) { setSide(false); return; }
  const t = e.target.closest("[data-toggle]");
  if (t) { setLayer(t.dataset.toggle, !on[t.dataset.toggle]); return; }
  const o = e.target.closest("[data-opt]");
  if (o) toggleOpt(o.dataset.opt);
});

function setSide(show) {
  document.body.classList.toggle("collapsed", !show);
  $("#show-side").hidden = show;
  // The globe's canvas has to be told its box changed.
  requestAnimationFrame(() => viewer.resize());
}
$("#show-side").onclick = () => setSide(true);

function toggleOpt(k) {
  state[k] = !state[k];
  for (const f of fleets) { f.showLabels = state.labels; f.showTrails = state.trails; }
  slow();
  renderSide();
}

// -- region, basemap --------------------------------------------------------------

function flyRegion(r, instant = false) {
  const v = REGIONS[r].view;
  const opts = {
    destination: Cesium.Cartesian3.fromDegrees(v.lon, v.lat, v.h),
    orientation: { heading: 0, pitch: Cesium.Math.toRadians(v.pitch), roll: 0 },
  };
  if (instant) viewer.camera.setView(opts); else viewer.camera.flyTo({ ...opts, duration: 2.2 });
}

function setRegion(r) {
  if (r === region) { flyRegion(r); return; }
  region = r;
  segOn("#regions", "region", r);
  for (const k of ["air", "sea", "wz", "states"]) if (on[k]) schedule(k, true);
  flyRegion(r);
}
$("#regions").onclick = (e) => { const b = e.target.closest("button"); if (b) setRegion(b.dataset.region); };
$("#basemaps").onclick = (e) => { const b = e.target.closest("button"); if (b) setBasemap(b.dataset.base); };

// -- selection, following, trail --------------------------------------------------

let sel = null;          // {kind, layer, id}
let following = false;
// The reticle holds still (it pulsed until 2026-11-01) and is white with a
// dark edge, so it reads on both basemaps and never looks like a contact.
// The trail keeps the accent: a line, not a mark, so it cannot be confused.
const ACCENT = Cesium.Color.fromCssColorString(I.COL.trail);
const MARK = Cesium.Color.fromCssColorString(I.COL.select);
const selPrims = scene.primitives.add(new Cesium.PolylineCollection());
const selBbs = scene.primitives.add(new Cesium.BillboardCollection({ scene }));
const reticle = selBbs.add({ image: I.reticle(), show: false, width: 50, height: 50,
  color: MARK, disableDepthTestDistance: Number.POSITIVE_INFINITY });
let glow = null, drop = null;

const proxy = viewer.entities.add({
  position: new Cesium.CallbackProperty(() => selPosition() || Cesium.Cartesian3.ZERO, false),
  point: { pixelSize: 1, color: Cesium.Color.TRANSPARENT },
});

function selPosition() {
  if (!sel) return null;
  return sel.layer.position(sel.id);
}
const selItem = () => (sel && sel.kind === "fleet" ? sel.layer.items.get(sel.id) : null);

function select(pick) {
  sel = pick;
  if (sel.kind === "sat") ctx.sat.showOrbit(sel.id); else ctx.sat.orbit.removeAll();
  if (following) follow(true);
  renderCard();
  fast();
}

function deselect() {
  sel = null;
  unfollow();
  ctx.sat.orbit.removeAll();
  reticle.show = false;
  for (const p of [glow, drop]) if (p) p.show = false;
  $("#card").hidden = true;
}

function follow(yes = true) {
  if (!yes || !sel) { unfollow(); return; }
  following = true;
  const k = sel.kind === "fleet" ? sel.layer.key : sel.kind;
  proxy.viewFrom = {
    air: new Cesium.Cartesian3(0, -5200, 2600),
    sea: new Cesium.Cartesian3(0, -1800, 1100),
    land: new Cesium.Cartesian3(0, -520, 380),
    sat: new Cesium.Cartesian3(0, -900e3, 500e3),
  }[k] || new Cesium.Cartesian3(0, -1500, 1200);
  viewer.trackedEntity = undefined;
  viewer.trackedEntity = proxy;
  renderCard();
}

function unfollow() {
  if (!following) return;
  following = false;
  viewer.trackedEntity = undefined;
  renderCard();
}

function flyToSel() {
  const p = selPosition();
  if (!p) return;
  const range = sel.kind === "sat" ? 2.5e6 : sel.kind === "fleet" && sel.layer.key === "air" ? 25e3 : 4e3;
  viewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(p, 10), {
    offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-40), range), duration: 1.8 });
}

function cardOf(pick = sel) {
  if (!pick) return null;
  if (pick.kind === "fleet") {
    const it = pick.layer.items.get(pick.id);
    return it ? pick.layer.card(it) : null;
  }
  return pick.layer.card(pick.id);
}

// The 2D card, unchanged, with the 3D actions under it. The two parts are
// replaced separately: the card's age pill changes every second, and
// rebuilding the buttons with it would swallow a click that landed between.
let bodyKey = "", actsKey = "";
function renderCard() {
  const c = cardOf();
  const el = $("#card");
  if (!c) { el.hidden = true; return; }
  if (!el.firstElementChild) el.innerHTML = '<div id="cbody"></div><div class="acts" id="cacts"></div>';
  const body = cardHtml(c).replace('<div class="ph">',
    '<div class="ph"><button class="x" data-act="close" title="Close (Esc)">&times;</button>');
  const acts = [
    `<button data-act="follow" class="${following ? "on" : ""}"
      title="Keep the camera on it (F)">${following ? "Following" : "Follow"}</button>`,
    `<button data-act="fly">Fly to</button>`,
    c.watch ? `<button data-act="watch" data-cam="${sel.id}">Watch</button>` : "",
    c.link ? `<a href="${esc(c.link[0])}" target="_blank" rel="noopener noreferrer">${esc(c.link[1])} &#8599;</a>` : "",
  ].join("");
  if (body !== bodyKey) { $("#cbody").innerHTML = body; bodyKey = body; }
  if (acts !== actsKey) { $("#cacts").innerHTML = acts; actsKey = acts; }
  $("#cacts").style.setProperty("--chip", c.chip || "transparent");
  el.hidden = false;
}

$("#card").addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const a = b.dataset.act;
  if (a === "close") deselect();
  else if (a === "follow") follow(!following);
  else if (a === "fly") { unfollow(); flyToSel(); }
  else if (a === "watch") watch(Number(b.dataset.cam));
});

// -- camera video ---------------------------------------------------------------

// THREE KINDS OF PICTURE, each played from its publisher, none stored:
//   player  Maryland CHART's own player, in a sandboxed frame
//   hls     a live stream (DelDOT), played with hls.js where the browser
//           cannot play HLS itself
//   image   a still frame the publisher refreshes; asked for again every
//           ten seconds while the panel is open
let frameTimer = 0;
let hlsPlayer = null;

function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  return new Promise((ok, no) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js";
    s.onload = () => ok(window.Hls);
    s.onerror = () => no(new Error("hls.js did not load"));
    document.head.append(s);
  });
}

function stopPicture() {
  clearInterval(frameTimer);
  if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
  $("#vbody").textContent = "";
}

async function watch(i) {
  const c = ev.cam.row(i);
  if (!c || !c.media) return;
  stopPicture();
  $("#vtitle").textContent = c.name || "Traffic camera";
  $("#vsrc").textContent = c.src;
  if (c.page) $("#vlink").href = c.page; else $("#vlink").removeAttribute("href");
  $("#vlink").hidden = !c.page;
  $("#video").hidden = false;
  const body = $("#vbody");
  if (c.kind === "player") {
    body.textContent = "asking for CHART's player";
    let s;
    try { s = await get(c.media); } catch (e) {
      body.textContent = e.status === 404
        ? "No stream link on this machine. Run: python scripts/fetch_road_mask.py --layer cameras"
        : `No stream: ${e.message}`;
      return;
    }
    if (s.page) { $("#vlink").href = s.page; $("#vlink").hidden = false; }
    body.textContent = "";
    if (s.embed && s.embeddable !== false) {
      const fr = document.createElement("iframe");
      fr.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups");
      fr.setAttribute("referrerpolicy", "no-referrer");
      fr.setAttribute("allow", "autoplay; fullscreen");
      fr.src = s.embed;
      body.append(fr);
    } else body.textContent = "CHART does not allow its player to be framed here. Use the publisher's page.";
  } else if (c.kind === "hls") {
    const v = document.createElement("video");
    v.muted = true; v.autoplay = true; v.playsInline = true; v.controls = true;
    body.append(v);
    if (v.canPlayType("application/vnd.apple.mpegurl")) v.src = c.media;
    else {
      try {
        const Hls = await loadHls();
        hlsPlayer = new Hls({ lowLatencyMode: true });
        hlsPlayer.loadSource(c.media);
        hlsPlayer.attachMedia(v);
      } catch (e) { body.textContent = String(e.message); }
    }
  } else {
    const img = document.createElement("img");
    img.alt = c.name || "camera frame";
    img.referrerPolicy = "no-referrer";
    const src = c.media.startsWith("/") ? `${API}${c.media}` : c.media;
    const show = () => { img.src = `${src}${src.includes("?") ? "&" : "?"}t=${Math.floor(Date.now() / 10e3)}`; };
    img.onerror = () => { body.dataset.err = "1"; img.replaceWith(Object.assign(document.createElement("span"),
      { textContent: "The publisher did not send a frame." })); clearInterval(frameTimer); };
    show();
    body.append(img);
    frameTimer = setInterval(show, 10e3);
  }
}
const closeVideo = () => { stopPicture(); $("#video").hidden = true; };
$("#vclose").onclick = closeVideo;

// -- picking: hover and click ---------------------------------------------------------

function pickAt(pos) {
  const picked = scene.pick(pos);
  if (!Cesium.defined(picked)) return null;
  const id = picked.id;
  if (id && id.kind && id.layer) return id;
  return null;
}

// Beside the cursor, on the side toward the middle of the globe, so it is
// never clipped and never covers the symbol: the 2D viewer's rule.
function placeTip(x, y) {
  const t = $("#tip");
  const r = scene.canvas.getBoundingClientRect();
  const w = t.offsetWidth, h = t.offsetHeight, gap = 14;
  const left = x + r.left < r.left + r.width / 2 ? x + r.left + gap : x + r.left - gap - w;
  const top = y + r.top < r.top + r.height / 2 ? y + r.top + gap : y + r.top - gap - h;
  t.style.left = `${Math.max(r.left + 4, Math.min(left, r.right - w - 4))}px`;
  t.style.top = `${Math.max(r.top + 4, Math.min(top, r.bottom - h - 4))}px`;
}

let hoverPending = null;
const handler = new Cesium.ScreenSpaceEventHandler(scene.canvas);
handler.setInputAction((m) => { hoverPending = m.endPosition; }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

const fmtLat = (d) => `${Math.abs(d).toFixed(4)}°${d >= 0 ? "N" : "S"}`;
const fmtLon = (d) => `${Math.abs(d).toFixed(4)}°${d >= 0 ? "E" : "W"}`;
const fmtDist = (m) => (m > 10e3 ? `${(m / 1000).toFixed(m > 1e6 ? 0 : 1)} km` : `${Math.round(m)} m`);
let cursor = "";

function hoverFrame() {
  if (hoverPending) {
    const pos = hoverPending;
    hoverPending = null;
    const p = pickAt(pos);
    const c = p && !(sel && p.layer === sel.layer && p.id === sel.id) ? cardOf(p) : null;
    scene.canvas.style.cursor = p ? "pointer" : "";
    if (c) {
      // The header of the 2D card only: who, how fresh, and the hint.
      $("#tip").innerHTML = cardHtml({ title: c.title, sub: c.sub, chip: c.chip, age: c.age,
        ageState: c.ageState, hint: "Click to pin" });
      $("#tip").hidden = false;
      placeTip(pos.x, pos.y);
    } else $("#tip").hidden = true;
    const g = viewer.camera.pickEllipsoid(pos);
    if (g) {
      const k = Cesium.Cartographic.fromCartesian(g);
      cursor = `${fmtLat(Cesium.Math.toDegrees(k.latitude))} ${fmtLon(Cesium.Math.toDegrees(k.longitude))}`;
    }
  }
  requestAnimationFrame(hoverFrame);
}
requestAnimationFrame(hoverFrame);
scene.canvas.addEventListener("mouseleave", () => { $("#tip").hidden = true; scene.canvas.style.cursor = ""; });

handler.setInputAction((c) => {
  const p = pickAt(c.position);
  $("#tip").hidden = true;
  if (p) select(p); else deselect();
}, Cesium.ScreenSpaceEventType.LEFT_CLICK);
handler.setInputAction((c) => {
  const p = pickAt(c.position);
  if (p) { select(p); follow(true); }
}, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

// -- search -----------------------------------------------------------------------------

const hits = $("#hits");
$("#search input").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  if (q.length < 2) { hits.hidden = true; return; }
  const found = [];
  for (const f of fleets) if (on[f.key]) for (const it of f.find(q)) found.push([f, it]);
  if (on.sat) for (const s of ctx.sat.sats) if (s.name.toLowerCase().includes(q)) found.push([ctx.sat, s]);
  hits.innerHTML = found.slice(0, 12).map(([f, it], i) => {
    const isSat = f === ctx.sat;
    const label = isSat ? it.name : f.labelOf(it.props) || it.id;
    const img = isSat ? I.tinted(I.satellite(), it.color) : it.bb.image;
    return `<li data-i="${i}"><img src="${img}" alt="">${esc(label)}<small>${isSat ? "satellite" : f.key}</small></li>`;
  }).join("") || `<li>No match among the layers that are on.</li>`;
  hits._found = found;
  hits.hidden = false;
});
hits.addEventListener("click", (e) => {
  const li = e.target.closest("li[data-i]");
  if (!li) return;
  const [f, it] = hits._found[Number(li.dataset.i)];
  select(f === ctx.sat ? { kind: "sat", layer: ctx.sat, id: it.norad } : { kind: "fleet", layer: f, id: it.id });
  flyToSel();
  hits.hidden = true;
});
document.addEventListener("click", (e) => { if (!e.target.closest("#search")) hits.hidden = true; });

// -- keyboard -------------------------------------------------------------------------------

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input")) { if (e.key === "Escape") e.target.blur(); return; }
  if (e.key === "Escape") {
    if (!$("#video").hidden) closeVideo();
    else if (following) unfollow(); else deselect();
  } else if (e.key === "l" || e.key === "L") toggleOpt("labels");
  else if (e.key === "t" || e.key === "T") toggleOpt("trails");
  else if ((e.key === "f" || e.key === "F") && sel) follow(!following);
});

// -- the loops ------------------------------------------------------------------------------

// 4 Hz: positions, the reticle and the selected trail.
function fast() {
  const now = Date.now();
  for (const f of fleets) f.tick(now);
  const pos = selPosition();
  if (!sel || !pos) {
    reticle.show = false;
    for (const p of [glow, drop]) if (p) p.show = false;
    return;
  }
  reticle.position = pos;
  reticle.show = true;
  const it = selItem();
  if (it && it.hist.length) {
    const pts = it.hist.map(([lo, la, al]) => Cesium.Cartesian3.fromDegrees(lo, la, al)).concat([pos]);
    if (!glow) glow = selPrims.add({ width: 5, material: Cesium.Material.fromType("PolylineGlow", { color: ACCENT, glowPower: 0.2 }) });
    glow.positions = pts; glow.show = true;
  } else if (glow) glow.show = false;
  if (it && sel.layer.key === "air" && it.now && it.now.alt > 50) {
    if (!drop) drop = selPrims.add({ width: 1.5, material: Cesium.Material.fromType("PolylineDash", { color: ACCENT, dashLength: 10 }) });
    drop.positions = [pos, Cesium.Cartesian3.fromDegrees(it.now.lon, it.now.lat, 0)];
    drop.show = true;
  } else if (drop) drop.show = false;
}

// 1 Hz: trails, labels, satellites, the card and the readout.
function slow() {
  const now = Date.now();
  const h = viewer.camera.positionCartographic.height;
  for (const f of fleets) { f.trailPass(now); f.labelPass(h, viewer.camera); }
  ctx.sat.tick();
  if (sel) renderCard();
  let out = `${cursor ? `${cursor} · ` : ""}eye ${fmtDist(h)}`;
  if (sel && following) {
    const p = selPosition();
    if (p) out += ` · following at ${fmtDist(Cesium.Cartesian3.distance(viewer.camera.positionWC, p))}`;
  }
  $("#readout").textContent = out;
  renderSide();
}

// If a followed contact leaves the feed, say so rather than follow nothing.
for (const f of fleets) {
  f.onGone = (layer, id) => {
    if (sel && sel.kind === "fleet" && sel.layer === layer && sel.id === id) {
      toast(`${layer.key === "air" ? "Aircraft" : layer.key === "sea" ? "Vessel" : "Vehicle"} no longer reported`);
      deselect();
    }
  };
}

let toastT = 0;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => { t.hidden = true; }, 4000);
}

// ALPR asks for the view in front of the camera, after the camera stops.
let alprT = 0;
viewer.camera.moveEnd.addEventListener(() => {
  if (!on.alpr) return;
  clearTimeout(alprT);
  alprT = setTimeout(() => ctx.alpr.load().then(renderSide, fails("alpr")), 600);
});
viewer.camera.percentageChanged = 0.05;

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) for (const k of Object.keys(on)) if (on[k]) schedule(k, true);
});

// -- start -------------------------------------------------------------------------------

setBasemap("map");
get("/basemap/status").then((s) => {
  carto = !!s.carto;
  if (carto && baseKey === "map") setBasemap("map");
}).catch(() => {});
flyRegion("air", true);
for (const f of fleets) f.setShown(on[f.key]);
for (const [k, v] of Object.entries(on)) {
  const t = ev[k] || ctx[k];
  if (!t.items) t.setOn(v);
  if (v) schedule(k, true);
}
renderSide();
setInterval(fast, 250);
setInterval(slow, 1000);

// For the headless check and for the console.
window.__ops = { viewer, ev, ctx, on, setLayer, select, follow, unfollow, setRegion, setSide, status, requests };
