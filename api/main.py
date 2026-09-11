import logging
import os

from fastapi import FastAPI

from infra.runtime import init_runtime
from infra.logging_config import configure_logging
from api.routes import health, rag
from api.routes.msgchannels import telegram, whatsapp, webchat


def create_app() -> FastAPI:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(project_root, ".env")
        if not os.path.exists(env_path):
            env_path = os.path.join(os.path.dirname(project_root), ".env")
        load_dotenv(env_path, override=True)
    except ModuleNotFoundError:
        pass
    configure_logging(project_root=project_root)
    init_runtime(project_root)

    app = FastAPI(title="CronoCRM Agent Service")

    # mem/db.sqlite3 is a CACHE of the model's working memory - every message
    # and every inquiry is already on SQL Server. Nothing in agno expires a
    # session, so without this it grows for the life of the deployment and keeps
    # parents' contact details in an unencrypted file indefinitely. Swept at
    # startup: a restart is frequent enough for a file this size, and it costs a
    # single DELETE. Never fatal.
    @app.on_event("startup")
    def _sweep_session_cache() -> None:
        try:
            from core.manager import MEM_DIR
            from infra.session_retention import sweep_sessions
            sweep_sessions(str(MEM_DIR / "db.sqlite3"))
        except Exception:
            logging.getLogger(__name__).warning(
                "session cache sweep failed - continuing", exc_info=True)

    # The webchat widget calls cross-origin from the browser; other channels
    # are server-to-server and need no CORS.
    #
    # The allow-list is read from the REGISTRY on every request, not captured
    # here at startup. Under SaaS each organisation brings its own website
    # origin in tenants.json, and onboarding is advertised as "append one org
    # + reload, no restart" - a list frozen at boot would keep that promise
    # for routing and quietly break it for the browser, so a newly onboarded
    # school's chat would fail CORS until someone bounced the container.
    # (A single-org deployment's WEBCHAT_ALLOWED_ORIGINS reaches the same
    # place: it is the default tenant's own webchat_origins.)
    from starlette.middleware.cors import CORSMiddleware

    class RegistryCORSMiddleware(CORSMiddleware):
        def is_allowed_origin(self, origin: str) -> bool:
            from infra.tenants import all_webchat_origins
            allowed = all_webchat_origins()
            # "*" keeps working - it is what a local dev .env carries. The
            # reply still names the requesting origin rather than echoing
            # "*", which is the same permission and survives credentials.
            return '*' in allowed or origin in allowed

    app.add_middleware(
        RegistryCORSMiddleware,
        allow_origins=[],          # never "*": the check above is the list
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    app.include_router(health.router)
    app.include_router(rag.router)
    app.include_router(telegram.router)
    app.include_router(whatsapp.router)
    app.include_router(webchat.router)
    return app


app = create_app()
