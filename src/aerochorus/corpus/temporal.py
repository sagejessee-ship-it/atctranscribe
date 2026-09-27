"""Absolute UTC resolution for corpus segments (ADR-010).

The collector encodes a wall-clock start time in each filename. Converting it
to UTC needs the collector's timezone, which is ambiguous for one hour per year
(DST fall-back) and undefined for another (spring-forward gap). The file's
mtime, written by the collector when it closes the segment, is independent
evidence: it should sit at roughly ``start + duration``.

Rules:
* one candidate + mtime agrees   -> resolved
* one candidate + no agreement   -> unverified (UTC kept, flagged)
* fold + mtime picks exactly one -> resolved
* fold otherwise                 -> ambiguous (no UTC)
* no usable time in the name     -> unresolved (no UTC)

A timestamp is never invented from mtime alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aerochorus.contracts import TemporalStatus


@dataclass(frozen=True)
class TemporalResolution:
    status: TemporalStatus
    capture_start_utc: datetime | None = None
    capture_end_utc: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def _utc_candidates(wall_clock: datetime, zone: ZoneInfo) -> list[datetime]:
    """All UTC instants that display as ``wall_clock`` in ``zone``."""
    candidates = set()
    for fold in (0, 1):
        utc = wall_clock.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        # Round-trip check rejects wall-clock times inside a DST gap.
        if utc.astimezone(zone).replace(tzinfo=None) == wall_clock:
            candidates.add(utc)
    return sorted(candidates)


def resolve_capture_time(
    *,
    wall_clock_start: datetime | None,
    timezone_name: str | None,
    duration_ms: int | None,
    file_mtime_utc: datetime,
    tolerance_seconds: float,
) -> TemporalResolution:
    if wall_clock_start is None:
        return TemporalResolution(
            TemporalStatus.UNRESOLVED, evidence={"reason": "no_timestamp_in_name"}
        )
    if timezone_name is None:
        return TemporalResolution(
            TemporalStatus.UNRESOLVED, evidence={"reason": "timezone_not_configured"}
        )

    zone = ZoneInfo(timezone_name)
    evidence: dict[str, Any] = {
        "method": "filename_wall_clock",
        "timezone": timezone_name,
        "wall_clock_start": wall_clock_start.isoformat(),
        "tolerance_s": tolerance_seconds,
    }
    candidates = _utc_candidates(wall_clock_start, zone)
    if not candidates:
        evidence["reason"] = "nonexistent_local_time"
        return TemporalResolution(TemporalStatus.UNRESOLVED, evidence=evidence)

    duration = timedelta(milliseconds=duration_ms) if duration_ms is not None else None

    def mtime_delta(start: datetime) -> float | None:
        if duration is None:
            return None
        return (file_mtime_utc - (start + duration)).total_seconds()

    deltas = {c: mtime_delta(c) for c in candidates}
    agreeing = [c for c, d in deltas.items() if d is not None and abs(d) <= tolerance_seconds]

    if len(candidates) > 1:
        evidence["fold_candidates_utc"] = [c.isoformat() for c in candidates]
        if len(agreeing) != 1:
            evidence["reason"] = "dst_fold_not_disambiguated"
            return TemporalResolution(TemporalStatus.AMBIGUOUS, evidence=evidence)
        evidence["fold_resolved_by"] = "mtime"

    start = agreeing[0] if agreeing else candidates[0]
    delta = deltas[start]
    evidence["utc_offset"] = start.astimezone(zone).strftime("%z")
    evidence["mtime_delta_s"] = round(delta, 3) if delta is not None else None

    if agreeing:
        status = TemporalStatus.RESOLVED
    else:
        status = TemporalStatus.UNVERIFIED
        evidence["reason"] = "duration_unknown" if duration is None else "mtime_disagrees"

    return TemporalResolution(
        status=status,
        capture_start_utc=start,
        capture_end_utc=start + duration if duration is not None else None,
        evidence=evidence,
    )
