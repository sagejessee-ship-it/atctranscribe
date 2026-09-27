from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from aerochorus.api.deps import SessionDep
from aerochorus.contracts import WorkerHeartbeat, WorkerRead
from aerochorus.db.models import Worker

router = APIRouter(tags=["workers"])


def worker_read(worker: Worker) -> WorkerRead:
    return WorkerRead(
        name=worker.name,
        hostname=worker.hostname,
        platform=worker.platform,
        version=worker.version,
        health=worker.health,
        last_heartbeat_at=worker.last_heartbeat_at,
    )


@router.post("/workers/heartbeat", response_model=WorkerRead)
def heartbeat(body: WorkerHeartbeat, session: SessionDep) -> WorkerRead:
    values = body.model_dump() | {"last_heartbeat_at": datetime.now(UTC)}
    session.execute(
        pg_insert(Worker)
        .values(**values)
        .on_conflict_do_update(index_elements=["name"], set_=values)
    )
    session.commit()
    worker = session.scalar(select(Worker).where(Worker.name == body.name))
    return worker_read(worker)


@router.get("/workers", response_model=list[WorkerRead])
def list_workers(session: SessionDep) -> list[WorkerRead]:
    return [worker_read(w) for w in session.scalars(select(Worker).order_by(Worker.name))]
