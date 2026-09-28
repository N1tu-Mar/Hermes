import { savedBackground, $, $$, esc, safeUrl, LIST, SUBTYPE_LABEL, OUTCOMES, label, day, when, AUTH, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { openCampaign } from "./workspace.js";
import { loadCampaignList } from "./nav.js";

// ---------------------------------------------------------------- landing
$$(".mode").forEach((b) => b.addEventListener("click", () => {
  state.mode = b.dataset.mode; state.subtype = b.dataset.mode === "research" ? "research_professor" : null;
  $$(".mode").forEach((m) => m.setAttribute("aria-pressed", m === b));
  $("#subtypes").hidden = state.mode !== "outreach";
  $$(".chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
  $("#ask-form").hidden = state.mode === "outreach";
  if (state.mode === "research") $("#ask-text").focus();
}));
$$(".chip").forEach((c) => c.addEventListener("click", () => {
  state.subtype = c.dataset.subtype;
  $$(".chip").forEach((x) => x.setAttribute("aria-pressed", x === c));
  $("#ask-form").hidden = false;
  $("#ask-text").placeholder = {
    startup: "Early-stage climate tech startups in NYC or NJ; I want to reach a technical founder about a summer internship",
    research_professor: "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates",
    speaker_mentor: "Founders who could speak at a Road to Silicon Valley fireside chat about raising a pre-seed round, remote OK",
  }[state.subtype];
  $("#ask-text").focus();
}));

$("#ask-form").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const text = $("#ask-text").value.trim();
  const { intake, question } = await api("/parse", { method: "POST", body: { text, mode: state.mode, subtype: state.subtype } });
  state.parsed = intake;
  await fillIntake(intake);
  $("#original-text").textContent = text;
  $("#missing-question").hidden = !question;
  $("#missing-question").textContent = question || "";
  show("intake");
}));

// ---------------------------------------------------------------- intake
export async function fillIntake(intake, extras = {}) {
  const f = $("#intake-form");
  await loadIdentityOptions(intake.sender_identity_id);
  for (const [k, v] of Object.entries(intake)) {
    if (f.elements[k]) f.elements[k].value = Array.isArray(v) ? v.join(", ") : v ?? "";
  }
  for (const [k, v] of Object.entries(extras)) if (f.elements[k]) f.elements[k].value = v;
  const bg = savedBackground.get();
  if (!f.elements.sender_background.value && bg) f.elements.sender_background.value = bg;
  toggleEventField(); toggleCustom(); project();
}
async function loadIdentityOptions(selected) {
  const f = $("#intake-form").elements.sender_identity_id;
  const list = await api("/identities");
  f.innerHTML = '<option value="">Custom background (below)</option>' + list.map((x) => `<option value="${esc(x.id)}">${esc(x.display_name)}${x.organization ? ` · ${esc(x.organization)}` : ""}</option>`).join("");
  f.value = selected && list.some((x) => String(x.id) === String(selected)) ? String(selected) : "";
  $("#identity-hint").hidden = list.length > 0;
}
function toggleCustom() {
  $("[data-when=custom]").hidden = !!$("#intake-form").elements.sender_identity_id.value;
}
function toggleEventField() {
  $("[data-for=speaker_mentor]").hidden = $("#intake-form").elements.subtype.value !== "speaker_mentor";
}
function project() {
  const f = $("#intake-form").elements;
  const n = +f.max_candidates.value || 0;
  $("#projection").textContent = `Projected API calls: 1 discovery + up to ${n} research + up to ${n} drafts = ${1 + 2 * n}. Budget: ${f.budget.value}. Cached pages and research don't count again.`;
}
$("#intake-form").addEventListener("input", project);
$("#intake-form").elements.subtype.addEventListener("change", toggleEventField);
$("#intake-form").elements.sender_identity_id.addEventListener("change", toggleCustom);

function readIntake() {
  const f = $("#intake-form");
  const out = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    out[el.name] = LIST.includes(el.name) ? el.value.split(",").map((s) => s.trim()).filter(Boolean) : el.value.trim() || null;
  }
  out.raw_request = $("#original-text").textContent;
  return out;
}

$("#intake-form").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const intake = readIntake();
  if (intake.sender_background && !intake.sender_identity_id) savedBackground.set(intake.sender_background);
  if (state.cid && state.editing) {
    await api(`/campaigns/${state.cid}/intake`, { method: "PATCH", body: { ...intake, budget: +intake.budget } });
    state.editing = false; toast("Saved. Drafts using old details now need regenerating before approval.");
    return openCampaign(state.cid);
  }
  const { campaign_id } = await api("/campaigns", { method: "POST",
    body: { intake, max_candidates: +intake.max_candidates, budget: +intake.budget } });
  await api(`/campaigns/${campaign_id}/discover`, { method: "POST" });
  await loadCampaignList(campaign_id);
  openCampaign(campaign_id);
}));
