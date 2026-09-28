// One reusable modal for "type something" (rename) and "type the id to confirm" (delete) flows.
import { $ } from "./core.js";

// -> the entered text, or null when cancelled. `expect` makes Confirm available only on an exact match.
export function askText({ title, help = "", label, value = "", expect = null, confirm = "Save", danger = false }) {
  const dlg = $("#input-dialog"), input = $("#input-dialog-field"), ok = $("#input-dialog-ok");
  $("#input-dialog-title").textContent = title;
  $("#input-dialog-help").textContent = help;
  $("#input-dialog-label").firstChild.textContent = label;
  input.value = value; ok.textContent = confirm; ok.classList.toggle("stop", danger);
  const sync = () => { ok.disabled = expect != null ? input.value !== expect : !input.value.trim(); };
  sync(); input.oninput = sync;
  dlg.returnValue = "";
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok" ? input.value : null), { once: true });
    dlg.showModal(); input.focus(); input.select();
  });
}
