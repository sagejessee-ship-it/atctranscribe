from fastapi import FastAPI

from aerochorus import __version__
from aerochorus.api.adjudication import PricingBook, RunnerRegistry
from aerochorus.api.context import make_provider
from aerochorus.api.routes import (
    adjudication,
    context,
    datasets,
    evaluation,
    health,
    review,
    scans,
    sources,
    sweeps,
    workers,
)
from aerochorus.db.session import make_engine, make_sessionmaker
from aerochorus.settings import ControlPlaneSettings, get_settings


def create_app(settings: ControlPlaneSettings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="AeroChorus", version=__version__)
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)
    # None when OpenSky credentials are absent: review works, ADS-B says "not configured".
    app.state.adsb_provider = make_provider(settings)
    # Adjudication: public OpenRouter prices (no key here) and runners seen polling.
    app.state.pricing_book = PricingBook(settings)
    app.state.adjudication_runners = RunnerRegistry()

    app.include_router(health.router)
    app.include_router(sources.router, prefix="/api/v1")
    app.include_router(scans.router, prefix="/api/v1")
    app.include_router(workers.router, prefix="/api/v1")
    app.include_router(sweeps.router, prefix="/api/v1")
    app.include_router(evaluation.router, prefix="/api/v1")
    app.include_router(review.router, prefix="/api/v1")
    app.include_router(datasets.router, prefix="/api/v1")
    app.include_router(context.router, prefix="/api/v1")
    app.include_router(adjudication.router, prefix="/api/v1")
    return app
