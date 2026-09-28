// Contacts screen: search/filter, edit, manual timeline entries, and duplicate-contact review resolution.
import { $, esc, when, label, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { routes } from "./nav.js";
import { download, openImport } from "./csvio.js";

const RELATIONSHIPS = ["new", "contacted", "replied", "meeting", "declined", "bounced"];
const KINDS = ["note", "invitation", "followup", "reply", "meeting", "decline", "bounce"];
const filters = $("#contact-filters"), rows = $("#contact-rows"), detail = $("#contact-detail"), reviews = $("#reviews");
let selected = null, seq = 0;

const dncTag = (c) => (c.do_not_contact ? ' <span class="dnc">do not contact</span>' : "");

async function loadList() {
  const n = ++seq, f = filters.elements;
  const q = new URLSearchParams({ q: f.q.value.trim(), tag: f.tag.value.trim(), relationship: f.relationship.value, dnc: f.dnc.value });
  const list = await api(`/contacts?${q}`);
  if (n !== seq) return;
  loaded(rows);
  rows.innerHTML = list.map((c) => `<tr data-key="${c.id}" tabindex="0" aria-selected="${c.id === selected}">
    <td><div class="who">${esc(c.name)}${dncTag(c)}</div><div class="org">${esc(c.organization || "")}${c.email ? ` · ${esc(c.email)}` : ""}</div></td>
    <td class="status">${esc(c.relationship)}</td><td>${c.campaign_count}</td>
    <td>${c.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(" ")}</td></tr>`).join("")
    || `<tr><td colspan="4" class="empty">${f.q.value || f.tag.value ? "No contacts match." : "No contacts yet. Add one or import a CSV."}</td></tr>`;
}

async function loadReviews() {
  const list = await api("/contact-reviews");
  reviews.hidden = !list.length;
  $("#review-count").hidden = !list.length; $("#review-count").textContent = list.length;
  reviews.innerHTML = list.length ? `<h3>Possible duplicates (${list.length})</h3>
    <p class="note">HERMES will not merge people on a guess. Decide for each: merge into an existing contact, or keep as a new person.</p>` +
    list.map((r) => `<div class="card" data-key="${r.id}"><div><b>${esc(r.person.name)}</b> <span class="note">${esc(r.person.organization || "")} ${esc(r.person.email || "")} ${esc(r.person.profile_url || "")}</span></div>
      <p class="note">${esc(r.reason)}</p>
      <div class="row-actions">${r.option_contacts.map((o) => `<button class="small" data-act="merge" data-contact="${o.id}">Same person as ${esc(o.name)}${o.email ? ` (${esc(o.email)})` : ""}</button>`).join("")}
        <button class="small ghost" data-act="new">Different person</button></div></div>`).join("") : "";
}

function form(c) {
  const v = (k) => esc(c?.[k] ?? "");
  return `<h3>${c ? esc(c.name) : "New contact"}</h3>
    <form class="fields contact-form" data-id="${c?.id ?? ""}">
      <div class="row"><label>Name<input name="name" value="${v("name")}" required></label><label>Organization<input name="organization" value="${v("organization")}"></label></div>
      <div class="row"><label>Role<input name="role" value="${v("role")}"></label><label>Email<input name="email" type="email" value="${v("email")}"></label></div>
      <label>Profile URL<input name="profile_url" value="${v("profile_url")}"></label>
      <div class="row"><label>Tags <small>comma separated</small><input name="tags" value="${esc((c?.tags || []).join(", "))}"></label>
        <label>Relationship<select name="relationship">${RELATIONSHIPS.map((x) => `<option ${c?.relationship === x ? "selected" : ""}>${x}</option>`).join("")}</select></label></div>
      <label>Notes<textarea name="notes" rows="3">${v("notes")}</textarea></label>
      <div class="row"><label class="check"><input type="checkbox" name="do_not_contact" ${c?.do_not_contact ? "checked" : ""}> Do not contact</label>
        <label>Reason<input name="dnc_reason" value="${v("dnc_reason")}"></label></div>
      <div class="actions"><button type="submit" class="primary">${c ? "Save contact" : "Add contact"}</button></div>
    </form>`;
}

async function showDetail(id) {
  selected = id; $$rows();
  if (id == null) { detail.innerHTML = form(null); return; }
  const { contact: c, campaigns, timeline } = await api(`/contacts/${id}`);
  detail.innerHTML = form(c) + `<section><div class="eyebrow">Campaigns</div>
      ${campaigns.map((x) => `<div class="note">${esc(x.name || x.campaign_id)}${x.archived ? " (archived)" : ""} · ${esc(x.candidate_id)}</div>`).join("") || '<p class="note">Not in any campaign.</p>'}</section>
    <section><div class="eyebrow">Timeline</div>
      <ol class="timeline">${timeline.map((t) => `<li>${esc(when(t.at))} · <b>${esc(label(t.kind))}</b> ${esc(t.detail || "")}${t.campaign_name ? ` <span class="note">(${esc(t.campaign_name)})</span>` : ""}</li>`).join("") || "<li>Nothing recorded yet.</li>"}</ol>
      <form class="inline-form interaction-form"><div class="row">
        <label>Log an event<select name="kind">${KINDS.map((k) => `<option value="${k}">${label(k)}</option>`).join("")}</select></label>
        <label>Date <small>optional</small><input name="at" type="date"></label></div>
        <label>Detail<input name="detail" maxlength="300"></label>
        <button type="submit" class="small">Add to timeline</button></form></section>`;
}
const $$rows = () => rows.querySelectorAll("tr").forEach((r) => r.setAttribute("aria-selected", r.dataset.key === String(selected)));

const refresh = () => keepFocus($("#view-contacts"), async () => {
  await Promise.all([loadList(), loadReviews()]);
  if (selected != null) await showDetail(selected);
});

export async function openContacts() {
  show("contacts");
  const sel = filters.elements.relationship;
  if (sel.options.length === 1) sel.insertAdjacentHTML("beforeend", RELATIONSHIPS.map((r) => `<option>${r}</option>`).join(""));
  loading(rows, "Loading contacts…");
  try { await Promise.all([loadList(), loadReviews()]); } catch (e) { failed(rows, e); }
}
routes.contacts = openContacts;

let timer;
filters.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run(loadList), 250); });
filters.addEventListener("change", run(loadList));
filters.addEventListener("submit", (e) => e.preventDefault());

