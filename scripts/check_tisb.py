import collections, json, httpx
d = httpx.get("https://opendata.adsb.fi/api/v2/lat/38.95/lon/-77.0/dist/70",
              timeout=30).raise_for_status().json()
ac = d.get("ac") or []
print(f"{len(ac)} aircraft within 70 nm of DC\n")
for t, n in collections.Counter(a.get("type", "?") for a in ac).most_common():
    tag = "INDEPENDENT" if t.startswith("tisb") or t == "mlat" else "cooperative"
    print(f"  {n:>4}  {t:<16} {tag}")