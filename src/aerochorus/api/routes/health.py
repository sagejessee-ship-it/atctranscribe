from fastapi import APIRouter, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from aerochorus import __version__
from aerochorus.contracts import HealthRead
from aerochorus.db.migrate import current_revision, head_revision

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthRead)
def health(request: Request, response: Response) -> HealthRead:
    head = head_revision()
    database: dict = {"reachable": False, "revision": None, "head": head}
    try:
        database["revision"] = current_revision(request.app.state.engine)
        database["reachable"] = True
    except SQLAlchemyError as exc:
        database["error"] = type(exc).__name__
    database["migrations_current"] = database["revision"] == head

    ok = database["reachable"] and database["migrations_current"]
    if not ok:
        response.status_code = 503
    return HealthRead(status="ok" if ok else "degraded", version=__version__, database=database)
