// ANGELS front end.
//
// Layers and cadences:
//   live air    ADS-B positions, polled every 8s, dead-reckoned between polls
//   live sea    AIS positions, polled every 10s, dead-reckoned between polls
//   tracks      historical trails from the archive, refreshed rarely
//
// No framework, no build step. At this size vanilla is fine, and a toolchain
// you have to maintain is a toolchain that breaks in March.
//
// EVERYTHING ON THIS MAP IS COOPERATIVE REPORTING.
//
// Every aircraft and every vessel here is one that CHOSE to broadcast. This
// map cannot show a dark vessel and it must never be read as though it
// could. The discrepancy product -- SAR observation minus AIS report, over a
// searched-water denominator -- is retrospective by hours to days, and it
// belongs to a different view. The panel says so on every render rather than
// once in a tooltip, because the whole point of the project is that a display
// of self-reported positions looks exactly like a display of reality.

import { fetchTracks, addTracks } from "./views/map.js";
import { addLive, startLive, liveInfo, setVisible as setAirVisible, POLL_MS }
  from "./views/live.js";
import { addVessels, startVessels, vesselInfo, setVisible as setSeaVisible,
         setGroups, GROUPS, SUBTYPE_LABEL, HULL_PATH }
  from "./views/vessels.js";
import { addDiscrepancy, loadDiscrepancy, discrepancyInfo,
         setVisible as setObsVisible, coverageAt, coverageSummary }
  from "./views/discrepancy.js";

