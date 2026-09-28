import { $, $$, esc, safeUrl, LIST, SUBTYPE_LABEL, OUTCOMES, label, day, when, AUTH, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { renderSending, liveSend, sendFlow } from "./sending.js";

// ---------------------------------------------------------------- workspace
export async function openCampaign(cid) {
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

export function selectedTemplate() {
  const raw = $("#campaign-template")?.value || ""; if (!raw) return {};
  const [template_id, version] = raw.split("@"); return { template_id, template_version: +version };
}

export async function refresh() {
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
  renderSending(sending, sends, view);
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
    const send = c.send_status ? ` <span class="${["failed", "uncertain", "bounced"].includes(c.send_status) ? "needs" : "ok"}">· ${esc(c.send_status)}</span>` : "";
    return `<tr data-id="${esc(c.candidate_id)}" tabindex="0" aria-selected="${state.focus === c.candidate_id}" class="${c.status === "excluded" ? "excluded" : ""}">
      <td><input type="checkbox" aria-label="Select ${esc(c.name)}" ${state.selected.has(c.candidate_id) ? "checked" : ""}></td>
      <td><div class="who">${c.pinned ? "📌 " : ""}${esc(c.name)}</div><div class="org">${esc(c.role)}${c.role && c.organization ? " · " : ""}${esc(c.organization)}</div></td>
      <td class="score">${esc(c.ranking?.score ?? 0)}<small>/100</small></td>
      <td class="status ${cls}" title="${esc(c.error || "")}">${esc(statusLabel)}</td>
      <td class="status">${email}</td>
      <td class="status">${draft}${send}</td>
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

export function focusRow(id) {
  state.focus = id;
  $$("#rows tr").forEach((r) => r.setAttribute("aria-selected", r.dataset.id === id));
  run(renderDetail)(id);
}

export async function renderDetail(id) {
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
          ${["approved", "gmail_draft_created"].includes(d.status) && !liveSend(id) ? `
          <button type="button" class="small" data-act="send-now">Send now…</button>
          <input type="datetime-local" name="send_at" aria-label="Send at (sending timezone)" class="send-at">
          <button type="button" class="small" data-act="schedule">Schedule…</button>` : ""}
          ${(d.template_category === "speaker_invitation" || d.template_version.startsWith("speaker_invite") || d.template_version.startsWith("speaker_invitation")) && ["approved", "gmail_draft_created"].includes(d.status)
            ? `<button type="button" class="small ghost" data-act="invited" ${d.invited_at ? "disabled" : ""}>${d.invited_at ? "Invitation marked sent" : "I sent this invitation"}</button>` : ""}
          ${!(d.template_category === "speaker_invitation" || d.template_version.startsWith("speaker_invite") || d.template_version.startsWith("speaker_invitation")) && ["approved", "gmail_draft_created"].includes(d.status)
            ? `<button type="button" class="small ghost" data-act="contacted" ${d.invited_at ? "disabled" : ""}>${d.invited_at ? "Message marked sent" : "I sent this message"}</button>` : ""}
        </div></form>`}</section>`;
  } else if (p && p.status !== "research_failed") {
    html += `<section><button class="small" data-act="draft">Draft email</button></section>`;
  }
  html += renderOutreach(o, timeline);
  html += `<section><button class="small ghost" data-act="forget">Delete this person</button></section>`;
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
  if (act === "send-now") return sendFlow(id, null);
  if (act === "schedule") {
    const when = $("#detail [name=send_at]").value;
    if (!when) throw new Error("Pick a date and time first.");
    return sendFlow(id, when);
  }
  if (act === "forget") {
    if (!confirm("Delete this person from the campaign, including research and drafts?")) return;
    await api(`${base}/candidates/${id}`, { method: "DELETE" });
    state.focus = null; $("#detail").innerHTML = '<p class="empty">Person deleted.</p>'; return refresh();
  }
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
