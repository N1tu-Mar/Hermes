// Outreach Desk front end. Talks only to /api with the app token; never touches files or keys.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const LIST = ["organizations", "locations", "research_areas", "industries", "source_urls"];
const SUBTYPE_LABEL = { research_professor: "Research professors", startup: "Startups", speaker_mentor: "Speakers & mentors" };

// token: from ?t= once, then sessionStorage; strip from the address bar
const params = new URLSearchParams(location.search);
if (params.get("t")) { sessionStorage.setItem("appToken", params.get("t")); history.replaceState(null, "", "/"); }
const TOKEN = sessionStorage.getItem("appToken");

const state = { mode: null, subtype: null, cid: null, view: null, selected: new Set(), focus: null, poll: null, parsed: null, q: "due" };
const OUTCOMES = ["awaiting_reply", "replied", "interested", "meeting_booked", "declined", "bounced", "no_response", "closed"];
const label = (s) => String(s ?? "").replaceAll("_", " ");
const day = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });

async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-App-Token": TOKEN || "" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed (${res.status})`);
  }
  return (res.headers.get("content-type") || "").includes("json") ? res.json() : res.text();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 3500);
}
const run = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message); } };

function show(view) {
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  if (view !== "work") clearInterval(state.poll);
}

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
  fillIntake(intake);
  $("#original-text").textContent = text;
  $("#missing-question").hidden = !question;
  $("#missing-question").textContent = question || "";
  show("intake");
}));

// ---------------------------------------------------------------- intake
function fillIntake(intake, extras = {}) {
  const f = $("#intake-form");
  for (const [k, v] of Object.entries(intake)) {
    if (f.elements[k]) f.elements[k].value = Array.isArray(v) ? v.join(", ") : v ?? "";
  }
  for (const [k, v] of Object.entries(extras)) if (f.elements[k]) f.elements[k].value = v;
  const bg = localStorage.getItem("senderBackground");
  if (!f.elements.sender_background.value && bg) f.elements.sender_background.value = bg;
  toggleEventField(); project();
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
  if (intake.sender_background) localStorage.setItem("senderBackground", intake.sender_background);
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

// ---------------------------------------------------------------- workspace
async function openCampaign(cid) {
  state.cid = cid; state.selected.clear(); state.focus = null;
  $("#campaign-select").value = cid;
  show("work");
  $("#detail").innerHTML = '<p class="empty">Pick a person to see sources and their draft.</p>';
  await refresh();
  await renderCampaignTools();
  clearInterval(state.poll);
  state.poll = setInterval(run(refresh), 1500);
}

async function renderCampaignTools() {
  if (!state.cid || !$("#campaign-assets")) return;
  const [allAttachments, allContent, selected, rules, templates] = await Promise.all([
    api("/attachments"), api("/content"), api(`/campaigns/${state.cid}/assets`), api(`/campaigns/${state.cid}/rules`), api("/templates"),
  ]);
  const versions = (await Promise.all(templates.map(async (t) => (await api(`/templates/${t.template_id}/history`)).map((v) => ({ ...v, name: t.name })) ))).flat();
  $("#campaign-template").innerHTML = '<option value="">Automatic default</option>' + versions.map((v) => `<option value="${esc(v.template_id)}@${v.version}">${esc(v.name)} · v${v.version}</option>`).join("");
  const chosenA = new Set(selected.attachment_ids), chosenC = new Set(selected.content_ids);
  $("#campaign-assets").innerHTML = [
    ...allAttachments.map((a) => `<label><input type="checkbox" data-asset="attachment" value="${esc(a.attachment_id)}" ${chosenA.has(a.attachment_id) ? "checked" : ""}> ${esc(a.display_name)} <span class="note">${esc(a.kind)}</span></label>`),
    ...allContent.map((c) => `<label><input type="checkbox" data-asset="content" value="${esc(c.content_id)}" ${chosenC.has(c.content_id) ? "checked" : ""}> ${esc(c.name)} <span class="note">${esc(c.kind)}</span></label>`),
  ].join("") || '<span class="note">Add reusable content or attachments in the Messaging library first.</span>';
  $("#campaign-rules").innerHTML = rules.map((r) => `<div class="rule-row"><span>${esc(r.kind.replaceAll("_", " "))}</span><button class="small ghost" data-rule="${esc(r.rule_id)}" data-dry="true">Dry run</button><button class="small" data-rule="${esc(r.rule_id)}" data-dry="false">Apply</button></div>`).join("") || '<p class="note">No saved rules.</p>';
}

$("#btn-save-assets").addEventListener("click", run(async () => {
  const checked = $$("#campaign-assets input:checked");
  await api(`/campaigns/${state.cid}/assets`, { method: "PUT", body: {
    attachment_ids: checked.filter((x) => x.dataset.asset === "attachment").map((x) => x.value),
    content_ids: checked.filter((x) => x.dataset.asset === "content").map((x) => x.value),
  }});
  toast("Campaign assets saved. Any earlier approvals were withdrawn."); await refresh();
}));

$("#rule-form").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  await api(`/campaigns/${state.cid}/rules`, { method: "POST", body: { kind: e.target.kind.value, config: { limit: +e.target.limit.value } } });
  toast("Rule saved. Preview it before applying."); await renderCampaignTools();
}));
$("#campaign-rules").addEventListener("click", run(async (e) => {
  const rid = e.target.dataset.rule; if (!rid) return;
  const dryRun = e.target.dataset.dry === "true";
  const result = await api(`/campaigns/${state.cid}/rules/${rid}/run`, { method: "POST", body: { dry_run: dryRun } });
  const lines = result.actions.map((a) => `${a.candidate_id}: ${a.status} ${a.action} — ${a.reason}`).join("\n");
  alert(`${dryRun ? "Dry-run preview" : "Applied"}: ${result.summary}\n\n${lines}`);
  await refresh();
}));

