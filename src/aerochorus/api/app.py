from fastapi import FastAPI

from aerochorus import __version__
from aerochorus.api.routes import health, scans, sources, workers
from aerochorus.db.session import make_engine, make_sessionmaker
from aerochorus.settings import ControlPlaneSettings, get_settings


def create_app(settings: ControlPlaneSettings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="AeroChorus", version=__version__)
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)

    app.include_router(health.router)
    app.include_router(sources.router, prefix="/api/v1")
    app.include_router(scans.router, prefix="/api/v1")
    app.include_router(workers.router, prefix="/api/v1")
    return app
