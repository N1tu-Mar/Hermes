import { $, $$, esc, safeUrl, LIST, SUBTYPE_LABEL, OUTCOMES, label, day, when, AUTH, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { refresh, selectedTemplate } from "./workspace.js";
import { fillIntake } from "./intake.js";
import { go } from "./nav.js";
import { download, openImport } from "./csvio.js";

// ---------------------------------------------------------------- batch controls
const need = () => { if (!state.selected.size) throw new Error("Tick at least one person first."); return [...state.selected]; };
const post = (p, body) => api(`/campaigns/${state.cid}${p}`, { method: "POST", body });
$("#btn-research").addEventListener("click", run(async () => {
  const ids = need();
  await post("/select", { candidate_ids: ids, action: "select" });
  const r = await post("/research", { candidate_ids: ids });
  toast(r.candidates.length ? `Researching ${r.candidates.length}. Already-researched people are skipped.` : "Everyone selected is already researched.");
  refresh();
}));
$("#btn-generate").addEventListener("click", run(async () => {
  const r = await post("/drafts/generate", { candidate_ids: need(), ...selectedTemplate() });
  toast(`Drafting ${r.queued.length}.${r.skipped_not_researched.length ? ` ${r.skipped_not_researched.length} need research first.` : ""}`);
}));
$("#btn-followup").addEventListener("click", run(async () => {
  await post("/drafts/generate", { candidate_ids: need(), followup: true, ...selectedTemplate() });
  toast("Follow-ups are only drafted for invitations you marked as sent.");
}));
$("#btn-exclude").addEventListener("click", run(async () => { await post("/select", { candidate_ids: need(), action: "exclude" }); refresh(); }));
$("#btn-include").addEventListener("click", run(async () => { await post("/select", { candidate_ids: need(), action: "include" }); refresh(); }));
$("#btn-compare").addEventListener("click", run(async () => {
  const ids = need(); if (ids.length < 2 || ids.length > 5) throw new Error("Select between 2 and 5 people to compare.");
  const r = await post("/compare", { candidate_ids: ids });
  $("#comparison").hidden = false;
  $("#comparison").innerHTML = `<div class="eyebrow">Side-by-side research comparison</div><div class="compare-grid">${r.candidates.map((c) => `<article class="compare-card">
    <h3>${esc(c.name)}</h3><p class="org">${esc(c.role)} · ${esc(c.organization)}</p><p class="score">${esc(c.ranking?.score || 0)}/100</p>
    <p><b>Freshest source:</b> ${esc(c.freshest_source_at || "none")}</p>
    <p><b>Sources:</b> ${c.official_sources.length} official · ${c.third_party_sources.length} third-party</p>
    <p><b>Missing:</b> ${esc(c.missing_fields.join(", ") || "none")}</p><ul>${c.confidence_limitations.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></article>`).join("")}</div>`;
}));
$("#ranking-form").addEventListener("submit", run(async (e) => {
  e.preventDefault(); const weights = Object.fromEntries([...e.target.elements].filter((x) => x.name).map((x) => [x.name, +x.value]));
  await api(`/campaigns/${state.cid}/ranking`, { method: "PATCH", body: { weights } }); toast("Ranking weights updated."); await refresh();
}));
$("#btn-discover").addEventListener("click", run(async () => { await post("/discover"); toast("Looking for more people."); }));
$("#btn-stop").addEventListener("click", run(async () => { await post("/stop"); toast("Stopped. Finished work is saved."); refresh(); }));
$("#btn-resume").addEventListener("click", run(async () => { await post("/resume"); toast("Resumed. Finished people are skipped."); refresh(); }));
$("#btn-edit-intake").addEventListener("click", run(async () => {
  state.editing = true;
  await fillIntake(state.view.intake);
  $("#original-text").textContent = state.view.intake.raw_request || "";
  $("#missing-question").hidden = true;
  show("intake");
}));
$("#btn-export-csv").addEventListener("click", run(() => download(`/campaigns/${state.cid}/export.csv`, `${state.cid}-candidates.csv`)));
$("#btn-import-cands").addEventListener("click", () => openImport($("#cand-import"),
  { path: `/campaigns/${state.cid}/import`, done: refresh, hint: "Columns: name (required), organization, role, email, profile_url, tags, notes." }));
$("#btn-gmail").addEventListener("click", run(async () => {
  const approved = (state.view?.candidates || []).filter((c) => c.draft_status === "approved").map((c) => c.candidate_id);
  if (!approved.length) throw new Error("No approved drafts yet. Approve a draft first.");
  const { results } = await post("/gmail-drafts", { candidate_ids: approved });
  $("#gmail-results").innerHTML = results.map((r) => `<div>${esc(r.candidate_id)}: ${esc(r.result)}${r.preview ? `<pre>${esc(r.preview)}</pre>` : ""}</div>`).join("");
  refresh();
}));
$("#btn-sync").addEventListener("click", run(async () => {
  const r = await post("/gmail-sync");
  toast(r.error || `Checked ${r.checked} thread(s): ${r.replies} repl${r.replies === 1 ? "y" : "ies"}, ${r.bounces} bounce(s), ${r.followups_queued} follow-up(s) drafting.`);
  refresh();
}));
$$(".queue-tabs .chip").forEach((b) => b.addEventListener("click", run(async () => { state.q = b.dataset.q; await renderQueue(); })));
$("#queue-list").addEventListener("click", run(async (e) => {
  const act = e.target.dataset.fuAct; if (!act) return;
  const box = e.target.closest("[data-fu]");
  const body = {};
  if (act === "edit") body.body = $("textarea", box).value;
  if (act === "reschedule") body.due_at = new Date(`${$("input[type=date]", box).value}T09:00`).getTime() / 1000;
  if (act === "cancel" && !box.dataset.confirm) { box.dataset.confirm = 1; e.target.textContent = "Click again to cancel all remaining steps"; return; }
  const r = await post(`/followups/${box.dataset.fu}/${act}`, body);
  toast(r.result || `Follow-up ${label(r.status)}.`);
  document.activeElement.blur(); await refresh();
}));
// Deleting is permanent and typed-confirmed on the Campaigns screen (POST /delete only); never from here.
$("#btn-delete-campaign").addEventListener("click", () => { toast("Archive the campaign, then delete it from Campaigns."); go("campaigns"); });
$("#btn-export").addEventListener("click", run(async (e) => {
  e.preventDefault();
  const text = await api(`/campaigns/${state.cid}/export`);
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  Object.assign(document.createElement("a"), { href: url, download: `${state.cid}-drafts.txt` }).click();
  URL.revokeObjectURL(url);
}));
