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

const state = { mode: null, subtype: null, cid: null, view: null, selected: new Set(), focus: null, poll: null, parsed: null };

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
  const [view, prog, sending, sends] = await Promise.all([api(`/campaigns/${state.cid}`), api(`/campaigns/${state.cid}/progress`),
    api("/sending"), api(`/campaigns/${state.cid}/sends`)]);
  state.view = view; state.sends = sends;
  const it = view.intake;
  $("#work-kind").textContent = `${it.mode} · ${SUBTYPE_LABEL[it.subtype] || ""}`;
  $("#work-title").textContent = it.raw_request || [...(it.research_areas || []), ...(it.industries || [])].join(", ");
  $("#btn-followup").hidden = it.subtype !== "speaker_mentor";
  $("#mode-badge").hidden = false;
  $("#mode-badge").textContent = view.demo ? "DEMO DATA" : "LIVE";
  $("#gmail-state").textContent = view.gmail_connected ? "Gmail connected · this button creates drafts only, never sends"
    : "Gmail not connected · approved drafts can be exported instead";
  $("#btn-export").href = "#";
  renderProgress(prog); renderRows(view.candidates); renderSending(sending, sends, view);
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
    const send = c.send_status ? ` <span class="${["failed", "uncertain", "bounced"].includes(c.send_status) ? "needs" : "ok"}">· ${esc(c.send_status)}</span>` : "";
    return `<tr data-id="${esc(c.candidate_id)}" tabindex="0" aria-selected="${state.focus === c.candidate_id}" class="${c.status === "excluded" ? "excluded" : ""}">
      <td><input type="checkbox" aria-label="Select ${esc(c.name)}" ${state.selected.has(c.candidate_id) ? "checked" : ""}></td>
      <td><div class="who">${esc(c.name)}</div><div class="org">${esc(c.role)}${c.role && c.organization ? " · " : ""}${esc(c.organization)}</div></td>
      <td class="status ${cls}" title="${esc(c.error || "")}">${esc(label)}</td>
      <td class="status">${email}</td>
      <td class="status">${draft}${send}</td></tr>`;
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
  const { candidate: c, profile: p, draft: d } = await api(`/campaigns/${state.cid}/candidates/${id}`);
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
          ${["approved", "gmail_draft_created"].includes(d.status) && !liveSend(id) ? `
          <button type="button" class="small" data-act="send-now">Send now…</button>
          <input type="datetime-local" name="send_at" aria-label="Send at (sending timezone)" class="send-at">
          <button type="button" class="small" data-act="schedule">Schedule…</button>` : ""}
          ${d.template_version.startsWith("speaker_invite") && ["approved", "gmail_draft_created"].includes(d.status)
            ? `<button type="button" class="small ghost" data-act="invited" ${d.invited_at ? "disabled" : ""}>${d.invited_at ? "Invitation marked sent" : "I sent this invitation"}</button>` : ""}
        </div></form>`}</section>`;
  } else if (p && p.status !== "research_failed") {
    html += `<section><button class="small" data-act="draft">Draft email</button></section>`;
  }
  $("#detail").innerHTML = html;
}

$("#detail").addEventListener("click", run(async (e) => {
  const act = e.target.dataset.act; if (!act) return;
  const id = state.focus, base = `/campaigns/${state.cid}`;
  if (act === "research") await api(`${base}/research`, { method: "POST", body: { candidate_ids: [id] } });
  if (act === "refresh") await api(`${base}/research`, { method: "POST", body: { candidate_ids: [id], refresh: true } });
  if (act === "draft") await api(`${base}/drafts/generate`, { method: "POST", body: { candidate_ids: [id] } });
  if (act === "approve") { await api(`${base}/drafts/${id}/approve`, { method: "POST" }); toast("Approved."); }
  if (act === "send-now") return sendFlow(id, null);
  if (act === "schedule") {
    const when = $("#detail [name=send_at]").value;
    if (!when) throw new Error("Pick a date and time first.");
    return sendFlow(id, when);
  }
  if (act === "invited") { await api(`${base}/drafts/${id}/mark-invited`, { method: "POST" }); toast("Invitation recorded; RSVP follow-up is now possible."); }
  await refresh(); await renderDetail(id);
}));
$("#detail").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const f = e.target;
  await api(`/campaigns/${state.cid}/drafts/${state.focus}`, { method: "PATCH", body: { subject: f.subject.value, body: f.body.value } });
  toast("Saved. Review it again, then approve.");
  await refresh(); await renderDetail(state.focus);
}));

// ---------------------------------------------------------------- sending (opt-in)
const LIVE = ["scheduled", "sending", "uncertain", "sent", "bounced", "replied"];
const liveSend = (id) => (state.sends || []).some((s) => s.candidate_id === id && LIVE.includes(s.status));

function renderSending(st, sends, view) {
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

async function sendFlow(id, when) {
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
