// HERMES front end core: DOM helpers, API client, shared state, busy/focus guards.
export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
export const LIST = ["organizations", "locations", "research_areas", "industries", "source_urls"];
export const SUBTYPE_LABEL = { research_professor: "Research professors", startup: "Startups", speaker_mentor: "Speakers & mentors" };
export const OUTCOMES = ["awaiting_reply", "replied", "interested", "meeting_booked", "declined", "bounced", "no_response", "closed"];
export const label = (s) => String(s ?? "").replaceAll("_", " ");
export const day = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
export const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");

// Local mode: token from ?t= once, then sessionStorage; strip from the address bar.
// Remote mode: session cookie (HttpOnly, set by the server) + CSRF token from /auth/session; no app token at all.
const params = new URLSearchParams(location.search);
if (params.get("t")) { sessionStorage.setItem("appToken", params.get("t")); history.replaceState(null, "", "/"); }
export const TOKEN = sessionStorage.getItem("appToken");
export const AUTH = { mode: "local", csrf: null, user: null, onUnauth: () => {} };
// Sender background is remembered per browser only in local single-user mode. Remote accounts share an origin,
// so nothing is ever persisted there and any legacy value is purged on sign-in/out.
export const savedBackground = {
  get: () => { try { return AUTH.mode === "remote" ? null : localStorage.getItem("senderBackground"); } catch { return null; } },
  set: (v) => { try { if (AUTH.mode !== "remote") localStorage.setItem("senderBackground", v); } catch { /* storage blocked */ } },
  purge: () => { try { localStorage.removeItem("senderBackground"); } catch { /* storage blocked */ } },
};
export const state = { mode: null, subtype: null, cid: null, view: null, selected: new Set(), focus: null, poll: null, parsed: null, q: "due" };

export async function api(path, opts = {}, prefix = "/api") {
  const auth = AUTH.mode === "remote" ? { "X-CSRF-Token": AUTH.csrf || "" } : { "X-App-Token": TOKEN || "" };
  const res = await fetch(`${prefix}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...auth },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && AUTH.mode === "remote" && prefix === "/api") { AUTH.onUnauth(); throw new Error("Please sign in again."); }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const d = err.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Request failed (${res.status})`);
  }
  return (res.headers.get("content-type") || "").includes("json") ? res.json() : res.text();
}

export function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false; t.classList.toggle("error", isError);
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 3500);
}

// Wrap an event handler: errors become toasts, and the triggering button/form is disabled while it runs so a
// second click or Enter cannot double-submit. Focus returns to the trigger if it survives the re-render.
export const run = (fn) => async (...a) => {
  const ev = a[0] instanceof Event ? a[0] : null;
  const form = ev?.target instanceof HTMLFormElement ? ev.target : null;
  const btn = ev?.submitter || ev?.target?.closest?.("button");
  const gate = form || btn;
  if (gate && (gate.dataset.busy || btn?.disabled)) { ev.preventDefault?.(); return; }
  if (gate) { gate.dataset.busy = "1"; gate.setAttribute("aria-busy", "true"); }
  const hadFocus = btn && document.activeElement === btn;
  if (btn) btn.disabled = true;
  try { return await fn(...a); } catch (e) { toast(e.message, true); }
  finally {
    if (gate) { delete gate.dataset.busy; gate.removeAttribute("aria-busy"); }
    if (btn) { btn.disabled = false; if (hadFocus && btn.isConnected) btn.focus(); }
  }
};

// Re-render a container while keeping keyboard focus on the same logical control (data-key row + data-act button).
export async function keepFocus(root, render) {
  const a = document.activeElement;
  const inside = a && root.contains(a) ? a : null;
  const key = inside?.closest("[data-key]")?.dataset.key, act = inside?.dataset.act, id = inside?.id;
  await render();
  const target = (id && document.getElementById(id))
    || (key != null && ((act && $(`[data-key="${CSS.escape(key)}"] [data-act="${act}"]`, root)) || $(`[data-key="${CSS.escape(key)}"] button, [data-key="${CSS.escape(key)}"]`, root)))
    || null;
  if (inside && target) target.focus();
  else if (inside) (root.querySelector("h2, h3, [tabindex]") || root).focus?.();
}

export function show(view) {
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  $$(".nav a[data-go]").forEach((a) => (a.dataset.go === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  if (view !== "work") clearInterval(state.poll);
  const h = $(`#view-${view} h1, #view-${view} h2`);
  if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
}

// Empty-state / loading / error rendering for list screens.
export const loading = (el, text = "Loading…") => { el.setAttribute("aria-busy", "true"); el.innerHTML = `<p class="empty">${esc(text)}</p>`; };
export const loaded = (el) => el.removeAttribute("aria-busy");
export const failed = (el, e) => { loaded(el); el.innerHTML = `<p class="empty needs" role="alert">${esc(e.message)}</p>`; };
