import os

from fastapi import FastAPI

from infra.runtime import init_runtime
from infra.logging_config import configure_logging
from api.routes import health
from api.routes.msgchannels import telegram, whatsapp


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
    app.include_router(health.router)
    app.include_router(telegram.router)
    app.include_router(whatsapp.router)
    return app


app = create_app()
