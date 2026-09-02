"""Confirm OpenSky credentials work. Prints aircraft currently in the AOI."""
import httpx
from dotenv import load_dotenv
from angels.config import (AOI, OPENSKY_API_BASE, OPENSKY_TOKEN_URL,
                           OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET)

load_dotenv()

# config.py reads env at import, so re-read after load_dotenv
import os
cid = os.getenv("OPENSKY_CLIENT_ID") or OPENSKY_CLIENT_ID
sec = os.getenv("OPENSKY_CLIENT_SECRET") or OPENSKY_CLIENT_SECRET
assert cid and sec, "credentials missing -- check .env"

tok = httpx.post(OPENSKY_TOKEN_URL, data={
    "grant_type": "client_credentials",
    "client_id": cid,
    "client_secret": sec,
}).raise_for_status().json()["access_token"]
print(f"token ok ({len(tok)} chars)")

lomin, lamin, lomax, lamax = AOI
r = httpx.get(f"{OPENSKY_API_BASE}/states/all",
              params={"lamin": lamin, "lomin": lomin,
                      "lamax": lamax, "lomax": lomax},
              headers={"Authorization": f"Bearer {tok}"},
              timeout=30).raise_for_status().json()

states = r.get("states") or []
print(f"{len(states)} aircraft over Baltimore right now")
for s in states[:5]:
    print(f"  {s[0]}  {(s[1] or '').strip():8}  {s[6]:.3f},{s[5]:.3f}")