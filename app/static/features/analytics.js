import { $, $$, esc, safeUrl, LIST, SUBTYPE_LABEL, OUTCOMES, label, day, when, AUTH, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { openCampaign, focusRow } from "./workspace.js";
import { loadCampaignList } from "./nav.js";

// ---------------------------------------------------------------- contact timeline & outcomes
const STAGE_LABEL = { discovered: "Discovered", researched: "Researched", contactable: "Verified contact", research_failed: "Research failed",
  drafted: "Drafted", approved: "Approved", scheduled: "Scheduled", sent: "Sent", replied: "Replied", interested: "Interested",
  declined: "Declined", bounced: "Bounced", meeting_booked: "Meeting booked" };

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

export async function openAnalytics() {
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
