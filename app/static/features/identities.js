// Sender identities screen: list, create, edit, delete. Campaign intake selects from this list.
import { savedBackground, $, esc, state, api, toast, run, keepFocus, show, loading, loaded, failed } from "./core.js";
import { routes } from "./nav.js";

const list = $("#identity-list"), form = $("#identity-form");
let items = [];

function render() {
  loaded(list);
  list.innerHTML = items.map((x) => `<article class="card" data-key="${x.id}">
    <div class="who">${esc(x.display_name)}</div><div class="org">${esc([x.role, x.organization].filter(Boolean).join(" · "))}${x.reply_to ? ` · reply-to ${esc(x.reply_to)}` : ""}</div>
    <p class="note">${esc((x.biography || "").slice(0, 160))}</p>
    <div class="row-actions"><button class="small" data-act="edit">Edit</button><button class="small ghost" data-act="delete">Delete</button></div></article>`).join("")
    || '<p class="empty">No identities yet. Create your first one on the right.</p>';
}

function fill(x) {
  form.reset();
  form.elements.id.value = x?.id ?? "";
  for (const k of ["display_name", "reply_to", "organization", "role", "biography", "signature", "default_ask"]) form.elements[k].value = x?.[k] ?? "";
  form.elements.links.value = (x?.links || []).join(", ");
  $("#identity-form-title").textContent = x ? `Edit ${x.display_name}` : "New identity";
  // Local single-user mode only: offer the background this browser saved before identities existed.
  const legacy = !x && savedBackground.get();
  $("#identity-migrate").hidden = !legacy;
  if (legacy) form.elements.biography.value = legacy;
}

const reload = () => keepFocus($("#view-identities"), async () => { items = await api("/identities"); render(); });

export async function openIdentities() {
  show("identities");
  fill(null);  // before the fetch: a slow response must not wipe what the user already typed
  loading(list, "Loading identities…");
  try { items = await api("/identities"); render(); } catch (e) { failed(list, e); }
}
routes.identities = openIdentities;

$("#identity-reset").addEventListener("click", () => fill(null));
list.addEventListener("click", run(async (e) => {
  const act = e.target.dataset.act, id = e.target.closest("[data-key]")?.dataset.key; if (!act) return;
  if (act === "edit") { fill(items.find((x) => String(x.id) === id)); form.elements.display_name.focus(); return; }
  if (act === "delete") {
    const x = items.find((i) => String(i.id) === id);
    if (!(await confirmDialog(`Delete “${x.display_name}”?`, "Campaigns using it must be switched to another identity first."))) return;
    await api(`/identities/${id}`, { method: "DELETE" }); toast("Identity deleted.");
    if (form.elements.id.value === id) fill(null);
    await reload();
  }
}));

form.addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const { id, ...body } = Object.fromEntries(new FormData(form));
  const saved = id ? await api(`/identities/${id}`, { method: "PATCH", body }) : await api("/identities", { method: "POST", body });
  toast(id ? "Identity saved." : "Identity created.");
  fill(null); await reload();
  $(`[data-key="${saved.id}"] button`, list)?.focus();
}));

function confirmDialog(title, help) {
  const dlg = $("#confirm-dialog");
  $("#confirm-dialog-title").textContent = title; $("#confirm-dialog-help").textContent = help;
  dlg.returnValue = "";
  return new Promise((resolve) => { dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }); dlg.showModal(); });
}
