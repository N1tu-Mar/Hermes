// Outreach Desk front end. Talks only to /api with the app token; never touches files or keys.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const LIST = ["organizations", "locations", "research_areas", "industries"];
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
  clearInterval(state.poll);
  state.poll = setInterval(run(refresh), 1500);
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
  $("#rows").innerHTML = cands.map((c) => {
    const [label, cls] = RS[c.status] || [c.status, ""];
    const email = c.email ? `<span class="${c.email_verified ? "ok" : "needs"}">${esc(c.email)}${c.email_verified ? " ✓" : " (unverified)"}</span>`
      : c.status === "discovered" || c.status === "selected" ? "—" : '<span class="needs">missing</span>';
    const draft = c.draft_status ? `${esc(c.draft_status.replaceAll("_", " "))}${c.draft_flags ? ` <span class="needs">· ${c.draft_flags} flag${c.draft_flags > 1 ? "s" : ""}</span>` : ""}` : "—";
    return `<tr data-id="${esc(c.candidate_id)}" tabindex="0" aria-selected="${state.focus === c.candidate_id}" class="${c.status === "excluded" ? "excluded" : ""}">
      <td><input type="checkbox" aria-label="Select ${esc(c.name)}" ${state.selected.has(c.candidate_id) ? "checked" : ""}></td>
      <td><div class="who">${esc(c.name)}</div><div class="org">${esc(c.role)}${c.role && c.organization ? " · " : ""}${esc(c.organization)}</div></td>
      <td class="status ${cls}" title="${esc(c.error || "")}">${esc(label)}</td>
      <td class="status">${email}</td>
      <td class="status">${draft}</td>
      <td class="status ${["replied", "interested", "meeting_booked"].includes(c.outcome) ? "ok" : ["bounced", "declined"].includes(c.outcome) ? "bad" : ""}">${c.do_not_contact ? '<span class="needs">do not contact</span>' : esc(label(c.outcome) || "—")}${c.sequence_state === "paused" ? " · paused" : ""}</td></tr>`;
  }).join("");
}

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
  const { candidate: c, profile: p, draft: d, contact: o, timeline } = await api(`/campaigns/${state.cid}/candidates/${id}`);
  const link = (u, text) => (safeUrl(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text || u)}</a>` : esc(text || "—"));
  let html = `<div class="eyebrow">${esc(c.candidate_id)} · ${esc(c.status.replaceAll("_", " "))}</div>
    <h3>${esc(c.name)}</h3><div class="org">${esc(c.role)} · ${esc(c.organization)}</div>
    <div class="note">Profile ${link(c.profile_url, "page")} · found via ${link(c.discovery_source_url, "source")}</div>`;
  if (c.error) html += `<p class="banner warn">${esc(c.error)}</p>`;
  if (!p) {
    html += `<section><p class="note">Not researched yet. ${c.fit_hint ? `Discovery note: ${esc(c.fit_hint)}` : ""}</p>
      <button class="small primary" data-act="research">Research this person</button></section>`;
  } else {
    const contact = p.contact_email
      ? `<div class="contact ${p.email_verified_on_page ? "" : "needs"}">${esc(p.contact_email)} — ${p.email_verified_on_page ? `verified on ${link(p.contact_source_url, "page")}` : "NOT verified on a page; check before sending"}</div>`
      : `<div class="contact needs">No public email found. Look it up manually; nothing was guessed.</div>`;
    html += `<section><div class="eyebrow">Contact</div>${contact}</section>
      ${p.fit_reason ? `<section><div class="eyebrow">Why they fit</div><p>${esc(p.fit_reason)}</p><p class="note">${esc(p.summary)}</p></section>` : ""}
      ${p.notes?.unverified_model_note?.summary ? `<section><div class="eyebrow">Unverified model note (not used in emails)</div><p class="note">${esc(p.notes.unverified_model_note.summary)}</p></section>` : ""}
      <section><div class="eyebrow">Evidence (${p.evidence.length})</div>
      <ol class="claims">${p.evidence.map((e, i) => `<li><a class="cite" href="${esc(safeUrl(e.source_url) || "#")}" target="_blank" rel="noopener noreferrer" aria-label="Source ${i + 1}">e${i}</a>
        <span>${esc(e.claim)}<span class="src">${esc(e.source_url)} · ${esc(e.retrieved_at)}</span></span></li>`).join("") || "<li class='needs'>No sourced claims. This person can't be personalized yet.</li>"}</ol>
      ${p.notes?.dropped_unsourced_claims ? `<p class="note">${p.notes.dropped_unsourced_claims} unsourced claim(s) discarded.</p>` : ""}
      <button class="small ghost" data-act="refresh">${p.status === "research_failed" ? "Retry research" : "Refresh research"}</button></section>`;
  }
  if (d) {
    const locked = d.status === "gmail_draft_created" || d.status === "blocked";
    html += `<section><div class="eyebrow">Draft · ${esc(d.template_version)} · ${esc(d.status.replaceAll("_", " "))}</div>
      ${d.issues?.length ? `<ul class="flags">${d.issues.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${d.status === "blocked" ? "" : `<form class="draft-form">
        <label>Subject<input name="subject" value="${esc(d.subject)}" ${locked ? "disabled" : ""}></label>
        <label>Body<textarea name="body" ${locked ? "disabled" : ""}>${esc(d.body)}</textarea></label>
        <p class="note">Uses evidence: ${(d.evidence_ids || []).map(esc).join(", ") || "none"}</p>
        <div class="draft-actions">
          ${locked ? `<span class="ok">In Gmail Drafts (${esc(d.gmail_draft_id)}). Not sent.</span>` : `
          <button type="submit" class="small">Save edits</button>
          <button type="button" class="small primary" data-act="approve" ${d.status !== "needs_review" ? "disabled" : ""}>Approve</button>`}
          ${d.template_version.startsWith("speaker_invite") && ["approved", "gmail_draft_created"].includes(d.status)
            ? `<button type="button" class="small ghost" data-act="invited" ${d.invited_at ? "disabled" : ""}>${d.invited_at ? "Invitation marked sent" : "I sent this invitation"}</button>` : ""}
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
  if (act === "draft") await api(`${base}/drafts/generate`, { method: "POST", body: { candidate_ids: [id] } });
  if (act === "approve") { await api(`${base}/drafts/${id}/approve`, { method: "POST" }); toast("Approved."); }
  if (act === "seq") { await api(`${base}/contacts/${id}/sequence`, { method: "POST", body: { action: e.target.dataset.seq } }); toast("Saved."); }
  if (act === "invited") { await api(`${base}/drafts/${id}/mark-invited`, { method: "POST" }); toast("Invitation recorded; RSVP follow-up is now possible."); }
  await refresh(); await renderDetail(id);
}));
$("#detail").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const f = e.target;
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
  const r = await post("/drafts/generate", { candidate_ids: need() });
  toast(`Drafting ${r.queued.length}.${r.skipped_not_researched.length ? ` ${r.skipped_not_researched.length} need research first.` : ""}`);
}));
$("#btn-followup").addEventListener("click", run(async () => {
  await post("/drafts/generate", { candidate_ids: need(), followup: true });
  toast("Follow-ups are only drafted for invitations you marked as sent.");
}));
$("#btn-exclude").addEventListener("click", run(async () => { await post("/select", { candidate_ids: need(), action: "exclude" }); refresh(); }));
$("#btn-include").addEventListener("click", run(async () => { await post("/select", { candidate_ids: need(), action: "include" }); refresh(); }));
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
else run(loadCampaignList)();
