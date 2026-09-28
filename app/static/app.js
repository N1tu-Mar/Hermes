// HERMES front end entry. Talks only to /api with the app token; never touches files or keys.
// Feature modules are self-hosted under /static/features/ (script-src 'self'; no inline script needed).
import "./features/nav.js";
import "./features/intake.js";
import "./features/workspace.js";
import "./features/sending.js";
import "./features/batch.js";
import "./features/library.js";
import "./features/analytics.js";
import "./features/notifications.js";
import "./features/campaigns.js";
import "./features/contacts.js";
import "./features/identities.js";
import { boot } from "./features/auth.js";

boot();
