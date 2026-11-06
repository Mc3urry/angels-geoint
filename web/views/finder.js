// Search, follow, my location and the imagery basemap for the flat map,
// matching the globe (2026-11-03).
//
//   search    one box for contacts and places. Contacts on the layers that
//             are on are matched here as you type; places (a town, an
//             address, an airport, or "lat, lon") come from the API's
//             /geo/search once typing pauses. Only the text and the map's
//             centre are sent.
//   follow    choosing a moving contact keeps the map on it, at its own
//             dead-reckoned position, until you pan or press Stop.
//   location  MapLibre's own control: the browser asks, and the position is
//             drawn here and sent nowhere.
//   imagery   Esri World Imagery under the data layers, as in 3D.

import { project, HORIZON_S } from "./reckon.js";
import { esc } from "./popup.js";
import * as G from "../ops/icons.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";

// Where a fleet item is now: its fix, projected along its own heading at its
// own speed for no longer than the domain's horizon (reckon.js's rule).
export function nowOf(it, domain, horizonS) {
  const age = (Date.now() - it.t) / 1000;
  const h = horizonS ?? HORIZON_S[domain] ?? 120;
  if (it.speed_mps > 0.5 && it.heading != null && age > 0 && age < h) {
    return project(it.coords[1], it.coords[0], it.heading, it.speed_mps * age);
  }
  return it.coords;
}

