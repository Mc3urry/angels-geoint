// The ops view reads the same API as the 2D viewer and nothing else of
// ours. Upstream sources are reached only through the API.

export const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";

export let requests = 0;

export async function get(path) {
  requests += 1;
  let r;
  try {
    r = await fetch(`${API}${path}`);
  } catch {
    const e = new Error("the API is not reachable");
    e.kind = "offline";
    throw e;
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const e = new Error(typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`);
    e.status = r.status;
    throw e;
  }
  return r.json();
}

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
