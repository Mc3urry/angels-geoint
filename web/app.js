// ANGELS front end.
//
// Two layers, two cadences:
//   live   -- current positions, polled every 15s, dead-reckoned between polls
//   tracks -- historical trails from the archive, refreshed rarely
//
// No framework, no build step. At this size vanilla is fine, and a toolchain
// you have to maintain is a toolchain that breaks in March.

import { fetchTracks, addTracks } from "./views/map.js";
import { addLive, startLive, liveInfo, POLL_MS } from "./views/live.js";

// Same origin when uvicorn serves this page (port 8000), absolute when
// something else does -- so the old two-server setup keeps working.
const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
const AOI_AIR = [-77.7, 38.3, -76.0, 39.6];
const TRAIL_REFRESH_MS = 45000;

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/positron",
  bounds: AOI_AIR,
  fitBoundsOptions: { padding: 40 },
});
map.addControl(new maplibregl.NavigationControl(), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "nautical" }));

const el = (id) => document.getElementById(id);
let trailHours = 2;

// "43s ago" stops being useful the moment it passes a minute, and nobody
// parses "412s ago" without doing arithmetic.
function relTime(ms) {
  if (ms === null) return null;
  const s = Math.round(ms / 1000);
  if (s < 5) return "Just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m === 1) return "A minute ago";
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h ${rem}m ago` : `${h}h ago`;
}

// Every failure gets its own page, because the fixes are completely
// different and "/live 503" tells you none of them.
function errorPanel(err) {
  if (err.kind === "offline") {
    return `
      <div class="livebar off"><span class="dot"></span>API offline</div>
      <h2 class="errh">Cannot reach the API</h2>
      <p class="meta">Nothing is listening on port 8000.</p>
      <p class="meta">Start it in its own terminal:</p>
      <pre class="cmd">cd C:\Users\mccul\angels-geoint
uvicorn angels.api.main:app --reload</pre>
      <p class="empty">The map keeps flying the aircraft it already has,
      dead-reckoned forward, until they go stale.</p>`;
  }

  if (err.status === 503) {
    return `
      <div class="livebar off"><span class="dot"></span>No credentials</div>
      <h2 class="errh">OpenSky credentials missing</h2>
      <p class="meta">The API is running, but it has nothing to authenticate with,
      so it cannot ask OpenSky where anything is.</p>
      <p class="meta">Put both values in <code>.env</code> at the repo root:</p>
      <pre class="cmd">OPENSKY_CLIENT_ID=your_client_id
OPENSKY_CLIENT_SECRET=your_client_secret</pre>
      <p class="meta">Create an API client at
      <a href="https://opensky-network.org/my-opensky/account" target="_blank"
         rel="noopener">opensky-network.org</a>, then restart uvicorn &mdash;
      <code>.env</code> is read at startup, not per request.</p>
      <p class="empty">Trails still work without credentials. They read the
      archive on disk, not the live API.</p>`;
  }

  if (err.status === 502) {
    return `
      <div class="livebar off"><span class="dot"></span>Upstream down</div>
      <h2 class="errh">OpenSky is not responding</h2>
      <p class="meta">Your credentials are fine and the API is running.
      OpenSky itself is unreachable or rate-limiting.</p>
      <p class="meta">${err.detail || ""}</p>
      <p class="empty">Retrying automatically. If this persists, check whether
      the session request count has run past your daily credits.</p>`;
  }

  // Belt and braces: an error that reaches here unshaped must still say
  // something useful rather than rendering the word "undefined".
  const what = [err.status ? `HTTP ${err.status}` : null,
                err.detail || err.message || null]
               .filter(Boolean).join(" &middot; ") || "Unknown failure.";
  return `
    <div class="livebar off"><span class="dot"></span>Error</div>
    <h2 class="errh">Live feed failed</h2>
    <p class="meta">${what}</p>
    <p class="empty">Retrying automatically.
    Check the uvicorn terminal for the server-side reason.</p>`;
}

function renderStatus({ live, trails, error }) {
  if (error) {
    el("panel").innerHTML = errorPanel(error);
    return;
  }
  const info = liveInfo();
  const ageMs = info.lastPoll ? Date.now() - info.lastPoll : null;
  const fresh = ageMs !== null && ageMs < 20000;
  const verts = trails?.features.reduce((a, f) => a + f.properties.n_vertices, 0) ?? 0;

  el("panel").innerHTML = `
    <div class="livebar ${fresh ? "on" : "off"}">
      <span class="dot"></span>
      ${ageMs === null ? "Connecting…" : `Live &middot; ${relTime(ageMs)}`}
    </div>

    <div class="stat"><span class="n">${live?.properties.n ?? "—"}</span>
      <span class="l">Aircraft airborne</span></div>

    <div class="stat"><span class="n">${trails?.properties.n_tracks ?? "—"}</span>
      <span class="l">Trails &middot; last ${trailHours}h</span></div>

    <p class="meta">
      ${verts ? verts.toLocaleString() + " track vertices<br>" : ""}
      Polling every ${Math.round(POLL_MS / 1000)}s &middot; ${info.requests} request${info.requests === 1 ? "" : "s"} this session<br>
      Positions dead-reckoned between polls
    </p>

    ${trails && !trails.features.length ? `<p class="empty">
      No trails yet. That layer reads the archive<br>
      that <code>ingest_aviation.py</code> writes.</p>` : ""}

    <div class="legend">
      <span class="lt">Altitude</span>
      <div class="ramp"></div>
      <div class="ends"><span>Ground</span><span>40,000 ft</span></div>
    </div>
    <p class="empty">Hover an aircraft for detail.<br>Zoom in for callsigns.</p>`;
}

let live = null, trails = null, lastError = null;

async function loadTrails() {
  try {
    trails = await fetchTracks({ hours: trailHours });
    addTracks(map, trails);
  } catch { /* trails are optional context; live is the point */ }
  renderStatus({ live, trails });
}

map.on("load", async () => {
  addTracks(map, { type: "FeatureCollection", features: [] });
  addLive(map);   // added after tracks so planes draw above the trails

  startLive(map,
    (data) => { lastError = null; live = data; renderStatus({ live, trails }); },
    (err)  => { lastError = err;  renderStatus({ error: err }); });

  loadTrails();
  setInterval(() => loadTrails(), TRAIL_REFRESH_MS);
  // Keep the relative timestamp honest, but never clobber a live error --
  // it persists until a poll succeeds and clears it.
  setInterval(() => renderStatus({ live, trails, error: lastError }), 1000);

  document.querySelectorAll("#range button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#range button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      trailHours = Number(b.dataset.hours);
      loadTrails();
    };
  });

  try {
    const h = await (await fetch(`${API}/health`)).json();
    console.log("[angels]", h.region, h.bbox);
  } catch { /* renderStatus already reports this */ }
});
