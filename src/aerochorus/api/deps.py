from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from aerochorus.db.models import CorpusSource


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def load_source(session: Session, logical_key: str) -> CorpusSource:
    source = session.scalar(select(CorpusSource).where(CorpusSource.logical_key == logical_key))
    if source is None:
        raise HTTPException(404, f"unknown corpus source: {logical_key}")
    return source
