"""Route groups. Drop a module here that defines `routes(ctx) -> APIRouter` and it is mounted automatically,
so new endpoint groups (assets, sequences, Gmail, ...) never need an edit to app/api.py.

`ctx` (see Ctx) carries what routes need from the app: config, the per-request service getter `svc()`, the
`Svc` dependency, and in remote mode the account store, service pool and OAuth states.
"""

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


@dataclass
class Ctx:
    cfg: Any
    state: dict
    svc: Any  # () -> CampaignService for this request
    Svc: Any  # Annotated[CampaignService, Depends(...)]
    pool: Any = None
    oauth: Any = None

    def remote_user(self, request):
        if not self.cfg.remote:
            raise HTTPException(404)
        return request.state.user["user_id"]


def mount(app, ctx):
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if not info.name.startswith("_") and info.name != "pool":
            mod = importlib.import_module(f"{__name__}.{info.name}")
            if hasattr(mod, "routes"):
                app.include_router(mod.routes(ctx))