// `sources()` returns [{kind, icon, items, label(it), sub(it), where(it), hay(it)}].
export function addFinder(map, sources) {
  // -- the box ------------------------------------------------------------------
  const box = document.createElement("div");
  box.className = "maplibregl-ctrl finder";
  box.innerHTML = `<input type="search" placeholder="Search contacts or places" aria-label="Search contacts or places"
      autocomplete="off" spellcheck="false"><ul hidden></ul>`;
  const input = box.querySelector("input");
  const list = box.querySelector("ul");
  map.addControl({ onAdd: () => box, onRemove: () => box.remove() }, "top-left");

  let found = [], places = [], placeT = 0, seq = 0;

  function render(q) {
    found = [];
    for (const src of sources()) {
      let n = 0;
      for (const it of src.items) {
        if (n >= (src.max || 5)) break;
        if (src.hay(it).toLowerCase().includes(q)) { found.push([src, it]); n += 1; }
      }
    }
    found = found.slice(0, 10);
    const contacts = found.map(([src, it], i) => `<li data-i="${i}"><img src="${src.icon(it)}" alt="">${
      esc(src.label(it))}<small>${esc(src.sub(it))}</small></li>`);
    const pl = places.map((p, i) => `<li data-p="${i}"><img src="${G.pin()}" alt="">${esc(p.name)}<small>${
      esc(p.label || p.kind)}</small></li>`);
    list.innerHTML = (contacts.length ? `<li class="hd">On the map</li>${contacts.join("")}` : "")
      + (pl.length ? `<li class="hd">Places</li>${pl.join("")}` : "")
      || `<li class="none">${q.length >= 3 ? "Searching places&hellip;" : "No match among the layers that are on."}</li>`;
    list.hidden = false;
  }

  input.addEventListener("input", () => {
    const raw = input.value.trim();
    const q = raw.toLowerCase();
    places = [];
    clearTimeout(placeT);
    // A cleared box clears what it put on the map: the place pin.
    if (!raw) { pinMarker.remove(); seq += 1; }
    if (q.length < 2) { list.hidden = true; return; }
    render(q);
    if (q.length < 3) return;
    const mine = ++seq;
    placeT = setTimeout(async () => {
      const c = map.getCenter();
      try {
        const r = await fetch(`${API}/geo/search?q=${encodeURIComponent(raw)}&lon=${c.lng.toFixed(3)}&lat=${c.lat.toFixed(3)}`);
        const d = await r.json().catch(() => ({}));
        if (mine !== seq) return;
        if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : `HTTP ${r.status}`);
        places = d.results || [];
        render(q);
      } catch (e) {
        if (mine !== seq) return;
        list.insertAdjacentHTML("beforeend", `<li class="none">Places: ${
          esc(e.name === "TypeError" ? "API not reachable" : e.message)}</li>`);
      }
    }, 350);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") list.querySelector("li[data-i], li[data-p]")?.click();
    if (e.key === "Escape") { list.hidden = true; input.blur(); }
  });
  list.addEventListener("click", (e) => {
    const p = e.target.closest("li[data-p]");
    if (p) { goToPlace(places[Number(p.dataset.p)]); list.hidden = true; return; }
    const li = e.target.closest("li[data-i]");
    if (!li) return;
    const [src, it] = found[Number(li.dataset.i)];
    list.hidden = true;
    startFollow(src, it);
  });
  document.addEventListener("click", (e) => { if (!box.contains(e.target)) list.hidden = true; });

  // -- places -------------------------------------------------------------------
  const pinEl = document.createElement("img");
  pinEl.src = G.pin();
  pinEl.className = "place-pin";
  pinEl.alt = "";
  const pinMarker = new maplibregl.Marker({ element: pinEl, anchor: "bottom" });
  // Escape in an empty box, or the box's own clear (x) button, also clears it.
  input.addEventListener("search", () => { if (!input.value.trim()) pinMarker.remove(); });

  function goToPlace(p) {
    if (!p) return;
    stopFollow();
    pinMarker.setLngLat([p.lon, p.lat]).addTo(map);
    const b = p.bbox;
    if (b && b[2] - b[0] < 20 && b[3] - b[1] < 20 && (b[2] - b[0] > 0.002 || b[3] - b[1] > 0.002)) {
      map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 60, duration: 1200, maxZoom: 15 });
    } else {
      map.flyTo({ center: [p.lon, p.lat], zoom: Math.max(map.getZoom(), 13), duration: 1200 });
    }
    note(`${p.name}${p.label ? `, ${p.label}` : ""}`);
  }

  // -- follow -------------------------------------------------------------------
  const chip = document.createElement("div");
  chip.className = "follow-chip";
  chip.hidden = true;
  map.getContainer().appendChild(chip);
  let following = null, followT = 0;

  function startFollow(src, it) {
    stopFollow();
    const at = src.where(it);
    if (!at) return;
    map.flyTo({ center: at, zoom: Math.max(map.getZoom(), src.zoom || 11), duration: 1000 });
    if (src.onPick) src.onPick(it);
    if (!src.moves) { note(`${src.label(it)}: ${src.sub(it)}`); return; }
    following = { src, id: src.id(it) };
    chip.innerHTML = `<span>Following <b>${esc(src.label(it))}</b></span><button title="Stop following">Stop</button>`;
    chip.hidden = false;
    chip.querySelector("button").onclick = stopFollow;
    followT = setInterval(() => {
      const cur = following && following.src.items.find((x) => following.src.id(x) === following.id);
      if (!cur) { note("No longer reported"); stopFollow(); return; }
      if (!map.isMoving()) map.easeTo({ center: following.src.where(cur), duration: 900, easing: (t) => t });
    }, 1000);
  }
  function stopFollow() {
    clearInterval(followT);
    following = null;
    chip.hidden = true;
  }
  // Panning by hand means "stop following", as in the globe.
  map.on("dragstart", () => { if (following) stopFollow(); });

  // -- a one-line note ----------------------------------------------------------
  const toast = document.createElement("div");
  toast.className = "map-note";
  toast.hidden = true;
  map.getContainer().appendChild(toast);
  let toastT = 0;
  function note(text) {
    toast.textContent = text; toast.hidden = false;
    clearTimeout(toastT);
    toastT = setTimeout(() => { toast.hidden = true; }, 4000);
  }

  // -- my location ------------------------------------------------------------------
  const geo = new maplibregl.GeolocateControl({
    positionOptions: { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    trackUserLocation: false, showAccuracyCircle: true, fitBoundsOptions: { maxZoom: 15 },
  });
  map.addControl(geo, "top-right");
  geo.on("geolocate", (p) => note(`You are here, to within ${p.coords.accuracy < 1000
    ? `${Math.round(p.coords.accuracy)} m` : `${(p.coords.accuracy / 1000).toFixed(1)} km`}`));
  geo.on("error", (e) => note(e.code === 1
    ? "Location not allowed: allow it for this page in the browser's site settings"
    : "The browser could not find a location"));

  // -- imagery ------------------------------------------------------------------------
  const base = document.createElement("div");
  base.className = "maplibregl-ctrl maplibregl-ctrl-group basemap-ctrl";
  base.innerHTML = `<button type="button" title="Satellite imagery (Esri)" aria-pressed="false">Imagery</button>`;
  map.addControl({ onAdd: () => base, onRemove: () => base.remove() }, "top-right");
  const btn = base.querySelector("button");
  btn.onclick = () => {
    const want = btn.getAttribute("aria-pressed") !== "true";
    if (!map.getSource("imagery")) {
      map.addSource("imagery", { type: "raster", tileSize: 256, maxzoom: 19,
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
        attribution: "Imagery: Esri, Maxar, Earthstar Geographics" });
      map.addSource("imagery-labels", { type: "raster", tileSize: 256, maxzoom: 19,
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"],
        attribution: "Labels: Esri" });
      // Under every data layer: the first one the viewer adds is the archive
      // trail casing.
      const under = map.getLayer("tracks-casing") ? "tracks-casing" : undefined;
      map.addLayer({ id: "imagery", type: "raster", source: "imagery", layout: { visibility: "none" } }, under);
      map.addLayer({ id: "imagery-labels", type: "raster", source: "imagery-labels",
        layout: { visibility: "none" }, paint: { "raster-opacity": 0.85 } }, under);
    }
    for (const id of ["imagery", "imagery-labels"]) map.setLayoutProperty(id, "visibility", want ? "visible" : "none");
    btn.setAttribute("aria-pressed", String(want));
    btn.classList.toggle("on", want);
    btn.textContent = want ? "Map" : "Imagery";
    btn.title = want ? "Back to the map" : "Satellite imagery (Esri)";
  };

  return { stopFollow, note };
}