function selectedTemplate() {
  const raw = $("#campaign-template")?.value || ""; if (!raw) return {};
  const [template_id, version] = raw.split("@"); return { template_id, template_version: +version };
}

async function refresh() {
  if (!state.cid) return;
  const [view, prog] = await Promise.all([api(`/campaigns/${state.cid}`), api(`/campaigns/${state.cid}/progress`)]);
  state.view = view;
  const it = view.intake;
  $("#work-kind").textContent = `${it.mode} · ${SUBTYPE_LABEL[it.subtype] || ""}`;
  $("#work-title").textContent = it.raw_request || [...(it.research_areas || []), ...(it.industries || [])].join(", ");
  $("#btn-followup").hidden = it.subtype !== "speaker_mentor";
  $("#mode-badge").hidden = false;
  $("#mode-badge").textContent = view.demo ? "DEMO DATA" : "LIVE";
  $("#gmail-state").textContent = view.gmail_error ? view.gmail_error
    : view.gmail_connected ? `Gmail connected · creates drafts only, never sends${view.gmail_sync ? " · reply tracking on" : ""}`
    : "Gmail not connected · approved drafts can be exported instead";
  $("#gmail-state").classList.toggle("needs", !!view.gmail_error);
  $("#btn-sync").disabled = !view.gmail_sync;
  $("#gmail-scope").textContent = view.gmail_sync
    ? "Reply tracking reads only labels and headers (sender, subject, date; never message bodies) of threads HERMES created, using the gmail.metadata scope. Nothing else in your inbox is read or stored."
    : view.gmail_connected ? "Reply tracking is off: your Gmail token predates the gmail.metadata scope. Run python -m app.gmail to grant it (headers/labels of HERMES threads only; no bodies)." : "";
  $("#btn-export").href = "#";
  const wf = $("#ranking-form").elements;
  for (const [k, v] of Object.entries(view.ranking_config?.weights || {})) if (wf[k]) wf[k].value = v;
  renderProgress(prog); renderRows(view.candidates); await renderQueue();
  if (state.focus && !$("#detail").contains(document.activeElement)) await renderDetail(state.focus);
}

function renderProgress(p) {
  const busy = (p.jobs.queued || 0) + (p.jobs.running || 0);
  const parts = [
    busy ? `<span class="live">${busy} job${busy > 1 ? "s" : ""} running</span>` : p.stopped ? "<span class='needs'>stopped</span>" : "<span>idle</span>",
    ...Object.entries(p.candidates).map(([k, v]) => `<span><b>${v}</b> ${esc(k.replaceAll("_", " "))}</span>`),
    ...Object.entries(p.drafts).map(([k, v]) => `<span><b>${v}</b> draft ${esc(k.replaceAll("_", " "))}</span>`),
    `<span>API <b>${p.usage.api_calls}</b>/${p.usage.budget}</span>`,
    `<span>cache hits <b>${p.usage.cache_hits}</b></span>`,
    `<span>tokens ≈ <b>${p.usage.input_tokens + p.usage.output_tokens}</b></span>`,
    `<span>pages fetched <b>${p.usage.pages_fetched}</b> · skipped by cap <b>${p.usage.pages_skipped}</b></span>`,
    `<span>downloaded <b>${Math.round(p.usage.bytes_fetched / 1024)}</b> KB</span>`,
  ];
  if (p.jobs.failed) parts.push(`<span class="needs">${p.jobs.failed} failed job(s)</span>`);
  $("#progress").innerHTML = parts.join("");
  $("#events").innerHTML = p.recent.map((e) => `<li>${new Date(e.at * 1000).toLocaleTimeString()} — ${esc(e.message)}</li>`).join("");
}

const RS = { discovered: ["not started", ""], selected: ["queued", ""], researching: ["researching…", ""],
  researched: ["done", "ok"], needs_contact_review: ["done · check email", "bad"], research_failed: ["failed · retry", "bad"],
  excluded: ["excluded", ""] };

