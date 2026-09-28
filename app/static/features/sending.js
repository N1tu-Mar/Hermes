import { $, $$, esc, safeUrl, LIST, SUBTYPE_LABEL, OUTCOMES, label, day, when, AUTH, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { refresh, renderDetail } from "./workspace.js";

// ---------------------------------------------------------------- sending (opt-in)
const LIVE = ["scheduled", "sending", "uncertain", "sent", "bounced", "replied"];
export const liveSend = (id) => (state.sends || []).some((s) => s.candidate_id === id && LIVE.includes(s.status));

export function renderSending(st, sends, view) {
  state.sending = st;
  const mode = st.emergency_stop ? "Emergency stop" : !st.enabled ? "Draft-only · sending off" : st.paused ? "Sending paused" : "Sending ON";
  $("#send-mode").textContent = mode;
  $("#send-mode").className = st.enabled && !st.paused ? "needs" : "";
  $("#sending").classList.toggle("live", st.enabled && !st.paused);
  $("#send-summary").textContent = [
    st.gmail_can_send ? "Gmail send scope granted" : st.gmail_connected ? "Gmail token has no send scope" : "Gmail not connected",
    `${st.sent_last_hour}/${st.hourly_limit} this hour, ${st.sent_last_day}/${st.daily_limit} today, ${st.spacing_seconds}s apart`,
    `quiet ${st.quiet_start}–${st.quiet_end} ${st.timezone}${st.quiet_now ? " (now)" : ""}`, `now ${st.now_local}`,
  ].join(" · ");
  $("#btn-send-global").textContent = st.enabled ? "Turn sending off" : "Turn sending on…";
  $("#btn-send-pause").textContent = st.paused ? "Unpause" : "Pause all";
  $("#send-campaign").checked = view.sending_enabled;
  const f = $("#send-settings");
  if (!f.contains(document.activeElement)) for (const k of ["timezone", "quiet_start", "quiet_end", "daily_limit", "hourly_limit", "spacing_seconds"]) f.elements[k].value = st[k];
  if ($("#send-rows").contains(document.activeElement) || $("#send-rows details[open]")) return;  // don't yank a row mid-interaction
  const names = Object.fromEntries(view.candidates.map((c) => [c.candidate_id, c.name]));
  $("#send-rows").innerHTML = sends.length ? sends.slice().reverse().map((s) => `<tr data-send="${esc(s.send_id)}">
    <td><div class="who">${esc(names[s.candidate_id] || s.candidate_id)}</div><div class="org">${esc(s.subject)}</div></td>
    <td class="status">${esc(s.recipient)}</td><td class="status">${esc(s.scheduled_local)}</td>
    <td class="status ${["failed", "uncertain", "bounced"].includes(s.status) ? "bad" : s.status === "sent" ? "ok" : ""}" title="${esc(s.error || "")}">${esc(s.status)}${s.error && s.status !== "sent" ? ` · ${esc(s.error.slice(0, 60))}` : ""}
      <details><summary>log</summary><ol class="audit"></ol></details></td>
    <td>${s.status === "scheduled" ? `<button class="small ghost" data-send-act="cancel">Cancel</button>` : ""}
      ${["sent", "replied"].includes(s.status) ? `<select data-send-act="outcome" aria-label="Record outcome"><option value="">Outcome…</option>
        <option value="replied">replied</option><option value="declined">declined</option><option value="bounced">bounced</option></select>` : ""}</td></tr>`).join("")
    : '<tr><td colspan="5" class="empty">Nothing scheduled. Approve a draft, then use Send or Schedule on it.</td></tr>';
}

export async function sendFlow(id, when) {
  const base = `/campaigns/${state.cid}/sends`;
  const pv = await api(`${base}/preview`, { method: "POST", body: { candidate_id: id, scheduled_at: when } });
  const m = pv.message, dlg = $("#send-dialog");
  $("#send-dialog-title").textContent = when ? `Schedule for ${pv.scheduled_local}` : "Send now";
  $("#send-facts").innerHTML = [["From", m.sender || "unknown"], ["To", m.recipient || "missing"], ["Subject", m.subject],
    ["Attachments", m.attachments.length ? m.attachments.join(", ") : "none"], ["When", when ? pv.scheduled_local : "as soon as limits allow"],
    ["Rules", `quiet ${pv.quiet_hours} ${pv.timezone}${pv.paused ? " · queue is PAUSED" : ""}`]]
    .map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("#send-body").textContent = m.body;
  $("#send-blockers").innerHTML = pv.blockers.map((b) => `<li>${esc(b)}</li>`).join("");
  const btn = $("#btn-send-confirm");
  btn.disabled = pv.blockers.length > 0;
  btn.textContent = pv.blockers.length ? "Can't send (see above)" : when ? "Approve and schedule" : "Approve and send";
  dlg.returnValue = "";
  dlg.showModal();
  await new Promise((r) => dlg.addEventListener("close", r, { once: true }));
  if (dlg.returnValue !== "confirm") return;
  const row = await api(base, { method: "POST", body: { candidate_id: id, scheduled_at: when, approval_hash: pv.approval_hash } });
  toast(`Approved. ${when ? `Scheduled for ${row.scheduled_local}.` : "Queued; it goes out on the next scheduler tick if limits allow."}`);
  await refresh(); await renderDetail(id);
}

$("#btn-send-global").addEventListener("click", run(async () => {
  const on = !state.sending.enabled;
  if (on && !confirm("Turn on real email sending?\n\nMessages still go out only for campaigns you enable, and only after you approve each exact message.")) return;
  await api("/sending/settings", { method: "PATCH", body: { enabled: on } });
  toast(on ? "Sending is on. Each message still needs your approval." : "Sending is off. Scheduled messages are held.");
  refresh();
}));
$("#send-campaign").addEventListener("change", run(async (e) => {
  await api(`/campaigns/${state.cid}/sending`, { method: "PATCH", body: { enabled: e.target.checked } }); refresh();
}));
$("#btn-send-pause").addEventListener("click", run(async () => {
  await api(state.sending.paused ? "/sending/unpause" : "/sending/pause", { method: "POST" }); refresh();
}));
$("#btn-estop").addEventListener("click", run(async () => {
  const r = await api("/sending/emergency-stop", { method: "POST" });
  toast(`Emergency stop: sending off, ${r.cancelled.length} scheduled message(s) cancelled.`); refresh();
}));
$("#send-settings").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  await api("/sending/settings", { method: "PATCH", body: { timezone: f.timezone.value, quiet_start: f.quiet_start.value,
    quiet_end: f.quiet_end.value, daily_limit: +f.daily_limit.value, hourly_limit: +f.hourly_limit.value, spacing_seconds: +f.spacing_seconds.value } });
  document.activeElement.blur(); toast("Limits saved."); refresh();
}));
$("#suppress-form").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  await api("/suppressions", { method: "POST", body: { email: e.target.email.value, reason: e.target.reason.value } });
  e.target.reset(); toast("Added. Any scheduled message to that address was cancelled."); refresh();
}));
$("#send-rows").addEventListener("click", run(async (e) => {
  if (e.target.dataset.sendAct !== "cancel") return;
  await api(`/sends/${e.target.closest("tr").dataset.send}/cancel`, { method: "POST" }); toast("Cancelled."); refresh();
}));
$("#send-rows").addEventListener("change", run(async (e) => {
  if (e.target.dataset.sendAct !== "outcome" || !e.target.value) return;
  await api(`/sends/${e.target.closest("tr").dataset.send}/outcome`, { method: "POST", body: { outcome: e.target.value } });
  e.target.blur(); toast("Outcome recorded."); refresh();
}));
$("#send-rows").addEventListener("toggle", run(async (e) => {
  if (!e.target.open) return;
  const d = await api(`/sends/${e.target.closest("tr").dataset.send}`);
  $("ol", e.target).innerHTML = d.audit.map((a) => `<li>${new Date(a.at * 1000).toLocaleString()} — ${esc(a.event)}</li>`).join("");
}), true);