rows.addEventListener("click", run((e) => { const tr = e.target.closest("tr[data-key]"); return tr && showDetail(+tr.dataset.key); }));
rows.addEventListener("keydown", (e) => {
  const tr = e.target.closest("tr[data-key]"); if (!tr) return;
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(showDetail)(+tr.dataset.key); }
  if (e.key === "ArrowDown") tr.nextElementSibling?.focus();
  if (e.key === "ArrowUp") tr.previousElementSibling?.focus();
});

$("#btn-new-contact").addEventListener("click", () => { selected = null; showDetail(null); $("[name=name]", detail).focus(); });
$("#btn-export-contacts").addEventListener("click", run(() => download("/contacts/export.csv", "hermes-contacts.csv")));
$("#btn-import-contacts").addEventListener("click", () => openImport($("#contact-import"),
  { path: "/contacts/import", done: refresh, hint: "Columns: name (required), organization, role, email, profile_url, tags, notes." }));

reviews.addEventListener("click", run(async (e) => {
  const act = e.target.dataset.act, id = e.target.closest("[data-key]")?.dataset.key; if (!act) return;
  await api(`/contact-reviews/${id}/resolve`, { method: "POST", body: { contact_id: act === "merge" ? +e.target.dataset.contact : null } });
  toast(act === "merge" ? "Merged into the existing contact." : "Kept as a new contact.");
  await refresh();
}));

detail.addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const f = e.target, d = Object.fromEntries(new FormData(f));
  if (f.classList.contains("interaction-form")) {
    await api(`/contacts/${selected}/interactions`, { method: "POST", body: { kind: d.kind, detail: d.detail, at: d.at || null } });
    toast("Added to the timeline."); return refresh();
  }
  const body = { ...d, tags: d.tags.split(",").map((t) => t.trim()).filter(Boolean), do_not_contact: f.do_not_contact.checked };
  if (f.dataset.id) { await api(`/contacts/${f.dataset.id}`, { method: "PATCH", body }); toast("Contact saved."); }
  else {
    const r = await api("/contacts", { method: "POST", body });
    if (r.contact_id == null) { toast("Possible duplicate: resolve it in the review list above."); selected = null; detail.innerHTML = '<p class="empty">Pick a contact to see notes and history.</p>'; return refresh(); }
    selected = r.contact_id; toast(r.result === "matched" ? "Matched an existing contact and updated it." : "Contact added.");
  }
  await refresh();
}));
