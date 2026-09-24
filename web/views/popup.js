// Hover cards, shared by both live domains.
//
// WHY THIS IS ITS OWN MODULE AND NOT TWO STRINGS OF HTML
//
// The hover card is where a viewer stops looking at a pattern and starts
// asking about ONE contact, which makes it the only place in the interface
// that can answer "how do you know that". Both domains have to answer it the
// same way or the cross-domain comparison is being made across two different
// standards of evidence -- so the structure lives here, once, and each
// domain supplies its own rows.
//
// Every card carries, in the same order:
//
//   the identity it BROADCAST       not the identity we assigned it
//   where it is and what it is doing
//   how old that is                 the field most often missing from
//                                   displays like this, and the one that
//                                   decides what the position is worth
//   the evidence class              cooperative, always, on this map
//
// The last line is not boilerplate. A hover card with a callsign, a heading
// and a squawk looks exactly like a radar readout, and a radar readout
// implies something independent saw the aircraft. Nothing here did.

// --- formatting -----------------------------------------------------------

export const esc = (v) => String(v ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

export function compass(deg) {
  if (deg == null || Number.isNaN(Number(deg))) return null;
  const d = ((Number(deg) % 360) + 360) % 360;
  return `${Math.round(d).toString().padStart(3, "0")}° ${POINTS[Math.round(d / 22.5) % 16]}`;
}

// Ages are read at a glance and compared against a cadence, so seconds stay
// seconds well past the point where a clock would switch to minutes.
export function age(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return null;
  const s = Math.max(0, Math.round(Number(seconds)));
  if (s < 90) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

export const km = (m) => (m == null ? null
  : m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`);

// --- squawk ---------------------------------------------------------------
//
// The one field on an aircraft that can carry an emergency, and the reason a
// hover card is worth building at all. 7500/7600/7700 are set by the crew and
// mean something specific; everything else is routing. A display that shows
// "7700" as four grey digits has thrown away the only urgent thing it knows.

const SQUAWK = {
  "7500": ["hijack", true],
  "7600": ["radio failure", true],
  "7700": ["general emergency", true],
  "7777": ["military interceptor — do not intercept", true],
  "1200": ["VFR, uncontrolled (US)", false],
  "1201": ["VFR near a glider port (US)", false],
  "1202": ["VFR glider, no transponder-required airspace (US)", false],
  "1255": ["firefighting aircraft (US)", false],
  "1277": ["search and rescue (US)", false],
  "0000": ["unassigned / transponder fault", false],
  "2000": ["entered without a code assigned", false],
  "4000": ["special operations (US)", false],
};

export function squawk(code) {
  if (!code) return null;
  const hit = SQUAWK[String(code)];
  return hit ? { code, meaning: hit[0], urgent: hit[1] }
             : { code, meaning: null, urgent: false };
}

// --- MMSI -> flag ---------------------------------------------------------
//
// The first three digits of an MMSI are the Maritime Identification Digits,
// which name the administration that issued it. That is not the same as
// where the vessel is registered and definitely not where it is going, but
// it is the only provenance AIS carries, and it is broadcast rather than
// inferred -- which is the standard everything on this map is held to.
//
// Abridged to the administrations that actually appear off the US coast,
// plus the flags of convenience that dominate commercial shipping. An MID
// that is not here is reported as its digits rather than guessed at.

const MID = {
  201: "Albania", 202: "Andorra", 203: "Austria", 204: "Azores",
  205: "Belgium", 206: "Belarus", 207: "Bulgaria", 208: "Vatican",
  209: "Cyprus", 210: "Cyprus", 211: "Germany", 212: "Cyprus",
  213: "Georgia", 214: "Moldova", 215: "Malta", 218: "Germany",
  219: "Denmark", 220: "Denmark", 224: "Spain", 225: "Spain",
  226: "France", 227: "France", 228: "France", 230: "Finland",
  231: "Faroe Is.", 232: "UK", 233: "UK", 234: "UK", 235: "UK",
  236: "Gibraltar", 237: "Greece", 238: "Croatia", 239: "Greece",
  240: "Greece", 241: "Greece", 242: "Morocco", 244: "Netherlands",
  245: "Netherlands", 246: "Netherlands", 247: "Italy", 248: "Malta",
  249: "Malta", 250: "Ireland", 251: "Iceland", 252: "Liechtenstein",
  253: "Luxembourg", 254: "Monaco", 255: "Madeira", 256: "Malta",
  257: "Norway", 258: "Norway", 259: "Norway", 261: "Poland",
  262: "Montenegro", 263: "Portugal", 264: "Romania", 265: "Sweden",
  266: "Sweden", 267: "Slovakia", 268: "San Marino", 269: "Switzerland",
  270: "Czechia", 271: "Türkiye", 272: "Ukraine", 273: "Russia",
  274: "N. Macedonia", 275: "Latvia", 276: "Estonia", 277: "Lithuania",
  278: "Slovenia", 279: "Serbia",
  301: "Anguilla", 303: "Alaska (US)", 304: "Antigua & Barbuda",
  305: "Antigua & Barbuda", 306: "Curaçao", 307: "Aruba",
  308: "Bahamas", 309: "Bahamas", 310: "Bermuda", 311: "Bahamas",
  312: "Belize", 314: "Barbados", 316: "Canada", 319: "Cayman Is.",
  321: "Costa Rica", 323: "Cuba", 325: "Dominica",
  327: "Dominican Rep.", 329: "Guadeloupe", 330: "Grenada",
  331: "Greenland", 332: "Guatemala", 334: "Honduras", 336: "Haiti",
  338: "USA", 339: "Jamaica", 341: "St Kitts & Nevis",
  343: "St Lucia", 345: "Mexico", 347: "Martinique", 348: "Montserrat",
  350: "Nicaragua", 351: "Panama", 352: "Panama", 353: "Panama",
  354: "Panama", 355: "Panama", 356: "Panama", 357: "Panama",
  358: "Puerto Rico (US)", 359: "El Salvador", 361: "St Pierre",
  362: "Trinidad & Tobago", 364: "Turks & Caicos", 366: "USA",
  367: "USA", 368: "USA", 369: "USA", 370: "Panama", 371: "Panama",
  372: "Panama", 373: "Panama", 374: "Panama", 375: "St Vincent",
  376: "St Vincent", 377: "St Vincent", 378: "British Virgin Is.",
  379: "US Virgin Is.",
  401: "Afghanistan", 412: "China", 413: "China", 414: "China",
  416: "Taiwan", 419: "India", 422: "Iran", 423: "Azerbaijan",
  431: "Japan", 432: "Japan", 434: "Turkmenistan", 436: "Kazakhstan",
  437: "Uzbekistan", 438: "Jordan", 440: "South Korea",
  441: "South Korea", 445: "North Korea", 447: "Kuwait",
  450: "Lebanon", 451: "Kyrgyzstan", 453: "Macao", 455: "Maldives",
  457: "Mongolia", 459: "Nepal", 461: "Oman", 463: "Pakistan",
  466: "Qatar", 468: "Syria", 470: "UAE", 471: "UAE",
  473: "Yemen", 475: "Yemen", 477: "Hong Kong", 478: "Bosnia",
  501: "Adélie Land", 503: "Australia", 506: "Myanmar",
  508: "Brunei", 510: "Micronesia", 511: "Palau", 512: "New Zealand",
  514: "Cambodia", 515: "Cambodia", 516: "Christmas I.",
  518: "Cook Is.", 520: "Fiji", 523: "Cocos Is.", 525: "Indonesia",
  529: "Kiribati", 531: "Laos", 533: "Malaysia", 536: "N. Mariana Is.",
  538: "Marshall Is.", 540: "New Caledonia", 542: "Niue",
  544: "Nauru", 546: "French Polynesia", 548: "Philippines",
  553: "Papua New Guinea", 555: "Pitcairn", 557: "Solomon Is.",
  559: "American Samoa", 561: "Samoa", 563: "Singapore",
  564: "Singapore", 565: "Singapore", 566: "Singapore",
  567: "Thailand", 570: "Tonga", 572: "Tuvalu", 574: "Vietnam",
  576: "Vanuatu", 577: "Vanuatu", 578: "Wallis & Futuna",
  601: "South Africa", 603: "Angola", 605: "Algeria", 607: "Kerguelen",
  608: "Ascension", 609: "Burundi", 610: "Benin", 611: "Botswana",
  612: "C.A.R.", 613: "Cameroon", 615: "Congo", 616: "Comoros",
  617: "Cabo Verde", 618: "Crozet", 619: "Côte d'Ivoire",
  620: "Comoros", 621: "Djibouti", 622: "Egypt", 624: "Ethiopia",
  625: "Eritrea", 626: "Gabon", 627: "Ghana", 629: "Gambia",
  630: "Guinea-Bissau", 631: "Equatorial Guinea", 632: "Guinea",
  633: "Burkina Faso", 634: "Kenya", 635: "Kerguelen", 636: "Liberia",
  637: "Liberia", 638: "South Sudan", 642: "Libya", 644: "Lesotho",
  645: "Mauritius", 647: "Madagascar", 649: "Mali", 650: "Mozambique",
  654: "Mauritania", 655: "Malawi", 656: "Niger", 657: "Nigeria",
  659: "Namibia", 660: "Réunion", 661: "Rwanda", 662: "Sudan",
  663: "Senegal", 664: "Seychelles", 665: "St Helena",
  666: "Somalia", 667: "Sierra Leone", 668: "São Tomé",
  669: "Eswatini", 670: "Chad", 671: "Togo", 672: "Tunisia",
  674: "Tanzania", 675: "Uganda", 676: "DR Congo", 677: "Tanzania",
  678: "Zambia", 679: "Zimbabwe",
  701: "Argentina", 710: "Brazil", 720: "Bolivia", 725: "Chile",
  730: "Colombia", 735: "Ecuador", 740: "Falkland Is.",
  745: "Fr. Guiana", 750: "Guyana", 755: "Paraguay", 760: "Peru",
  765: "Suriname", 770: "Uruguay", 775: "Venezuela",
};

// MMSIs that are not a ship at all. Reading one as a vessel that has gone
// quiet would be a false positive of exactly the kind this project is built
// to avoid, so they are named.
export function mmsiKind(mmsi) {
  const s = String(mmsi ?? "");
  if (s.startsWith("00")) return { kind: "coast station", mid: s.slice(2, 5) };
  if (s.startsWith("0")) return { kind: "coast station group", mid: s.slice(1, 4) };
  if (s.startsWith("111")) return { kind: "SAR aircraft", mid: s.slice(3, 6) };
  if (s.startsWith("99")) return { kind: "navigation aid", mid: s.slice(2, 5) };
  if (s.startsWith("98")) return { kind: "craft associated with a parent ship", mid: s.slice(2, 5) };
  if (s.startsWith("970")) return { kind: "AIS-SART (distress beacon)", mid: null };
  if (s.startsWith("972")) return { kind: "man-overboard beacon", mid: null };
  if (s.startsWith("974")) return { kind: "EPIRB-AIS", mid: null };
  return { kind: "ship", mid: s.slice(0, 3) };
}

export function flagOf(mmsi) {
  const { mid } = mmsiKind(mmsi);
  if (!mid) return null;
  return MID[Number(mid)] ?? null;
}

// --- the card -------------------------------------------------------------

/** rows: [label, value, {strong, warn}] — a null value drops the row, so a
 *  caller never has to write a conditional per field. */
export function card({ title, id, sub, chip, age: ageText, ageState,
                       rows = [], foot = [], evidence, hint }) {
  // TWO TYPEFACES, AND THE SPLIT IS THE POINT.
  //
  // Everything the contact BROADCAST is set in mono: identifiers, codes,
  // numbers with units. Everything WE SAY ABOUT IT -- the notes and the
  // evidence line -- is set in the interface's sans. The card used to be
  // mono throughout, which made three paragraphs of English prose look like
  // more telemetry, and made our commentary look like something the vessel
  // had transmitted. On a map whose whole argument is about who said what,
  // that is not a cosmetic problem.
  const body = rows.filter((r) => r && r[1] != null && r[1] !== "")
    .map(([label, value, opt = {}]) => `
      <div class="pr${opt.alarm ? " alarm" : opt.warn ? " warn" : ""}">
        <span class="pl">${esc(label)}</span>
        <span class="pv${opt.strong ? " strong" : ""}">${value}</span>
      </div>`).join("");

  const notes = foot.filter(Boolean);

  // A hover card follows the cursor and covers the map, so its height is a
  // cost paid on every contact. The age moves into the header because it is
  // the field that decides what every other field is worth -- it was row
  // six of seven, read last if at all.
  return `
    <div class="pcard"${chip ? ` style="--chip:${esc(chip)}"` : ""}>
      <div class="ph">
        <strong>${esc(title)}</strong>
        ${ageText ? `<span class="page ${esc(ageState || "")}">${esc(ageText)}</span>` : ""}
      </div>
      ${(sub || id) ? `<div class="psub">${sub || esc(id)}</div>` : ""}
      ${body}
      ${notes.length ? `<div class="pn">${notes.map((f) => `<p>${f}</p>`).join("")}</div>` : ""}
      ${evidence ? `<p class="pe">${evidence}</p>` : ""}
      ${hint ? `<p class="pk">${esc(hint)}</p>` : ""}
    </div>`;
}

// How fresh is fresh depends on the feed, so the caller decides the
// thresholds and this only names the band. Three bands, because two would
// put "a minute late" and "gone" in the same bucket and the difference
// between them is the entire question.
export function freshness(seconds, fresh, aging) {
  if (seconds == null || Number.isNaN(Number(seconds))) return "";
  const s = Number(seconds);
  return s <= fresh ? "fresh" : s <= aging ? "aging" : "stale";
}

// The age, minus the " ago" -- a pill in a header has no room for it and
// the header already reads as "now, minus this".
export const ageShort = (seconds) => {
  const a = age(seconds);
  return a ? a.replace(" ago", "") : null;
};


// ==========================================================================
// HOVERING FOR DETAIL
// ==========================================================================
//
// The card is only worth as much as the act of getting to it, and that act
// was doing six things wrong. Both domains had written it separately, which
// is how both ended up with the same six.
//
//   1. `mouseenter` fires ONCE, when the cursor crosses into the layer. Move
//      across ten vessels in a cluster without leaving the layer and the
//      card still describes the first one. That is not a rough edge, it is
//      a card confidently describing the wrong contact.
//   2. The popup was placed at `e.lngLat` -- the CURSOR -- so its tip
//      pointed at open water beside the contact, and a dead-reckoned
//      aircraft slid out from under its own card.
//   3. The hit target was the drawn symbol. At national zoom a hull is
//      three pixels, and hovering it is a game rather than an interaction.
//      map.js has had a fat invisible `tracks-hit` line since the archive
//      view was built; the live layers never got the same treatment.
//   4. `e.features[0]` is an arbitrary pick among everything under the
//      cursor, with nothing said about the rest. At CONUS zoom, overlapping
//      contacts are the normal case, so the card was quietly choosing one
//      of several and presenting it as THE contact there.
//   5. Hover-only, on a card that is now four hundred pixels of real
//      reading. Move toward it to read the notes and it disappears --
//      the information was reachable but not readable.
//   6. `mouseleave` on one layer fired while the cursor was still over
//      another's feature, so the card flickered between the halo and the
//      symbol drawn on top of it.
//
// All six live here now, once, for every layer on the map. Both domains are
// meant to be compared, and an interaction that behaves differently between
// them is another way of quietly holding them to different standards.

const PIN_HINT = "Click to keep this open";

/** Wire hover + click-to-pin for one set of layers.
 *
 *  html(properties, { others }) builds the card. `others` is how many OTHER
 *  distinct contacts are under the same cursor position, so the card can say
 *  so rather than pretending it was the only one.
 */
export function attachHover(map, { layers, html, key, cursor = "pointer" }) {
  const hover = new maplibregl.Popup({
    closeButton: false, closeOnClick: false,
    className: "track-popup", maxWidth: "none",
  });
  // A SEPARATE INSTANCE, because closeButton is fixed at construction and a
  // pinned card needs one. Two popups also means pinning does not fight the
  // hover popup for the same DOM node.
  const pinned = new maplibregl.Popup({
    closeButton: true, closeOnClick: false,
    className: "track-popup pinned", maxWidth: "none",
  });

  let shownKey = null;          // what the hover card is currently describing
  let isPinned = false;

  const at = (f, e) => {
    // The FEATURE's position, not the cursor's. A tip pointing at empty
    // water next to the contact is a tip pointing at the wrong thing.
    const c = f.geometry?.coordinates;
    return (Array.isArray(c) && c.length === 2 && Number.isFinite(c[0]))
      ? c : e.lngLat;
  };

  const render = (e) => {
    const here = map.queryRenderedFeatures(e.point, { layers })
      .filter((f) => f && f.properties);
    if (!here.length) return null;

    // Distinct contacts, not distinct RENDERED features: one aircraft draws
    // a halo, a symbol and a label, and counting those as three contacts
    // would turn every single plane into a crowd.
    const seen = new Set();
    for (const f of here) {
      const k = key ? key(f.properties) : JSON.stringify(f.properties);
      if (k != null) seen.add(k);
    }
    const top = here[0];
    return { f: top, layer: top.layer?.id,
             k: key ? key(top.properties) : null,
             others: Math.max(0, seen.size - 1) };
  };

  const move = (e) => {
    map.getCanvas().style.cursor = cursor;
    if (isPinned) return;
    const hit = render(e);
    if (!hit) return;
    // Rebuilding identical HTML sixty times a second would defeat the
    // throttling everywhere else; only the contact CHANGING is a new card.
    if (hit.k != null && hit.k === shownKey) { hover.setLngLat(at(hit.f, e)); return; }
    shownKey = hit.k;
    hover.setLngLat(at(hit.f, e))
         .setHTML(html(hit.f.properties, { others: hit.others, layer: hit.layer,
                                           pinnable: true }))
         .addTo(map);
  };

  const leave = (e) => {
    // LEAVING ONE LAYER IS NOT LEAVING THE CONTACT.
    //
    // mouseleave is per-layer, and these layers deliberately overlap: the
    // invisible hit circle is wider than the symbol drawn inside it. So
    // moving from the symbol onto its own hit circle fires mouseleave for
    // the symbol while the cursor has not gone anywhere, and hiding there
    // makes the card vanish the moment you stop aiming dead centre --
    // which is the opposite of what a bigger target was for.
    //
    // Measured, not reasoned about: dispatching a mousemove 17px from an
    // aircraft showed the card appear at 0px and disappear at 17px, inside
    // a hit radius of 18. The first version of this function had the
    // failure it carried a comment about avoiding.
    if (e && e.point && map.queryRenderedFeatures(e.point, { layers }).length) return;
    map.getCanvas().style.cursor = "";
    if (!isPinned) { shownKey = null; hover.remove(); }
  };

  // A CLICK FIRES pin() ONCE PER LAYER UNDER THE CURSOR -- three times, for
  // the hit circle, the symbol and the halo. That is harmless in itself, but
  // Popup.addTo removes an already-added popup before re-adding it, and the
  // removal emits "close". So the second pin of the same click un-pinned the
  // card while leaving it on screen: `isPinned` said false, hovering added a
  // second card beside the pinned one, and Escape did nothing.
  //
  // Found by driving it synthetically rather than by eye. On screen it looked
  // like it worked -- the pinned card was right there.
  let closingToReopen = false;

  const pin = (e) => {
    const hit = render(e);
    if (!hit) return;
    hover.remove();
    shownKey = null;
    closingToReopen = true;
    pinned.setLngLat(at(hit.f, e))
          .setHTML(html(hit.f.properties, { others: hit.others, layer: hit.layer,
                                            pinnable: false }))
          .addTo(map);
    closingToReopen = false;
    isPinned = true;
  };

  // Only a close the USER asked for un-pins. addTo's internal remove does not.
  pinned.on("close", () => { if (!closingToReopen) isPinned = false; });

  for (const id of layers) {
    map.on("mousemove", id, move);
    map.on("mouseleave", id, leave);
    map.on("click", id, pin);
  }

  // Escape closes a pinned card without hunting for its ×.
  //
  // On WINDOW, not on the canvas: a <canvas> is not focusable by default, so
  // a listener there receives a keydown only in the case where the user has
  // already clicked the map AND the browser happened to focus it -- which
  // is to say, unreliably and invisibly.
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && isPinned) { pinned.remove(); isPinned = false; }
  });

  return { unpin: () => { pinned.remove(); isPinned = false; }, isPinned: () => isPinned };
}

/** The line a card shows when it was one of several under the cursor. */
export function crowd(others) {
  if (!others) return null;
  return `<b>${others} other contact${others === 1 ? "" : "s"}</b> under the
          cursor here. This card describes the one drawn on top \u2014 zoom
          in to separate them.`;
}

/** The footer that says the card can be kept. Only on the hover card:
 *  printing it on a pinned one would be telling you to do what you did. */
export const pinHint = (pinnable) => (pinnable ? PIN_HINT : null);