function renderRows(cands) {
  if (!cands.length) return;
  const filter = $("#filter-candidates").value;
  cands = cands.filter((c) => filter === "all" ||
    (filter === "contact" && c.email) || (filter === "verified" && c.email_verified) ||
    (filter === "evidence" && c.evidence_count) || (filter === "pinned" && c.pinned) ||
    (filter === "missing" && (!c.email || !c.evidence_count || !c.organization || !c.role)));
  const sort = $("#sort-candidates").value;
  cands = [...cands].sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) ||
    (sort === "score" ? (b.ranking?.score || 0) - (a.ranking?.score || 0) :
     sort === "name" ? a.name.localeCompare(b.name) : a.status.localeCompare(b.status)));
  if (!cands.length) { $("#rows").innerHTML = '<tr><td colspan="6" class="empty">No candidates match this filter.</td></tr>'; return; }
  $("#rows").innerHTML = cands.map((c) => {
    const [statusLabel, cls] = RS[c.status] || [c.status, ""];
    const email = c.email ? `<span class="${c.email_verified ? "ok" : "needs"}">${esc(c.email)}${c.email_verified ? " ✓" : " (unverified)"}</span>`
      : c.status === "discovered" || c.status === "selected" ? "—" : '<span class="needs">missing</span>';
    const draft = c.draft_status ? `${esc(c.draft_status.replaceAll("_", " "))}${c.draft_flags ? ` <span class="needs">· ${c.draft_flags} flag${c.draft_flags > 1 ? "s" : ""}</span>` : ""}` : "—";
    return `<tr data-id="${esc(c.candidate_id)}" tabindex="0" aria-selected="${state.focus === c.candidate_id}" class="${c.status === "excluded" ? "excluded" : ""}">
      <td><input type="checkbox" aria-label="Select ${esc(c.name)}" ${state.selected.has(c.candidate_id) ? "checked" : ""}></td>
      <td><div class="who">${c.pinned ? "📌 " : ""}${esc(c.name)}</div><div class="org">${esc(c.role)}${c.role && c.organization ? " · " : ""}${esc(c.organization)}</div></td>
      <td class="score">${esc(c.ranking?.score ?? 0)}<small>/100</small></td>
      <td class="status ${cls}" title="${esc(c.error || "")}">${esc(statusLabel)}</td>
      <td class="status">${email}</td>
      <td class="status">${draft}</td>
      <td class="status ${["replied", "interested", "meeting_booked"].includes(c.outcome) ? "ok" : ["bounced", "declined"].includes(c.outcome) ? "bad" : ""}">${c.do_not_contact ? '<span class="needs">do not contact</span>' : esc(label(c.outcome) || "—")}${c.sequence_state === "paused" ? " · paused" : ""}</td></tr>`;
  }).join("");
}
$("#sort-candidates").addEventListener("change", () => renderRows(state.view?.candidates || []));
$("#filter-candidates").addEventListener("change", () => renderRows(state.view?.candidates || []));

$("#rows").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]"); if (!tr) return;
  const id = tr.dataset.id;
  if (e.target.matches("input[type=checkbox]")) {
    e.target.checked ? state.selected.add(id) : state.selected.delete(id); return;
  }
  focusRow(id);
});
$("#rows").addEventListener("keydown", (e) => {
  const tr = e.target.closest("tr[data-id]"); if (!tr) return;
  if (e.key === "Enter") focusRow(tr.dataset.id);
  if (e.key === " ") { e.preventDefault(); const cb = $("input", tr); cb.checked = !cb.checked; cb.checked ? state.selected.add(tr.dataset.id) : state.selected.delete(tr.dataset.id); }
  if (e.key === "ArrowDown") tr.nextElementSibling?.focus();
  if (e.key === "ArrowUp") tr.previousElementSibling?.focus();
});
$("#check-all").addEventListener("change", (e) => {
  (state.view?.candidates || []).forEach((c) => (e.target.checked ? state.selected.add(c.candidate_id) : state.selected.delete(c.candidate_id)));
  $$("#rows input[type=checkbox]").forEach((cb) => (cb.checked = e.target.checked));
});

function focusRow(id) {
  state.focus = id;
  $$("#rows tr").forEach((r) => r.setAttribute("aria-selected", r.dataset.id === id));
  run(renderDetail)(id);
}

