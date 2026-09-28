import { $, $$, esc, safeUrl, LIST, SUBTYPE_LABEL, OUTCOMES, label, day, when, AUTH, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";

// ---------------------------------------------------------------- reusable messaging library
const templateSamples = { first_name: "Avery", last_name: "Lin", recipient_name: "Avery Lin",
  organization: "Example Labs", role: "Founder", sender_background: "I’m a Rutgers student.",
  event_details: "a virtual founder panel", outreach_goal: "a brief conversation",
  specific_connection: "their work on responsible AI", signature: "Best,\nNitu",
  event_description: "A student-led panel.", club_description: "A Rutgers student organization.",
  personal_introduction: "I organize student programs.", call_to_action: "Would you be open to a call?",
  supporting_links: "https://example.org", prior_subject: "Rutgers invitation", prior_sent_on: "September 20" };

export async function openLibrary() { show("library"); await loadLibrary(); }
async function loadLibrary() {
  const [templates, identities, content, attachments] = await Promise.all([
    api("/templates?include_archived=true"), api("/identities"), api("/content"), api("/attachments"),
  ]);
  $("#template-list").innerHTML = templates.map((t) => `<div class="library-item ${t.archived ? "excluded" : ""}"><button class="small ghost" data-template="${esc(t.template_id)}">Edit</button><b>${esc(t.name)}</b> · v${t.version}<br><span class="note">${esc(t.category)}${t.archived ? " · archived" : ""}</span></div>`).join("");
  const options = '<option value="">Any sender</option>' + identities.map((x) => `<option value="${x.id}">${esc(x.display_name)}</option>`).join("");
  $$("select[name=identity_id]").forEach((s) => { const old = s.value; s.innerHTML = options; s.value = old; });
  $("#content-list").innerHTML = content.map((x) => `<div class="library-item"><b>${esc(x.name)}</b> · ${esc(x.kind)}<br>${esc(x.body)}</div>`).join("") || '<p class="note">No reusable content yet.</p>';
  $("#attachment-list").innerHTML = attachments.map((x) => `<div class="library-item"><b>${esc(x.display_name)}</b> · ${esc(x.kind)} · ${(x.size / 1024).toFixed(1)} KB</div>`).join("") || '<p class="note">No attachments yet.</p>';
}
function clearTemplateForm() { $("#template-form").reset(); $("#template-form").elements.template_id.value = ""; $("#template-history").textContent = ""; $("#template-preview").textContent = ""; }
$("#btn-library").addEventListener("click", run(openLibrary));
$("#btn-template-new").addEventListener("click", clearTemplateForm);
$("#template-list").addEventListener("click", run(async (e) => {
  const id = e.target.dataset.template; if (!id) return;
  const [t, history] = await Promise.all([api(`/templates/${id}`), api(`/templates/${id}/history`)]);
  const f = $("#template-form"); for (const k of ["template_id", "name", "category", "subject", "body"]) f.elements[k].value = t[k] ?? "";
  $("#template-history").textContent = "Version history: " + history.map((v) => `v${v.version} (${v.change_note || "saved"})`).join(" · ");
}));
$("#template-form").addEventListener("submit", run(async (e) => {
  e.preventDefault(); const f = e.target;
  const body = Object.fromEntries(["name", "category", "subject", "body", "change_note"].map((k) => [k, f.elements[k].value]));
  const saved = f.elements.template_id.value ? await api(`/templates/${f.elements.template_id.value}`, { method: "PUT", body }) : await api("/templates", { method: "POST", body });
  toast(`Saved ${saved.template_id} v${saved.version}.${saved.invalidated_approvals ? ` ${saved.invalidated_approvals} approval(s) invalidated.` : ""}`); await loadLibrary();
}));
$("#btn-template-preview").addEventListener("click", run(async () => {
  const id = $("#template-form").elements.template_id.value; if (!id) throw new Error("Save the template before previewing it.");
  const p = await api(`/templates/${id}/preview`, { method: "POST", body: { values: templateSamples } });
  $("#template-preview").textContent = `Subject: ${p.subject}\n\n${p.body}`;
}));
$("#btn-template-duplicate").addEventListener("click", run(async () => {
  const id = $("#template-form").elements.template_id.value; if (!id) throw new Error("Choose a template first.");
  const copy = await api(`/templates/${id}/duplicate`, { method: "POST", body: {} }); toast(`Created ${copy.name}.`); await loadLibrary();
}));
$("#btn-template-archive").addEventListener("click", run(async () => {
  const id = $("#template-form").elements.template_id.value; if (!id) throw new Error("Choose a template first.");
  await api(`/templates/${id}/archive`, { method: "POST", body: { archived: true } }); clearTemplateForm(); await loadLibrary();
}));
$("#identity-form").addEventListener("submit", run(async (e) => {
  e.preventDefault(); await api("/identities", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); e.target.reset(); await loadLibrary();
}));
$("#content-form").addEventListener("submit", run(async (e) => {
  e.preventDefault(); const body = Object.fromEntries(new FormData(e.target)); body.identity_id = body.identity_id ? +body.identity_id : null;
  await api("/content", { method: "POST", body }); e.target.reset(); await loadLibrary();
}));
$("#attachment-form").addEventListener("submit", run(async (e) => {
  e.preventDefault(); const f = e.target, file = f.file.files[0]; if (!file) return;
  if (file.size > 10 * 1024 * 1024) throw new Error("Attachment exceeds 10 MB.");
  const content_base64 = await new Promise((resolve, reject) => { const r = new FileReader(); r.onerror = reject; r.onload = () => resolve(String(r.result).split(",")[1]); r.readAsDataURL(file); });
  await api("/attachments", { method: "POST", body: { filename: file.name, media_type: file.type, kind: f.kind.value,
    identity_id: f.identity_id.value ? +f.identity_id.value : null, content_base64 } });
  f.reset(); toast("Attachment stored locally."); await loadLibrary();
}));
