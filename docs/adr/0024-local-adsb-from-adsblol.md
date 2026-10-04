# ADR-024: Airport-area ADS-B imported per day from adsb.lol

Status: Accepted (2026-10-04). Amends [ADR-020](0020-on-demand-cached-adsb-context.md).

## Context

ADR-020 chose on-demand OpenSky queries and ruled out a local ADS-B store. In
practice OpenSky gave the map almost nothing:

- **Trino** (historical state vectors) still answers "Access Denied". Access
  needs OpenSky's approval, which has not come.
- **REST** is the only path left, and it is a poor fit:
  - it covers arrivals and departures of one airport only, so no overflights,
    satellite airports or helicopters;
  - flight tracks are served only for the last 30 days, while most of the
    archive is older;
  - its waypoints are sparse;
  - each call costs 30 of 4,000 daily credits, about a dozen new segment
    fetches a day.
- After a week the reviewer had not seen a single aircraft on the map.

adsb.lol publishes every trace its receiver network saw on a UTC day as a
GitHub release (`adsblol/globe_history_<year>`), openly licensed (ODbL 1.0).
A day is ~3.5 GB, a split tar of readsb `trace_full` files (one gzip JSON per
aircraft, a position every few seconds). Measured on 2026-09-26 around KBWI:
~3,200 aircraft within 40 nm, and ~263,000 positions within 25 nm.

## Decision

- **Import per day, only on request.** Someone presses *Import* in the ADS-B
  panel, or runs `aerochorus adsb import <day> [--to <day>]`. Nothing is
  downloaded otherwise, and there is no automatic backfill.
- **Keep only the airport area.** The default is 25 nm around the configured
  airport's reference point (`AEROCHORUS_ADSB_AIRPORT`,
  `AEROCHORUS_ADSB_IMPORT_RADIUS_NM`), about 20 MB a day. The tar is streamed
  and nothing is written to disk. A text pre-filter on integer degrees skips
  the ~97 % of aircraft that never come near. On real data it found exactly
  what a full parse did, 3.7× faster, so a day costs about its download time
  (a few minutes).
- **Separate importer process.** The API starts
  `python -m aerochorus.api.adsb_local` on its own database, so parsing never
  slows the API or the worker's requests. A PostgreSQL advisory lock allows one
  importer at a time. Queued days survive restarts: the next status read that
  sees one starts the importer again.
- **Storage.**
  - `adsb_day` holds the status, progress, release tag, area and counts.
  - `adsb_position` is a plain table written in time order, with a BRIN index
    on `t`.
  - `adsb_aircraft` holds registration and type.
  - A day is all-or-nothing: its positions appear together with `ready`.
    Re-importing replaces the day.
- **Same snapshot model.** A segment fetch still builds the bounded query of
  ADR-020 (window, box, radius) and stores a `context_snapshot`. It now reads
  the local store, so it is instant and free. Once a day is imported, the panel
  fetches as soon as it is opened.
- **Attribution.** The source names the release, and `provider_meta.license`
  carries "ODbL 1.0, adsb.lol contributors", both shown in the panel.
- **Provider selection.** `AEROCHORUS_ADSB_PROVIDER=auto` (the default) uses
  the local store. `opensky`, `trino` and `rest` restore the OpenSky providers,
  which are unchanged.

## Consequences

- Every aircraft the adsb.lol network saw appears on the map for any imported
  day: arrivals, departures, overflights and helicopters, with trails.
- An archive of ~80 days is ~280 GB to download and ~1.6 GB to keep. Importing
  everything is a deliberate, explicit action (overnight with the CLI).
- adsb.lol publishes a day after it ends, so today's segments have no traffic
  until tomorrow.
- Coverage depends on adsb.lol's feeders. The Baltimore/Washington area is well
  covered.
- ADR-020's "no ADS-B archive" no longer holds. What remains is the principle:
  explicit, bounded, cached and reproducible.
