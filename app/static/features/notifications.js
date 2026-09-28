import { $, $$, esc, safeUrl, LIST, SUBTYPE_LABEL, OUTCOMES, label, day, when, AUTH, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";

// ---------------------------------------------------------------- notifications (in-app; deduplicated server-side)
export async function loadNotifications() {
  const n = await api(`/notifications${$("#notif-dismissed").checked ? "?include_dismissed=true" : ""}`);
  $("#notif-count").hidden = !n.unread;
  $("#notif-count").textContent = n.unread;
  if ($("#notif-panel").hidden) return;
  $("#notif-list").innerHTML = n.items.map((x) => `<li class="${x.read_at ? "" : "unread"} ${x.dismissed_at ? "dismissed" : ""}">
    <div><span class="eyebrow">${esc(x.kind.replaceAll("_", " "))} · ${esc(when(x.created_at))}</span><br>
      ${x.campaign_id ? `<a href="#" data-open="${esc(x.campaign_id)}" data-cand="${esc(x.candidate_id || "")}" data-nid="${x.id}">${esc(x.message)}</a>` : esc(x.message)}</div>
    <div class="notif-actions">${x.read_at ? "" : `<button class="small ghost" data-nact="read" data-nid="${x.id}">Read</button>`}
      ${x.dismissed_at ? "" : `<button class="small ghost" data-nact="dismiss" data-nid="${x.id}">Dismiss</button>`}</div></li>`).join("")
    || "<li class='note'>Nothing new.</li>";
}
$("#btn-bell").addEventListener("click", run(async () => {
  const p = $("#notif-panel"); p.hidden = !p.hidden;
  $("#btn-bell").setAttribute("aria-expanded", !p.hidden);
  await loadNotifications();
}));
$("#notif-panel").addEventListener("click", run(async (e) => {
  const nid = e.target.dataset.nid; if (!nid) return;
  await api(`/notifications/${nid}/${e.target.dataset.nact || "read"}`, { method: "POST" });
  await loadNotifications();
}));
$("#notif-read-all").addEventListener("click", run(async () => { await api("/notifications/read-all", { method: "POST" }); await loadNotifications(); }));
$("#notif-dismissed").addEventListener("change", run(loadNotifications));
