// Top-level navigation. Screens register themselves in `routes`; every [data-go] link resolves here.
import { $, esc, state, api, run, show } from "./core.js";

export const routes = {};

export async function go(name) {
  if (name === "home") { state.cid = null; $("#campaign-select").value = ""; return show("home"); }
  const open = routes[name];
  if (open) await open();
}

export async function loadCampaignList(select) {
  const list = await api("/campaigns");
  $("#campaign-select").innerHTML = '<option value="">New campaign</option>' +
    list.map((c) => `<option value="${esc(c.campaign_id)}">${esc((c.name || c.request || c.campaign_id).slice(0, 48))}</option>`).join("");
  $("#campaign-select").value = select && list.some((c) => c.campaign_id === select) ? select : "";
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-go]"); if (!a) return;
  e.preventDefault(); run(() => go(a.dataset.go))();
});
