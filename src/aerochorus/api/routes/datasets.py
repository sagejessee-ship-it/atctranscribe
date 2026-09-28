from fastapi import APIRouter, Query
from sqlalchemy import select

from aerochorus.api import datasets as svc
from aerochorus.api.deps import SessionDep
from aerochorus.api.routes.sweeps import _call
from aerochorus.dataset_contracts import (
    DatasetCreate,
    DatasetItem,
    DatasetView,
    ExportReport,
    TrainingSummary,
)
from aerochorus.db.models import TrainingDataset

router = APIRouter(tags=["training"])


@router.post("/datasets", response_model=DatasetView, status_code=201)
def create_dataset(body: DatasetCreate, session: SessionDep) -> DatasetView:
    """Freeze the current matching annotations into a new dataset version."""
    return _call(session, lambda: svc.create_dataset(session, body))


@router.get("/datasets", response_model=list[DatasetView])
def list_datasets(session: SessionDep, limit: int = Query(50, ge=1, le=500)) -> list[DatasetView]:
    rows = session.scalars(
        select(TrainingDataset)
        .order_by(TrainingDataset.name, TrainingDataset.version.desc())
        .limit(limit)
    )
    return [svc.dataset_view(d) for d in rows]


@router.get("/datasets/{dataset_id}", response_model=DatasetView)
def get_dataset(dataset_id: int, session: SessionDep) -> DatasetView:
    return svc.dataset_view(_call(session, lambda: svc.load_dataset(session, dataset_id)))


@router.get("/datasets/{dataset_id}/items", response_model=list[DatasetItem])
def dataset_items(
    dataset_id: int,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=5000),
) -> list[DatasetItem]:
    return _call(
        session, lambda: svc.dataset_items(session, dataset_id, offset, limit), commit=False
    )


@router.post("/datasets/{dataset_id}/export", response_model=DatasetView)
def record_export(dataset_id: int, body: ExportReport, session: SessionDep) -> DatasetView:
    """Called by `aerochorus dataset export` after clips are materialized."""
    return _call(session, lambda: svc.record_export(session, dataset_id, body))


@router.get("/training/summary", response_model=TrainingSummary)
def training_summary(session: SessionDep) -> TrainingSummary:
    return svc.training_summary(session)
