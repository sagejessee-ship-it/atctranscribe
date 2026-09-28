"""Evaluation endpoints: the only HTTP surface that returns gold text."""

from fastapi import APIRouter, Query

from aerochorus.api import evaluation as ev
from aerochorus.api.deps import SessionDep
from aerochorus.api.routes.sweeps import _call
from aerochorus.eval_contracts import (
    EvalReport,
    EvalSegmentRow,
    ReferenceImport,
    ReferenceImportResult,
)

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


@router.post("/references", response_model=ReferenceImportResult)
def import_references(body: ReferenceImport, session: SessionDep) -> ReferenceImportResult:
    return _call(session, lambda: ev.import_references(session, body))


@router.get("/sweeps/{sweep_id}", response_model=EvalReport)
def evaluate(
    sweep_id: int,
    session: SessionDep,
    canonical_numbers: bool = False,
    english_only: bool = False,
    split: str | None = None,
) -> EvalReport:
    return _call(
        session,
        lambda: ev.evaluate_sweep(
            session,
            sweep_id,
            canonical_numbers=canonical_numbers,
            english_only=english_only,
            split=split,
        ),
        commit=False,
    )


@router.get("/sweeps/{sweep_id}/segments", response_model=list[EvalSegmentRow])
def segments(
    sweep_id: int,
    session: SessionDep,
    model: str | None = None,
    limit: int = Query(10, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    canonical_numbers: bool = False,
) -> list[EvalSegmentRow]:
    return _call(
        session,
        lambda: ev.evaluation_segments(
            session,
            sweep_id,
            model=model,
            limit=limit,
            offset=offset,
            canonical_numbers=canonical_numbers,
        ),
        commit=False,
    )
