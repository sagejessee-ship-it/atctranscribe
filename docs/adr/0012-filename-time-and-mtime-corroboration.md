# ADR-012: Filename wall-clock time, corroborated by mtime

Status: Accepted (2026-09-26)

## Context

The collector is RTLSDR-Airband in `split_on_transmission` mode. It names each
transmission `<STATION>_<POSITION>_<YYYYMMDD>_<HHMMSS>_<frequency Hz>.mp3`
inside `YYYY/MM/DD/` directories. A profile of all 447,967 archive files
(2026-09-26, see [../corpus/home_atc_archive.md](../corpus/home_atc_archive.md))
showed:

- the filename time is **local America/New_York time**, not UTC (collector
  `localtime = true`). Every file's mtime falls after its filename time only
  under that interpretation (typically about 1 s after start + duration);
- the date directory always matches the filename date (also local).

Local time has two hazards. The DST fall-back hour happens twice, so a name
there is ambiguous. The spring-forward hour never happens, so a name there
cannot be valid.

## Decision

- The source's adapter config names the filename convention
  (`filename_parser = rtlsdr_airband`) and the collector's clock
  (`filename_timezone = America/New_York`). If a parser is configured, the
  timezone is mandatory, and AeroChorus never guesses it.
- The file mtime is independent corroborating evidence. The collector writes
  it when it closes the segment, so it should sit at `start + duration`.
  Agreement within `mtime_tolerance_seconds` (default 120 s, capped below
  30 min) yields `resolved`. Disagreement yields `unverified`.
- In a DST fold, mtime picks the occurrence. If it cannot, the segment is
  `ambiguous` and has no UTC.
- The evidence (method, zone, wall-clock time, UTC offset, mtime delta, fold
  candidates) is stored in `segment.metadata.temporal`.

## Consequences

- If the collector is ever reconfigured to UTC filenames without updating the
  source config, new segments become `unverified` (mtime off by hours). The
  change is detected rather than silently mis-timed.
- Copying the archive in a way that resets mtimes would downgrade segments to
  `unverified`. It would not corrupt their times.
- One known archive file already shows the disagreement case: `2026/07/18/`
  `BWI_TWR_20260718_141532_119400000.mp3` has an mtime 9.5 h after its
  filename time.