const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
const AOI_AIR = [-77.7, 38.3, -76.0, 39.6];
const AOI_SEA = [-77.2, 36.0, -71.0, 39.6];
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
const on = { air: true, sea: false };
let groupFilter = null;             // null = every vessel group
let observed = false;               // the SAR discrepancy layer
let probe = null;                   // the last /coverage answer, or null
let probeBusy = false;
let covSummary = null;

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
  const sea = err.domain === "sea";

  if (err.kind === "offline") {
    return `
      <div class="livebar off"><span class="dot"></span>API offline</div>
      <h2 class="errh">Cannot reach the API</h2>
      <p class="meta">Nothing is listening on port 8000.</p>
      <p class="meta">Start it in its own terminal:</p>
      <pre class="cmd">cd C:\\Users\\mccul\\angels-geoint
uvicorn angels.api.main:app --reload</pre>
      <p class="empty">The map keeps the contacts it already has,
      dead-reckoned forward, until they go stale.</p>`;
  }

  if (err.status === 503 && sea) {
    return `
      <div class="livebar off"><span class="dot"></span>No AIS key</div>
      <h2 class="errh">AIS stream credentials missing</h2>
      <p class="meta">The API is running, but it has no key for the AIS
      stream, so nothing is listening to the sea.</p>
      <pre class="cmd">AISSTREAM_API_KEY=your_key</pre>
      <p class="meta">Register free at
      <a href="https://aisstream.io" target="_blank" rel="noopener">aisstream.io</a>
      &mdash; email only, no review &mdash; then put the key in
      <code>.env</code> and restart uvicorn. <code>.env</code> is read at
      startup, not per request.</p>
      <p class="empty">This is a missing key, not an empty sea.</p>`;
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

  if (err.status === 429 && !sea) {
    return `
      <div class="livebar off"><span class="dot"></span>Out of credits</div>
      <h2 class="errh">OpenSky's daily credits are spent</h2>
      <p class="meta">This is not an outage and not an empty sky. OpenSky is
      answering &mdash; the answer is "no more requests today".</p>
      <p class="meta">${err.detail || ""}</p>
      <p class="meta">The collectors draw on the same 4,000 credits a day, so
      the aircraft archive is also being refused until the reset. That gap is
      recorded in the collector's uptime log.</p>
      <p class="empty">No aircraft are drawn because we cannot ask, not
      because nothing is flying.</p>`;
  }

  if (err.status === 502) {
    return `
      <div class="livebar off"><span class="dot"></span>Upstream down</div>
      <h2 class="errh">${sea ? "The AIS stream" : "OpenSky"} is not responding</h2>
      <p class="meta">Your credentials are fine and the API is running.
      The upstream feed is unreachable or rate-limiting.</p>
      <p class="meta">${err.detail || ""}</p>
      <p class="empty">Retrying automatically.${sea ? " Anything broadcast\n      while the socket is down is not recoverable \u2014 the stream has no\n      backfill." : " If this persists, check whether the session\n      request count has run past your daily credits."}</p>`;
  }

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

// --- the vessel legend ---------------------------------------------------
//
// The sea's counterpart to the altitude ramp, and it doubles as the filter:
// click a row to hide or show that group. Two blocks, because the map uses
// two kinds of encoding and a legend that explains only colour leaves the
// shape, the size and the line to be guessed at.

// Hull and course line drawn in ONE frame, rotated together, so the key
// cannot show the line leaving from the wrong end -- which is exactly what an
// earlier version did by rotating the two separately.
const underWaySvg = (color) =>
  `<svg viewBox="-40 -26 80 52" class="glyph" aria-hidden="true">
     <g transform="rotate(70)">
       <line x1="0" y1="0" x2="0" y2="-46" stroke="${color}" stroke-width="3.5"
             stroke-linecap="round" opacity=".75"/>
       <path d="${HULL_PATH}" fill="${color}" stroke="#fff" stroke-width="4"
             stroke-linejoin="round" paint-order="stroke"/>
     </g></svg>`;

const swatch = (g) => g.color
  ? `<i class="sw" style="background:${g.color}"></i>`
  : `<i class="sw hollow"></i>`;

function vesselLegend(counts, showCounts) {
  const rows = GROUPS.map((g) => {
    const n = g.subtypes.reduce((a, s) => a + (counts[s] || 0), 0);
    // Detail lives in TEXT, not in more colours. The grouped rows name what
    // they contain with their counts, so cargo and tanker are still
    // distinguishable without spending two of the three hues the map can
    // afford.
    const parts = g.subtypes.length > 1
      ? g.subtypes.filter((s) => counts[s])
          .map((s) => `${SUBTYPE_LABEL[s]} ${counts[s]}`).join(" &middot; ")
      : "";
    const active = !groupFilter || groupFilter.includes(g.key);
    return `<button class="vrow ${active ? "on" : ""}" data-group="${g.key}"
              aria-pressed="${active}">
        ${swatch(g)}
        <span class="nm">${g.label}${showCounts && parts ? `<small>${parts}</small>` : ""}</span>
        ${showCounts ? `<b>${n}</b>` : ""}
      </button>`;
  }).join("");

  return `
    <div class="legend">
      <span class="lt">Vessel type${showCounts ? " &middot; click to filter" : ""}</span>
      <div class="vrows">${rows}</div>
      <p class="lnote">Colour picks out the three groups a dark-vessel
      question turns on. Everything else is grey context.</p>
    </div>
    <div class="legend">
      <span class="lt">Symbol</span>
      <div class="vkey">
        <span class="k">${underWaySvg("#2a78d6")}</span>
        <span>Under way &mdash; points along its course; the line is where it
        will be in 6&nbsp;min</span>
        <span class="k"><i class="dot"></i></span>
        <span>Stopped or at anchor &mdash; no course is drawn, because a
        moored ship has none</span>
        <span class="k"><i class="dot s"></i><i class="dot l"></i></span>
        <span>Size is declared length, about 10&nbsp;m to 300&nbsp;m+.
        Undeclared length is drawn as 50&nbsp;m.</span>
        <span class="k"><i class="dot faded"></i></span>
        <span>Faded &mdash; not heard for 6&nbsp;min or more</span>
      </div>
    </div>`;
}

// --- the observed block ---------------------------------------------------
//
// The rule for this block: NO NUMBER WITHOUT ITS SAMPLING RATE, and no
// position without its date. A count of 1,150 candidates printed on its own
// is read as a census of dark vessels. It is a sample of unexplained returns,
// taken by a sensor that finds 93% of ships over 100 m and 4% of boats under
// 15 m, on days spread twelve days apart. The curve and the as-of line are
// therefore part of the count, not a footnote to it.

const pct = (r) => (r == null ? "—" : `${Math.round(100 * r)}%`);

function dayAge(iso) {
  if (!iso) return null;
  return (Date.now() - Date.parse(`${iso}T00:00:00Z`)) / 86400000;
}

// The detectability curve, drawn. Classes at or below the detectable length
// are marked: those are the sizes where absence means nothing.
function curveBlock(dr) {
  if (!dr || !dr.curve) return "";
  const order = ["0-15 m", "15-25 m", "25-50 m", "50-100 m", ">100 m"];
  const rows = order.filter((k) => dr.curve[k] && dr.curve[k].scored)
    .map((k) => {
      const r = dr.curve[k];
      const blind = r.rate != null && r.rate < 0.5;
      return `<div class="row">
          <span class="lab">${k}</span>
          <span class="bar ${blind ? "blind" : ""}"><i style="width:${
            Math.round(100 * (r.rate ?? 0))}%"></i></span>
          <span class="val">${pct(r.rate)} <small>n=${r.scored}</small></span>
        </div>`;
    }).join("");
  if (!rows) return "";
  const un = dr.curve.unknown;
  return `
    <div class="curve">
      <span class="lt">Sampling rate &middot; share of AIS-reporting vessels
        this detector actually found</span>
      ${rows}
    </div>
    <p class="meta">Measured on ${dr.n_passes} pass${dr.n_passes === 1 ? "" : "es"},
      in the same searched water, against vessels that reported a length.
      Red bars are sizes this sensor misses more often than it finds:
      <strong>at those lengths, nothing detected is not evidence of nothing
      there</strong>.${un && un.scored ? ` ${un.scored} vessels never declared
      a length and are binned apart, not assumed.` : ""}</p>`;
}

function observedBlock() {
  const info = discrepancyInfo();
  if (info.error) {
    const e = info.error;
    return `
      <div class="asof"><span class="dot"></span>Observed layer unavailable</div>
      <p class="meta">${e.detail || e.message}</p>
      <p class="empty">${e.status === 404
        ? "This is a missing product, not an empty sea. The layer appears once the detection and scoring scripts have run."
        : "The API answered, but not with candidates."}</p>`;
  }
  if (!info.loaded || !info.meta) {
    return `<div class="asof"><span class="dot"></span>Observed &middot; loading</div>`;
  }

  const m = info.meta;
  const age = dayAge(m.as_of);
  const rc = m.reception_counts || {};
  const withheld = ["intermittent", "thin", "unheard"]
    .reduce((a, k) => a + (rc[k] || 0), 0);
  const nSites = info.siteMeta?.n ?? null;

  return `
    <!-- Its OWN timestamp, not the map's. The live bar above says seconds;
         this says months, and both are true at once. -->
    <div class="asof">
      <span class="dot"></span>
      Observed &middot; as of ${m.as_of || "unknown"}${
        age != null ? ` &middot; ${Math.round(age)} days old` : ""}
    </div>

    <div class="stat"><span class="n">${(m.n ?? 0).toLocaleString()}</span>
      <span class="l">Unexplained returns${
        m.reception_filter === "all" ? "" : " &middot; in water AIS reaches"}</span></div>

    ${curveBlock(m.detection_rate)}

    <p class="meta">
      ${m.n_total != null && m.n_total !== m.n
        ? `${m.n.toLocaleString()} of ${m.n_total.toLocaleString()} shown. ` : ""}
      ${withheld ? `${withheld.toLocaleString()} more sit in water where AIS
        reception is intermittent, thin or absent. They are excluded rather
        than counted: there, "no report" may mean nobody was listening, which
        is not the same claim as "somebody was not reporting".` : ""}
      ${m.dates?.length ? `<br>${m.dates.length} pass date${
        m.dates.length === 1 ? "" : "s"}, ${m.dates[0]} to ${
        m.dates[m.dates.length - 1]}. Sentinel-1 revisits this water about
        every ${m.revisit_days || 12} days, so between any two of them there
        is no observation at all.` : ""}
    </p>

    ${nSites != null ? `<p class="meta">${nSites.toLocaleString()} fixed
      structures drawn in grey &mdash; turbines, platforms, bridge islands.
      They were <strong>removed</strong> from the count above; they are shown
      so the removal can be checked rather than taken on trust.</p>` : ""}

    ${m.detection_rate?.expected_chance_matches
      ? `<p class="meta">About
         ${m.detection_rate.expected_chance_matches.toFixed(1)} of the AIS
         matches behind that rate could be coincidence at these densities.</p>`
      : ""}

    <div class="legend">
      <span class="lt">Observed symbols</span>
      <div class="vkey">
        <span class="k"><i class="ring"></i></span>
        <span>Unexplained return &mdash; hollow, because a ring is a
        measurement, not a ship</span>
        <span class="k"><i class="ring faint"></i></span>
        <span>Fixed structure, already subtracted</span>
      </div>
    </div>

    <p class="empty">Click any water to ask when the radar last looked
      there.</p>

    ${probeBlock()}`;
}

// --- the click-through answer --------------------------------------------
//
// "Nothing here" on a map has two completely different meanings -- searched
// and empty, or never searched -- and they are drawn identically. This is the
// only control in the interface that can tell them apart, which is why the
// panel points at it in words rather than waiting to be discovered.

function probeBlock() {
  if (probeBusy) {
    return `<div class="probe"><span class="lt">This point</span>
      <p>asking&hellip;</p></div>`;
  }
  if (!probe) return "";
  const p = probe.last;
  const dm = `${probe.lat.toFixed(3)}, ${probe.lon.toFixed(3)}`;

  if (!p) {
    return `
      <div class="probe">
        <span class="lt">This point &middot; ${dm}</span>
        <p class="big never">Never searched</p>
        <p>${probe.note}</p>
        <p>${probe.n_passes_on_disk} pass${
          probe.n_passes_on_disk === 1 ? "" : "es"} on disk; none of them
          covered this cell. Empty water here is an untested claim, not a
          negative result.</p>
      </div>`;
  }

  const hist = (probe.history || []).slice(1);
  return `
    <div class="probe">
      <span class="lt">This point &middot; ${dm}</span>
      <p class="big">Last searched ${p.acquired.replace("T", " ").slice(0, 16)} UTC</p>
      <p>${Math.round(p.age_days)} days ago &middot; ${p.scene}<br>
        ${p.status === "searched"
          ? `this cell was examined in full (${pct(p.searched_fraction)} of it)`
          : `only ${pct(p.searched_fraction)} of this cell was examined &mdash;
             part-searched shoreline, reported but never counted`}${
        p.mask_source === "fallback"
          ? "<br>water mask from the pooled shoreline, not this scene's own contrast"
          : ""}</p>
      <p>Anything that arrived since is unobserved by construction.</p>
      ${hist.length ? `<div class="hist">${hist.map((h) =>
        `<span>${h.acquired.slice(0, 10)} &middot; ${Math.round(h.age_days)} d
          &middot; ${h.status}</span>`).join("")}</div>` : ""}
    </div>`;
}

async function askCoverage(lngLat) {
  probeBusy = true;
  renderStatus();
  try {
    probe = await coverageAt(lngLat.lng, lngLat.lat);
  } catch (e) {
    probe = null;
  } finally {
    probeBusy = false;
    renderStatus();
  }
}

// --- the sea block --------------------------------------------------------

function seaBlock() {
  const info = vesselInfo();
  const m = info.meta;
  if (!m) {
    return `<div class="stat"><span class="n">&mdash;</span>
      <span class="l">Vessels &middot; connecting</span></div>`;
  }

  // THE GUARD. A table four seconds old holds the handful of vessels that
  // happened to transmit in those four seconds, and a count drawn from it
  // reads as an empty sea. A vessel at anchor reports only every three
  // minutes, so until the stream has been listening that long the honest
  // answer is a progress bar, not a number.
  if (m.warming) {
    // Progress is measured against the DISCOVERY RATE, not a stopwatch.
    // The first real run of this box was still finding 123 new vessels a
    // minute at t=120s against a peak of 129 — a straight line, not a curve
    // flattening out. A bar filling against a fixed constant would have
    // reached 100% while the table was a fifth complete.
    const peak = m.peak_discovery_per_min || 0;
    const now = m.discovery_per_min || 0;
    const pct = peak > 0 ? Math.min(100, Math.round((1 - now / peak) * 100)) : 0;
    return `
      <div class="stat warming">
        <span class="n">${info.n.toLocaleString()}</span>
        <span class="l">heard so far &middot; still arriving</span>
      </div>
      <div class="warmbar"><i style="width:${pct}%"></i></div>
      <p class="meta">
        ${m.connected ? "Listening" : "Reconnecting"} &middot;
        ${Math.round(m.listening_s)}s &middot;
        ${m.n_messages.toLocaleString()} messages<br>
        Finding <strong>${now.toFixed(0)}</strong> new vessels/min${
          peak ? `, peaked at ${peak.toFixed(0)}` : ""}.<br>
        <strong>Not a count yet.</strong> A vessel at anchor transmits every
        three minutes, and this box was still discovering new ones after two,
        so the moored traffic is still coming in. An empty map right now
        means an empty socket, not an empty sea.
      </p>
      ${m.stream_error ? `<p class="empty">last error: ${m.stream_error}</p>` : ""}
      ${vesselLegend({}, false)}`;
  }

  const counts = m.by_subtype || {};
  const filtered = groupFilter && info.shown !== info.n;
  const untyped = info.n - (m.n_typed ?? 0);
  return `
    <div class="stat"><span class="n">${info.n}</span>
      <span class="l">Vessels reporting</span></div>
    ${filtered ? `<p class="meta">Showing ${info.shown} of ${info.n}.
      The counts below are everything heard, not everything drawn &mdash; a
      filter narrows the view, it does not remove traffic.</p>` : ""}
    ${vesselLegend(counts, true)}
    ${untyped > 0 ? `<p class="meta">
      ${untyped} of ${info.n} have not declared a type yet. Class B sends
      its type in a separate half-message, less often, so this falls slowly
      &mdash; undeclared is an absence of information, not a category.</p>` : ""}
    ${m.discovery_per_min >= 5 ? `<p class="empty">
      Still finding ${m.discovery_per_min.toFixed(0)} new vessels/min
      (peak ${m.peak_discovery_per_min.toFixed(0)}). Past the knee, not
      complete &mdash; treat ${info.n} as a floor.</p>` : ""}`;
}

// Errors are kept PER DOMAIN. The first version kept one, so an OpenSky
// credential error went on blanking the panel after Air was switched off,
// hiding a healthy sea feed behind a message about aircraft. A failure is now
// shown full-page only when EVERY domain that is switched on has failed;
// otherwise the working domain renders and the failing one gets a line.
const errors = { air: null, sea: null };

function renderStatus() {
  const active = Object.keys(on).filter((d) => on[d]);
  const failing = active.filter((d) => errors[d]);
  // A dead live feed must not blank the observed layer. They are independent
  // evidence from independent sources: OpenSky being out of credits says
  // nothing about what Sentinel-1 saw in June, and a full-page "cannot reach
  // the API" over a working archive would be the panel lying about what it
  // knows.
  if (active.length && failing.length === active.length && !observed) {
    el("panel").innerHTML = errorPanel(errors[failing[0]]);
    return;
  }
  const air = liveInfo();
  const sea = vesselInfo();
  const ageMs = on.air && air.lastPoll ? Date.now() - air.lastPoll
              : on.sea && sea.lastPoll ? Date.now() - sea.lastPoll
              : null;
  const fresh = ageMs !== null && ageMs < 20000;
  const verts = trails?.features.reduce((a, f) => a + f.properties.n_vertices, 0) ?? 0;

  el("panel").innerHTML = `
    <div class="livebar ${fresh ? "on" : "off"}">
      <span class="dot"></span>
      ${failing.length === active.length && active.length
        ? "Live feeds down &mdash; the observed layer below is unaffected"
        : ageMs === null ? "Connecting…" : `Live &middot; ${relTime(ageMs)}`}
    </div>
    ${failing.map((d) => `<p class="err">${d === "air" ? "Air" : "Sea"} feed
      failed: ${errors[d].detail || errors[d].message || "unreachable"}.
      Showing the other domain; switch it off and back on to see details.</p>`).join("")}

    ${on.air && live ? `<p class="meta">${live.properties.source === "collector"
      ? `Aircraft via the collector &middot; 0 credits`
      : `Aircraft direct from OpenSky &middot; 1 credit per ${live.properties.cache_s}s
         &mdash; the collector is not running or its feed is stale`}${
      live.properties.credits_remaining != null
        ? ` &middot; ${Math.round(live.properties.credits_remaining).toLocaleString()} credits left today`
        : ""}</p>` : ""}
    ${on.air ? `<div class="stat"><span class="n">${live?.properties.n ?? "—"}</span>
      <span class="l">Aircraft airborne</span></div>` : ""}

    ${on.sea ? seaBlock() : ""}

    ${observed ? observedBlock() : ""}

    ${on.air ? `<div class="stat"><span class="n">${trails?.properties.n_tracks ?? "—"}</span>
      <span class="l">Trails &middot; last ${trailHours}h</span></div>` : ""}

    <p class="meta">
      ${on.air && verts ? verts.toLocaleString() + " track vertices<br>" : ""}
      ${[on.air ? `air every ${Math.round(POLL_MS / 1000)}s` : "",
         on.sea ? "sea every 10s" : ""].filter(Boolean).join(" &middot; ")}<br>
      ${air.requests + sea.requests} request${air.requests + sea.requests === 1 ? "" : "s"} this session &middot;
      positions dead-reckoned between polls
    </p>

    ${on.air ? `<div class="legend">
      <span class="lt">Altitude</span>
      <div class="ramp"></div>
      <div class="ends"><span>Ground</span><span>40,000 ft</span></div>
    </div>` : ""}

    <!-- Printed on every render, not tucked into an about box. A map of
         self-reported positions is visually indistinguishable from a map of
         reality, and that confusion is the thing this project exists to
         take apart. -->
    <div class="evidence">
      <span class="lt">Evidence class${observed ? "es \u00b7 two, mixed" : ""}</span>
      <p>${observed ? "<strong>Cooperative</strong> \u2014 filled symbols. " : ""}Cooperative reporting${
        observed ? ":" : " only."} everything ${observed ? "filled" : "here"} chose to
        broadcast, current to seconds.</p>
      ${observed ? `<p><strong>Observed</strong> &mdash; hollow rings.
        Sentinel-1 radar returns no AIS report explained, from passes
        <em>${covSummary?.newest_age_days != null
          ? Math.round(covSummary.newest_age_days) + " to "
            + Math.round(covSummary.oldest_age_days ?? covSummary.newest_age_days)
            + " days old"
          : "hours to days old"}</em>, about
        ${covSummary?.revisit_days ?? 12} days apart. The two layers are on
        one map and are <em>not</em> one picture: a ring beside no ship is a
        discrepancy from a past moment, not a vessel that is there now.</p>
      <p>A ring is a candidate, not a confirmed dark vessel. It carries the
        detector's residual false-alarm rate and the sampling rate above.</p>`
      : `<p>${on.sea
        ? "Independent observation of vessels exists (Sentinel-1 SAR) but is retrospective \u2014 hours to days late, twelve days between revisits. A vessel missing from this map is <em>not</em> a dark vessel."
        : "No independent observation channel for aircraft is available to civilians; six sources were tested and none returned MLAT. Anomalies in this domain are found inside the cooperative record, not by subtracting an observation from it."}</p>`}
      ${observed ? "" : `<p>Switch on <em>Observed &middot; SAR</em> to see
        what independent observation found, and to ask any point when it was
        last looked at.</p>`}
    </div>

    ${on.air && trails && !trails.features.length ? `<p class="empty">
      No trails yet. That layer reads the archive<br>
      that <code>ingest_aviation.py</code> writes.</p>` : ""}

    <p class="empty">Hover a contact for detail.<br>Zoom in for labels.</p>`;

  el("panel").querySelectorAll(".vrow").forEach((b) => {
    b.onclick = () => toggleGroup(b.dataset.group);
  });
}

// --- filters --------------------------------------------------------------

function toggleGroup(key) {
  const all = GROUPS.map((g) => g.key);
  let next = groupFilter ? [...groupFilter] : [...all];
  next = next.includes(key) ? next.filter((k) => k !== key) : [...next, key];
  // Deselecting the last one means "show everything" rather than "show
  // nothing". An empty map is a claim, and it should take more than one
  // stray click to make it.
  groupFilter = (next.length === 0 || next.length === all.length) ? null : next;
  setGroups(map, groupFilter);
  renderStatus();
}

function setDomain(name, want) {
  on[name] = want;
  if (name === "air") setAirVisible(map, want);
  if (name === "sea") {
    setSeaVisible(map, want);
    // Fit to whichever domain was just switched on, once, so turning the sea
    // on does not leave the user staring at Baltimore while four hundred
    // vessels sit off Virginia.
    if (want) map.fitBounds(AOI_SEA, { padding: 40, duration: 900 });
    else if (on.air) map.fitBounds(AOI_AIR, { padding: 40, duration: 900 });
  }
  for (const id of ["tracks-casing", "tracks-line"]) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", on.air ? "visible" : "none");
    }
  }
  renderStatus();
}

// --- boot -----------------------------------------------------------------

let live = null, trails = null;

async function loadTrails() {
  if (!on.air) return;
  try {
    trails = await fetchTracks({ hours: trailHours });
    addTracks(map, trails);
  } catch { /* trails are optional context; live is the point */ }
  renderStatus();
}

async function setObserved(want) {
  observed = want;
  setObsVisible(map, want);
  if (!want) { probe = null; renderStatus(); return; }
  map.fitBounds(AOI_SEA, { padding: 40, duration: 900 });
  renderStatus();                       // show "loading" before the wait
  if (!discrepancyInfo().loaded) {
    covSummary = covSummary || await coverageSummary();
    await loadDiscrepancy(map);
  }
  renderStatus();
}

map.on("load", async () => {
  addTracks(map, { type: "FeatureCollection", features: [] });
  addDiscrepancy(map);   // under the live layers: a past return must never
                         // draw over a vessel reporting now
  addLive(map);       // added after tracks so aircraft draw above the trails
  addVessels(map);
  setSeaVisible(map, false);

  // The probe is armed only while the observed layer is on. Clicking water on
  // a purely cooperative map and being told about radar coverage would be an
  // answer to a question the view was not asking.
  map.on("click", (e) => {
    if (!observed) return;
    askCoverage(e.lngLat);
  });

  startLive(map,
    (data) => { errors.air = null; live = data; renderStatus(); },
    (err)  => { errors.air = err; renderStatus(); });

  startVessels(map,
    () => { errors.sea = null; renderStatus(); },
    (err)  => { errors.sea = err; renderStatus(); });

  loadTrails();
  setInterval(() => loadTrails(), TRAIL_REFRESH_MS);
  setInterval(() => renderStatus(), 1000);

  document.querySelectorAll("#domains button").forEach((b) => {
    if (b.disabled) return;
    b.onclick = () => {
      const want = !b.classList.contains("on");
      // At least one domain stays on. A map with nothing on it is not a
      // filter state anyone means to reach.
      if (!want && Object.values(on).filter(Boolean).length === 1) return;
      b.classList.toggle("on", want);
      setDomain(b.dataset.domain, want);
    };
  });

  document.querySelectorAll("#layers button").forEach((b) => {
    b.onclick = () => {
      const want = !b.classList.contains("on");
      b.classList.toggle("on", want);
      setObserved(want);
    };
  });

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
