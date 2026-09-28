// CSV export download and import (preview first, then commit) shared by Contacts and campaign candidates.
import { esc, api, toast, run } from "./core.js";

export async function download(path, filename) {
  const text = await api(path);
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  Object.assign(document.createElement("a"), { href: url, download: filename }).click();
  URL.revokeObjectURL(url);
}

// Renders a file picker into `panel` (toggling it visible); `path` takes {csv, commit}; `done` runs after a commit.
export function openImport(panel, { path, done, hint }) {
  panel.hidden = false;
  panel.innerHTML = `<h3>Import CSV</h3><p class="note">${esc(hint)} Nothing is saved until you confirm the preview.</p>
    <label>CSV file<input type="file" name="csv" accept=".csv,text/csv"></label><div class="import-preview"></div>
    <div class="actions"><button type="button" class="ghost" data-act="cancel">Close</button>
      <button type="button" class="primary" data-act="commit" disabled>Import valid rows</button></div>`;
  const preview = panel.querySelector(".import-preview"), commit = panel.querySelector("[data-act=commit]");
  let text = "";
  panel.querySelector("input").onchange = run(async (e) => {
    const file = e.target.files[0]; commit.disabled = true; if (!file) return;
    if (file.size > 2 * 1024 * 1024) throw new Error("CSV is larger than 2 MB.");
    text = await file.text();
    const r = await api(path, { method: "POST", body: { csv: text, commit: false } });
    preview.innerHTML = `<p><b>${r.valid.length}</b> row(s) ready, <b>${r.invalid.length}</b> skipped.</p>` +
      (r.invalid.length ? `<ul class="flags">${r.invalid.slice(0, 10).map((x) => `<li>Row ${esc(x.row)}: ${esc(x.errors.join("; "))}</li>`).join("")}</ul>` : "");
    commit.disabled = !r.valid.length;
  });
  panel.onclick = run(async (e) => {
    const act = e.target.dataset.act;
    if (act === "cancel") { panel.hidden = true; panel.innerHTML = ""; }
    if (act === "commit") {
      const r = await api(path, { method: "POST", body: { csv: text, commit: true } });
      toast(`Imported ${r.results.length} row(s).`);
      panel.hidden = true; panel.innerHTML = ""; await done();
    }
  });
  panel.querySelector("input").focus();
}
