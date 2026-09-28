// Sign-in (remote mode), account panel, and application start-up.
import { savedBackground, $, $$, AUTH, TOKEN, state, api, toast, run, show } from "./core.js";
import { loadCampaignList } from "./nav.js";
import { loadNotifications } from "./notifications.js";

let notifTimer;
const pollNotifications = () => { clearInterval(notifTimer); notifTimer = setInterval(run(loadNotifications), 10000); };

function showLogin() { clearInterval(state.poll); clearInterval(notifTimer); $("#account").hidden = true; show("login"); }
AUTH.onUnauth = showLogin;

async function startRemote(session) {
  AUTH.csrf = session.csrf; AUTH.user = session.user;
  savedBackground.purge();  // origin-wide storage would otherwise carry one user's background into the next session
  $("#account").hidden = false; $("#account-name").textContent = session.user;
  const a = await api("/account");
  $("#account-state").textContent = `${a.openai_key_set ? "Your OpenAI key is set" : "No OpenAI key: demo data"} · Gmail ${a.gmail_connected ? "connected" : "not connected"}`;
  show("home"); await loadCampaignList(); await loadNotifications(); pollNotifications();
}
$("#login-form").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const f = e.target;
  const s = await api("/login", { method: "POST", body: { username: f.username.value, password: f.password.value } }, "/auth");
  f.password.value = ""; await startRemote(s);
}));
$("#btn-logout").addEventListener("click", run(async () => { await api("/logout", { method: "POST" }, "/auth"); savedBackground.purge(); location.reload(); }));
$("#openai-form").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  await api("/account/openai-key", { method: "PUT", body: { api_key: e.target.api_key.value } });
  e.target.api_key.value = ""; toast("Key saved (encrypted)."); await startRemote({ user: AUTH.user, csrf: AUTH.csrf });
}));
$("#btn-clear-key").addEventListener("click", run(async () => { await api("/account/openai-key", { method: "DELETE" }); location.reload(); }));
$("#btn-disconnect-gmail").addEventListener("click", run(async () => { await api("/account/gmail", { method: "DELETE" }); location.reload(); }));

export async function boot() {
  const res = await fetch("/auth/session");
  const session = await res.json().catch(() => ({ mode: "local" }));
  AUTH.mode = session.mode;
  if (AUTH.mode === "remote") return session.user ? run(startRemote)(session) : showLogin();
  if (!TOKEN) { $("#token-warning").hidden = false; $$(".view").forEach((v) => (v.hidden = true)); }
  else { run(loadCampaignList)(); run(loadNotifications)(); pollNotifications(); }
}