async function renderDetail(id) {
  const { candidate: c, profile: p, draft: d, corrections, contact: o, timeline } = await api(`/campaigns/${state.cid}/candidates/${id}`);
  const link = (u, text) => (safeUrl(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text || u)}</a>` : esc(text || "—"));
  let html = `<div class="eyebrow">${esc(c.candidate_id)} · ${esc(c.status.replaceAll("_", " "))}</div>
    <h3>${esc(c.name)}</h3><div class="org">${esc(c.role)} · ${esc(c.organization)}</div>
    <div class="note">Profile ${link(c.profile_url, "page")} · found via ${link(c.discovery_source_url, "source")}</div>
    <section><div class="eyebrow">Transparent score · ${esc(c.ranking?.score ?? 0)}/100</div>
      <ol class="score-explain">${(c.ranking?.explanation || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
      <div class="draft-actions"><button class="small ghost" data-act="pin">${c.pinned ? "Unpin" : "Pin"}</button>
      <label>Manual adjustment <input id="score-adjustment" type="number" min="-100" max="100" value="${esc(c.manual_score_adjustment || 0)}"></label>
      <button class="small ghost" data-act="adjust">Apply</button></div></section>`;
  if (c.error) html += `<p class="banner warn">${esc(c.error)}</p>`;
  if (!p) {
    html += `<section><p class="note">Not researched yet. ${c.fit_hint ? `Discovery note: ${esc(c.fit_hint)}` : ""}</p>
      <button class="small primary" data-act="research">Research this person</button></section>`;
  } else {
    const contact = p.contact_email
      ? `<div class="contact ${p.email_verified_on_page ? "" : "needs"}">${esc(p.contact_email)} — ${p.email_verified_on_page ? `verified on ${link(p.contact_source_url, "page")}` : "NOT verified on a page; check before sending"}</div>`
      : `<div class="contact needs">No public email found. Look it up manually; nothing was guessed.</div>`;
    html += `<section><div class="eyebrow">Contact</div>${contact}<button class="small ghost" data-act="dnc">Mark do not contact</button></section>
      ${p.fit_reason ? `<section><div class="eyebrow">Why they fit</div><p>${esc(p.fit_reason)}</p><p class="note">${esc(p.summary)}</p></section>` : ""}
      ${p.notes?.unverified_model_note?.summary ? `<section><div class="eyebrow">Unverified model note (not used in emails)</div><p class="note">${esc(p.notes.unverified_model_note.summary)}</p></section>` : ""}
      <section><div class="eyebrow">Evidence (${p.evidence.length})</div>
      <ol class="claims">${p.evidence.map((e, i) => `<li><a class="cite" href="${esc(safeUrl(e.source_url) || "#")}" target="_blank" rel="noopener noreferrer" aria-label="Source ${i + 1}">e${i}</a>
        <span>${esc(e.claim)}<span class="src">${esc(e.source_type || "unknown source type")} · ${esc(e.source_locator || "no section/page")} · ${esc(e.provenance || "web")}<br>${esc(e.source_url)} · ${esc(e.retrieved_at)}</span></span></li>`).join("") || "<li class='needs'>No sourced claims. This person can't be personalized yet.</li>"}</ol>
      ${p.notes?.dropped_unsourced_claims ? `<p class="note">${p.notes.dropped_unsourced_claims} unsourced claim(s) discarded.</p>` : ""}
      <button class="small ghost" data-act="refresh">${p.status === "research_failed" ? "Retry research" : "Refresh research"}</button></section>`;
  }
  html += `<section><div class="eyebrow">Correct remembered information</div>
    <form class="correction-form"><select name="field">
      <option value="name">Identity / name</option><option value="organization">Affiliation</option>
      <option value="role">Role</option><option value="profile_url">Profile URL</option>
      <option value="contact_email">Email</option><option value="contact_source_url">Contact URL</option>
      <option value="fit_reason">Fit information</option><option value="summary">Summary</option>
      <option value="research_interests">Research interests</option><option value="evidence">Evidence (JSON list)</option>
    </select><button class="small" type="submit">Save correction</button>
    <textarea name="value" required placeholder="Correct value"></textarea></form>
    <p class="note">Saved globally with original and corrected values. Manual corrections are never labeled web-verified.</p>
    ${corrections?.length ? `<p class="note">${corrections.length} remembered correction(s) apply to this person.</p>` : ""}</section>`;
  if (d) {
    const locked = d.status === "gmail_draft_created" || d.status === "blocked";
    html += `<section><div class="eyebrow">Draft · ${esc(d.template_version)} · ${esc(d.status.replaceAll("_", " "))}</div>
      ${d.issues?.length ? `<ul class="flags">${d.issues.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${d.status === "blocked" ? "" : `<form class="draft-form">
        <label>Subject<input name="subject" value="${esc(d.subject)}" ${locked ? "disabled" : ""}></label>
        <label>Body<textarea name="body" ${locked ? "disabled" : ""}>${esc(d.body)}</textarea></label>
        <p class="note">Uses evidence: ${(d.evidence_ids || []).map(esc).join(", ") || "none"}</p>
        <p class="note"><b>Exact attachments for approval:</b> ${(d.attachments || []).map((a) => esc(a.display_name)).join(", ") || "none"}</p>
        <div class="draft-actions">
          ${locked ? `<span class="ok">In Gmail Drafts (${esc(d.gmail_draft_id)}). Not sent.</span>` : `
          <button type="submit" class="small">Save edits</button>
          <button type="button" class="small primary" data-act="approve" ${d.status !== "needs_review" ? "disabled" : ""}>Approve</button>`}
          ${(d.template_category === "speaker_invitation" || d.template_version.startsWith("speaker_invite") || d.template_version.startsWith("speaker_invitation")) && ["approved", "gmail_draft_created"].includes(d.status)
            ? `<button type="button" class="small ghost" data-act="invited" ${d.invited_at ? "disabled" : ""}>${d.invited_at ? "Invitation marked sent" : "I sent this invitation"}</button>` : ""}
          ${!(d.template_category === "speaker_invitation" || d.template_version.startsWith("speaker_invite") || d.template_version.startsWith("speaker_invitation")) && ["approved", "gmail_draft_created"].includes(d.status)
            ? `<button type="button" class="small ghost" data-act="contacted" ${d.invited_at ? "disabled" : ""}>${d.invited_at ? "Message marked sent" : "I sent this message"}</button>` : ""}
        </div></form>`}</section>`;
  } else if (p && p.status !== "research_failed") {
    html += `<section><button class="small" data-act="draft">Draft email</button></section>`;
  }
  html += renderOutreach(o, timeline);
  $("#detail").innerHTML = html;
}

function renderOutreach(o, timeline) {
  o = o || {};
  const sent = o.sent_at ? `Sent ${day(o.sent_at)} (${o.sent_source === "gmail" ? "seen in Gmail Sent" : "recorded by you"})`
    : o.gmail_thread_id ? "Draft is in Gmail; not sent yet. Sync Gmail after you send it." : "No email recorded as sent.";
  const seq = o.do_not_contact ? "do not contact" : o.sequence_state || "not started";
  return `<section><div class="eyebrow">Outcome · sequence ${esc(seq)}</div>
    <p class="note">${esc(sent)}${o.sync_error ? ` <span class="needs">${esc(o.sync_error)}</span>` : ""}</p>
    <form class="outcome-form">
      <label>Outcome<select name="outcome">${OUTCOMES.map((x) => `<option value="${x}" ${o.outcome === x ? "selected" : ""}>${label(x)}</option>`).join("")}</select></label>
      <label>Note <small>kept in the audit trail</small><input name="note" maxlength="300"></label>
      <button type="submit" class="small">Record</button>
    </form>
    <div class="seq-actions">
      ${o.sent_at ? "" : '<button class="small ghost" data-act="seq" data-seq="mark_sent">I sent it</button>'}
      ${o.sequence_state === "paused" ? '<button class="small ghost" data-act="seq" data-seq="resume">Resume follow-ups</button>'
        : o.sequence_state === "active" ? '<button class="small ghost" data-act="seq" data-seq="pause">Pause follow-ups</button>' : ""}
      ${o.sequence_state && o.sequence_state !== "stopped" ? '<button class="small ghost" data-act="seq" data-seq="stop">Stop follow-ups</button>' : ""}
      ${o.do_not_contact ? "" : '<button class="small ghost" data-act="seq" data-seq="do_not_contact">Do not contact</button>'}
    </div></section>
    <section><div class="eyebrow">Timeline</div>
    <ol class="timeline">${(timeline || []).map((e) => `<li>${esc(day(e.at))} · <b>${esc(label(e.kind))}</b> ${esc(e.detail)} <span>(${esc(e.source)})</span></li>`).join("") || "<li>Nothing recorded yet.</li>"}</ol></section>`;
}

async function renderQueue() {
  if ($("#queue-list").contains(document.activeElement)) return;  // don't clobber an edit in progress
  const groups = await api(`/campaigns/${state.cid}/followups`);
  $$(".queue-tabs .chip").forEach((b) => { b.setAttribute("aria-pressed", b.dataset.q === state.q); b.textContent = `${b.dataset.q[0].toUpperCase()}${b.dataset.q.slice(1)} (${groups[b.dataset.q].length})`; });
  const names = Object.fromEntries((state.view?.candidates || []).map((c) => [c.candidate_id, c.name]));
  const items = groups[state.q];
  $("#queue-list").innerHTML = items.map((f) => {
    const id = `${esc(f.candidate_id)}/${f.step}`;
    const editable = ["needs_review", "approved"].includes(f.status);
    return `<div class="fu" data-fu="${id}">
      <div><b>${esc(names[f.candidate_id] || f.candidate_id)}</b> · follow-up ${f.step + 1} · ${esc(label(f.template))} ·
        <span class="status">${esc(label(f.status))}</span> · due ${esc(day(f.due_at))}</div>
      <div class="note">Earlier email on record: “${esc(f.prior.subject || "—")}”, sent ${esc(f.prior.sent_on || "—")} (${esc(f.prior.source || "—")}).</div>
      ${f.issues?.length ? `<ul class="flags">${f.issues.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${f.body ? `<label>Subject<input value="${esc(f.subject)}" disabled></label>
        <label>Body<textarea name="body" ${editable ? "" : "disabled"}>${esc(f.body)}</textarea></label>` : ""}
      <div class="fu-actions">
        ${editable ? `<button class="small" data-fu-act="edit">Save edits</button><button class="small primary" data-fu-act="approve">${f.status === "approved" ? "Retry Gmail draft" : "Approve"}</button>` : ""}
        ${["scheduled", "needs_review", "blocked"].includes(f.status) ? '<button class="small ghost" data-fu-act="skip">Skip</button>' : ""}
        ${["scheduled", "blocked"].includes(f.status) ? `<input type="date" aria-label="New due date" value="${new Date(Math.max(f.due_at * 1000, Date.now())).toISOString().slice(0, 10)}"><button class="small ghost" data-fu-act="reschedule">Reschedule</button>` : ""}
        ${["scheduled", "generating", "needs_review", "approved", "blocked"].includes(f.status) ? '<button class="small ghost" data-fu-act="cancel">Cancel sequence</button>' : ""}
        ${f.gmail_draft_id ? `<span class="ok">In Gmail Drafts, same thread (${esc(f.gmail_draft_id)}). Not sent.</span>` : ""}
      </div></div>`;
  }).join("") || `<p class="empty">Nothing ${esc(state.q)}.</p>`;
}

$("#detail").addEventListener("click", run(async (e) => {
  const act = e.target.dataset.act; if (!act) return;
  const id = state.focus, base = `/campaigns/${state.cid}`;
  if (act === "research") await api(`${base}/research`, { method: "POST", body: { candidate_ids: [id] } });
  if (act === "refresh") await api(`${base}/research`, { method: "POST", body: { candidate_ids: [id], refresh: true } });
  if (act === "draft") await api(`${base}/drafts/generate`, { method: "POST", body: { candidate_ids: [id], ...selectedTemplate() } });
  if (act === "approve") { await api(`${base}/drafts/${id}/approve`, { method: "POST" }); toast("Approved."); }
  if (act === "seq") { await api(`${base}/contacts/${id}/sequence`, { method: "POST", body: { action: e.target.dataset.seq } }); toast("Saved."); }
  if (act === "invited") { await api(`${base}/drafts/${id}/mark-invited`, { method: "POST" }); toast("Invitation recorded; RSVP follow-up is now possible."); }
  if (act === "contacted") { await api(`${base}/drafts/${id}/mark-contacted`, { method: "POST" }); toast("Contact recorded; no-reply rules can now prepare a follow-up."); }
  if (act === "dnc") { await api(`${base}/candidates/${id}/do-not-contact`, { method: "PUT", body: { do_not_contact: true, reason: "Marked in HERMES UI" } }); toast("Do-not-contact policy saved. Drafting and Gmail actions are now blocked."); }
  if (act === "pin") await api(`${base}/candidates/${id}/ranking`, { method: "PATCH", body: { pinned: !state.view.candidates.find((c) => c.candidate_id === id)?.pinned } });
  if (act === "outcome") {
    const o = e.target.dataset.outcome, on = e.target.getAttribute("aria-pressed") === "true";
    await api(`${base}/candidates/${id}/outcomes${on ? `/${o}` : ""}`, on ? { method: "DELETE" } : { method: "POST", body: { outcome: o } });
  }
  if (act === "adjust") await api(`${base}/candidates/${id}/ranking`, { method: "PATCH", body: { manual_score_adjustment: +$("#score-adjustment").value } });
  await refresh(); await renderDetail(id);
}));
$("#detail").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.matches(".correction-form")) {
    let value = f.value.value;
    if (f.field.value === "research_interests") value = value.split(",").map((x) => x.trim()).filter(Boolean);
    if (f.field.value === "evidence") { try { value = JSON.parse(value); } catch { throw new Error("Evidence must be a JSON list of claim/source_url objects."); } }
    await api(`/campaigns/${state.cid}/candidates/${state.focus}/corrections`, { method: "PATCH", body: { field: f.field.value, value } });
    toast("Correction saved and will be reused in future campaigns.");
    await refresh(); await renderDetail(state.focus); return;
  }
  if (f.classList.contains("outcome-form")) {
    await api(`/campaigns/${state.cid}/contacts/${state.focus}/outcome`, { method: "POST", body: { outcome: f.outcome.value, note: f.note.value } });
    toast("Outcome recorded in the audit trail.");
    return refresh().then(() => renderDetail(state.focus));
  }
  await api(`/campaigns/${state.cid}/drafts/${state.focus}`, { method: "PATCH", body: { subject: f.subject.value, body: f.body.value } });
  toast("Saved. Review it again, then approve.");
  await refresh(); await renderDetail(state.focus);
}));

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
$("#btn-edit-intake").addEventListener("click", () => {
  state.editing = true;
  fillIntake(state.view.intake);
  $("#original-text").textContent = state.view.intake.raw_request || "";
  $("#missing-question").hidden = true;
  show("intake");
});
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
$("#btn-export").addEventListener("click", run(async (e) => {
  e.preventDefault();
  const text = await api(`/campaigns/${state.cid}/export`);
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  Object.assign(document.createElement("a"), { href: url, download: `${state.cid}-drafts.txt` }).click();
  URL.revokeObjectURL(url);
}));

// ---------------------------------------------------------------- reusable messaging library
const templateSamples = { first_name: "Avery", last_name: "Lin", recipient_name: "Avery Lin",
  organization: "Example Labs", role: "Founder", sender_background: "I’m a Rutgers student.",
  event_details: "a virtual founder panel", outreach_goal: "a brief conversation",
  specific_connection: "their work on responsible AI", signature: "Best,\nNitu",
  event_description: "A student-led panel.", club_description: "A Rutgers student organization.",
  personal_introduction: "I organize student programs.", call_to_action: "Would you be open to a call?",
  supporting_links: "https://example.org", prior_subject: "Rutgers invitation", prior_sent_on: "September 20" };

async function openLibrary() { show("library"); await loadLibrary(); }
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

// ---------------------------------------------------------------- contact timeline & outcomes
const STAGE_LABEL = { discovered: "Discovered", researched: "Researched", contactable: "Verified contact", research_failed: "Research failed",
  drafted: "Drafted", approved: "Approved", scheduled: "Scheduled", sent: "Sent", replied: "Replied", interested: "Interested",
  declined: "Declined", bounced: "Bounced", meeting_booked: "Meeting booked" };
const ANALYTICS_OUTCOMES = ["replied", "interested", "declined", "bounced", "meeting_booked"];
const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");

function timelineHtml(tl) {
  const has = new Set(tl.map((t) => t.stage));
  const buttons = has.has("sent")
    ? `<div class="draft-actions">${ANALYTICS_OUTCOMES.map((o) => `<button type="button" class="small ghost" data-act="outcome" data-outcome="${o}" aria-pressed="${has.has(o)}">${STAGE_LABEL[o]}</button>`).join("")}</div>
       <p class="note">Record what happened after you sent it. Press again to undo.</p>`
    : `<p class="note">Outcomes can be recorded once you mark the message as sent.</p>`;
  return `<section><div class="eyebrow">Timeline</div><ol class="timeline">${tl.map((t) =>
    `<li><b>${esc(STAGE_LABEL[t.stage] || t.stage)}</b> <span class="note">${esc(when(t.at))}</span></li>`).join("") || "<li class='note'>Nothing recorded yet.</li>"}</ol>${buttons}</section>`;
}

// ---------------------------------------------------------------- analytics
const unknown = '<span class="unknown" title="HERMES has no data for this">unknown</span>';
const num = (v) => (v == null ? unknown : esc(v.toLocaleString()));
const pct = (v) => (v == null ? unknown : `${(v * 100).toFixed(1)}%`);
const anState = { stage: "discovered", report: null };

function anQuery() {
  const f = $("#an-filters").elements, q = new URLSearchParams();
  for (const k of ["campaign_id", "start", "end", "group_by"]) if (f[k].value) q.set(k, f[k].value);
  return q.toString();
}

async function openAnalytics() {
  show("analytics");
  const list = await api("/campaigns"), sel = $("#an-filters").elements.campaign_id, cur = sel.value;
  sel.innerHTML = '<option value="">All campaigns</option>' + list.map((c) => `<option value="${esc(c.campaign_id)}">${esc((c.request || c.campaign_id).slice(0, 48))}</option>`).join("");
  sel.value = cur || state.cid || "";
  await loadAnalytics();
}

async function loadAnalytics() {
  const r = anState.report = await api(`/analytics?${anQuery()}`);
  const max = Math.max(1, ...Object.values(r.counts).filter((v) => v != null));
  const funnel = ["discovered", "researched", "contactable", "drafted", "approved", "scheduled", "sent", "replied", "interested", "declined", "bounced", "meeting_booked"];
  $("#an-funnel").innerHTML = funnel.map((s) => {
    const v = r.counts[s];
    return `<li><button type="button" data-stage="${s}" aria-pressed="${anState.stage === s}" ${v == null ? "disabled" : ""} title="${esc(STAGE_LABEL[s])}: ${v == null ? "unknown" : v}">
      <span class="f-label">${STAGE_LABEL[s]}</span><span class="f-track"><span class="f-bar" style="width:${v == null ? 0 : (v / max) * 100}%"></span></span>
      <span class="f-val">${num(v)}</span></button></li>`;
  }).join("");
  const R = r.rates, t = r.time_to_reply_hours, u = r.usage;
  const tile = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  $("#an-rates").innerHTML = [tile("Verified contact", pct(R.verified_contact_rate)), tile("Research failure", pct(R.research_failure_rate)),
    tile("Approval", pct(R.approval_rate)), tile("Reply", pct(R.reply_rate)), tile("Positive response", pct(R.positive_response_rate)),
    tile("Median time to reply", t.median == null ? unknown : `${t.median} h <small>(n=${t.n})</small>`)].join("");
  $("#an-usage").innerHTML = [tile("API calls", num(u.api_calls)), tile("Tokens in / out", `${num(u.input_tokens)} / ${num(u.output_tokens)}`),
    tile("Cache hits", num(u.cache_hits)), tile("Est. cost (USD)", u.estimated_cost_usd == null ? unknown : `$${u.estimated_cost_usd}`),
    tile("Processing time", u.processing_seconds == null ? unknown : `${u.processing_seconds}s <small>(${u.jobs_timed} jobs)</small>`)].join("");
  $("#an-notes").textContent = [r.notes.cost, u.undated_usage_excluded ? "Usage recorded before analytics existed has no date and is excluded from date-filtered totals." : ""].filter(Boolean).join(" ");
  const g = r.filters.group_by, cols = ["discovered", "researched", "contactable", "drafted", "approved", "sent", "replied", "meeting_booked"];
  $("#an-breakdown-title").textContent = `Breakdown by ${$("#an-filters").elements.group_by.selectedOptions[0].textContent.toLowerCase()}`;
  $("#an-breakdown").innerHTML = `<thead><tr><th>${esc(g.replace("_", " "))}</th><th>Contacts</th>${cols.map((c) => `<th>${STAGE_LABEL[c]}</th>`).join("")}<th>Approval</th><th>Reply</th><th>API calls</th></tr></thead><tbody>` +
    (r.breakdown.map((b) => `<tr><td>${b.group == null ? unknown : g === "campaign" ? `<a href="#" data-open="${esc(b.group)}">${esc(b.group)}</a>` : esc(b.group)}</td>
      <td>${b.contacts}</td>${cols.map((c) => `<td>${num(b.counts[c])}</td>`).join("")}<td>${pct(b.rates.approval_rate)}</td><td>${pct(b.rates.reply_rate)}</td>
      <td>${b.usage ? num(b.usage.api_calls) : unknown}</td></tr>`).join("") || `<tr><td colspan="${cols.length + 5}" class="empty">No records in this range.</td></tr>`) + "</tbody>";
  renderAnContacts();
  const feed = (x) => `<li><span class="note">${esc(when(x.at))}</span> <a href="#" data-open="${esc(x.campaign_id)}" data-cand="${esc(x.candidate_id || "")}">${esc(x.campaign_id)}${x.candidate_id ? ` · ${esc(x.candidate_id)}` : ""}</a> — ${esc(x.detail || x.message)}</li>`;
  $("#an-failures").innerHTML = r.failures.map((x) => feed({ ...x, detail: `${x.type.replaceAll("_", " ")}: ${x.detail}` })).join("") || "<li class='note'>No failures in this range.</li>";
  $("#an-activity").innerHTML = r.activity.map(feed).join("") || "<li class='note'>No activity in this range.</li>";
}

function renderAnContacts() {
  const s = anState.stage, rows = anState.report.rows.filter((r) => r.stages.includes(s));
  $("#an-contacts-title").textContent = `Contacts: ${STAGE_LABEL[s]} (${rows.length})`;
  $("#an-contacts").innerHTML = `<thead><tr><th>Person</th><th>Organization</th><th>Campaign</th><th>Template</th><th>Reached</th><th>${esc(STAGE_LABEL[s])} at</th></tr></thead><tbody>` +
    (rows.map((r) => `<tr><td><a href="#" data-open="${esc(r.campaign_id)}" data-cand="${esc(r.candidate_id)}">${esc(r.name || r.candidate_id)}</a></td>
      <td>${r.organization == null ? unknown : esc(r.organization)}</td><td>${esc(r.campaign_id)}</td><td>${r.template ? esc(r.template) : "—"}</td>
      <td>${r.stages.map((x) => esc(STAGE_LABEL[x] || x)).join(" → ")}</td><td>${esc(when(r.times[s]))}</td></tr>`).join("") || `<tr><td colspan="6" class="empty">Nobody reached this stage in range.</td></tr>`) + "</tbody>";
}

$("#btn-analytics").addEventListener("click", run(openAnalytics));
$("#an-filters").addEventListener("change", run(loadAnalytics));
$("#an-funnel").addEventListener("click", (e) => {
  const b = e.target.closest("[data-stage]"); if (!b) return;
  anState.stage = b.dataset.stage;
  $$("#an-funnel [data-stage]").forEach((x) => x.setAttribute("aria-pressed", x === b));
  renderAnContacts();
});
$("#an-filters").addEventListener("click", run(async (e) => {
  const kind = e.target.dataset.export; if (!kind) return;
  const text = await api(`/analytics/export?kind=${kind}&${anQuery()}`);
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  Object.assign(document.createElement("a"), { href: url, download: `hermes-${kind}.csv` }).click();
  URL.revokeObjectURL(url);
}));

// Links from analytics and notifications back to the campaign / contact record.
async function openRecord(cid, cand) {
  if (!cid) return;
  $("#notif-panel").hidden = true; $("#btn-bell").setAttribute("aria-expanded", false);
  await loadCampaignList(cid);
  await openCampaign(cid);
  if (cand) focusRow(cand);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-open]"); if (!a) return;
  e.preventDefault(); run(openRecord)(a.dataset.open, a.dataset.cand);
});

// ---------------------------------------------------------------- notifications (in-app; deduplicated server-side)
async function loadNotifications() {
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

// ---------------------------------------------------------------- navigation
document.addEventListener("click", (e) => {
  if (e.target.dataset.go === "home") { e.preventDefault(); state.cid = null; $("#campaign-select").value = ""; show("home"); }
});
$("#campaign-select").addEventListener("change", (e) => (e.target.value ? openCampaign(e.target.value) : show("home")));

async function loadCampaignList(select) {
  const list = await api("/campaigns");
  $("#campaign-select").innerHTML = '<option value="">New campaign</option>' +
    list.map((c) => `<option value="${esc(c.campaign_id)}">${esc((c.request || c.campaign_id).slice(0, 48))}</option>`).join("");
  if (select) $("#campaign-select").value = select;
}

if (!TOKEN) { $("#token-warning").hidden = false; $$(".view").forEach((v) => (v.hidden = true)); }
else { run(loadCampaignList)(); run(loadNotifications)(); setInterval(run(loadNotifications), 10000); }
