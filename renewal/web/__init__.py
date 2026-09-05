"""FastAPI application. Routes parse the request and call the library; no
pipeline logic lives here.

The application itself is only the shell: static files, the error page, the
index, and the routers it mounts. Every route lives in a router module that is
handed its dependencies explicitly.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Client, Policy, RenewalRun
from renewal.web import (
    attention as attention_routes, calendar, clients as client_routes,
    comparison, mail as mail_routes, review, runs, search as search_routes,
    settings as settings_routes, unmatched,
)
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

ROUTER_MODULES = (
    runs, review, comparison, unmatched, calendar, settings_routes,
    search_routes, client_routes, attention_routes,
)


def build_inbound_provider(settings: Settings):
    """The local file-drop provider verifies nothing, so it is only ever built
    when it is asked for by name. Anything else is a configuration error rather
    than a reason to fall back to the one that trusts every caller."""
    if settings.inbound_provider == "filedrop":
        from renewal.mail.filedrop import FileDropProvider

        return FileDropProvider(Path(settings.inbound_drop_dir))
    raise ValueError(f"unknown inbound provider: {settings.inbound_provider!r}")


def create_app(
    *, settings: Settings, store: BlobStore, model_client, session_factory,
    inbound_provider=None,
):
    app = FastAPI()
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent.parent / "static")),
        name="static",
    )

    @app.exception_handler(StarletteHTTPException)
    def html_error(request: Request, exc: StarletteHTTPException):
        """Every error here lands in front of the person doing the review, not
        a client library, so it gets a page rather than a JSON blob."""
        titles = {400: "That upload was rejected", 404: "Nothing here"}
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {
                "status": exc.status_code,
                "title": titles.get(exc.status_code, "Something went wrong"),
                "detail": exc.detail,
            },
            status_code=exc.status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with session_factory() as session:
            run_rows = (
                session.query(RenewalRun, Client, Policy)
                .join(Policy, RenewalRun.policy_id == Policy.id)
                .join(Client, Policy.client_id == Client.id)
                .order_by(RenewalRun.id.desc())
                .all()
            )
            return TEMPLATES.TemplateResponse(
                request, "index.html", {"runs": run_rows}
            )

    deps = Deps(
        settings=settings,
        store=store,
        model_client=model_client,
        session_factory=session_factory,
    )
    for module in ROUTER_MODULES:
        module.register(app, deps)
    mail_routes.register(
        app, deps, inbound_provider or build_inbound_provider(settings)
    )
    return app
