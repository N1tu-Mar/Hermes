// Campaigns screen: search, open, rename, archive/restore, duplicate, and typed-confirmation delete.
import { $, esc, when, SUBTYPE_LABEL, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { routes, loadCampaignList } from "./nav.js";
import { openCampaign } from "./workspace.js";
import { askText } from "./dialogs.js";

const filters = $("#campaign-filters"), rows = $("#campaign-rows");
let seq = 0;  // drop responses from superseded searches

async function load() {
  const n = ++seq, f = filters.elements;
  const q = new URLSearchParams({ q: f.q.value.trim(), status: f.status.value, subtype: f.subtype.value });
  const list = await api(`/campaigns?${q}`);
  if (n !== seq) return;
  loaded(rows);
  rows.innerHTML = list.map((c) => `<tr data-key="${esc(c.campaign_id)}" class="${c.archived ? "excluded" : ""}">
    <td><div class="who">${esc(c.name)}${c.archived ? ' <span class="tag">archived</span>' : ""}</div>
      <div class="org">${esc(c.error ? `unreadable: ${c.error}` : c.request)}</div></td>
    <td>${esc(SUBTYPE_LABEL[c.subtype] || c.mode || "—")}</td>
    <td>${esc(c.candidates ?? "—")}</td>
    <td class="status">${c.created_at ? esc(when(c.created_at)) : "—"}</td>
    <td><div class="row-actions">
      <button class="small" data-act="open">Open</button>
      <button class="small ghost" data-act="rename">Rename</button>
      <button class="small ghost" data-act="archive">${c.archived ? "Restore" : "Archive"}</button>
      <button class="small ghost" data-act="duplicate">Duplicate</button>
      ${c.archived ? '<button class="small stop" data-act="delete">Delete…</button>' : ""}
    </div></td></tr>`).join("") || `<tr><td colspan="5" class="empty">${f.q.value ? "No campaigns match this search." : "No campaigns here yet."}</td></tr>`;
  rows.dataset.names = JSON.stringify(Object.fromEntries(list.map((c) => [c.campaign_id, { name: c.name, archived: c.archived }])));
}

const reload = () => keepFocus($("#view-campaigns"), async () => { await load(); await loadCampaignList(state.cid); });

export async function openCampaigns() {
  show("campaigns");
  loading(rows, "Loading campaigns…");
  try { await load(); } catch (e) { failed(rows, e); }
}
routes.campaigns = openCampaigns;

let timer;
filters.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run(load), 250); });
filters.addEventListener("change", run(load));
filters.addEventListener("submit", (e) => e.preventDefault());

rows.addEventListener("click", run(async (e) => {
  const act = e.target.dataset.act, cid = e.target.closest("[data-key]")?.dataset.key; if (!act || !cid) return;
  const info = JSON.parse(rows.dataset.names || "{}")[cid] || {};
  if (act === "open") return loadCampaignList(cid).then(() => openCampaign(cid));
  if (act === "rename") {
    const name = await askText({ title: "Rename campaign", label: "Name", value: info.name, confirm: "Rename" });
    if (name == null) return;
    await api(`/campaigns/${cid}`, { method: "PATCH", body: { name: name.trim() } }); toast("Renamed.");
  }
  if (act === "archive") {
    await api(`/campaigns/${cid}`, { method: "PATCH", body: { archived: !info.archived } });
    toast(info.archived ? "Restored." : "Archived. Running jobs were stopped.");
  }
  if (act === "duplicate") {
    await api(`/campaigns/${cid}/duplicate`, { method: "POST", body: {} });
    toast("Duplicated settings only: no people, research, or drafts were copied.");
  }
  if (act === "delete") {
    const typed = await askText({ title: `Delete “${info.name}”?`, danger: true, confirm: "Delete permanently", expect: cid,
      label: `Type ${cid} to confirm`,
      help: "Permanently removes this campaign's people, research, drafts, and activity. Contacts and their history are kept. This cannot be undone." });
    if (typed == null) return;
    await api(`/campaigns/${cid}/delete`, { method: "POST", body: { confirm: typed } });
    if (state.cid === cid) state.cid = null;
    toast("Campaign deleted.");
  }
  await reload();
}));
