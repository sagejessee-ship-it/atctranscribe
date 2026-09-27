# ADR-020: On-demand, cached ADS-B context instead of bulk ingestion

Status: Accepted (2026-09-27)

## Context

While reviewing, an annotator sometimes wants to know which aircraft were
near the airport at the time of a transmission, to check a callsign or a
climb/descent. The corpus spans months. Mirroring OpenSky state vectors for
that period would add a large, separately managed store and heavy
ingestion. It would also couple the transcription system to an external
dataset it does not otherwise need.

OpenSky's REST `/states/all` cannot answer historical questions: anonymous
access is current-only, and authenticated access reaches back only about an
hour. Historical state vectors are served by its Trino interface
(`state_vectors_data4`), with per-user limits: two running queries, a 100 GB
scan limit, 30-minute queries, and a mandatory `hour` partition filter.

## Decision

- **On demand only.** A provider query runs only when a person presses Fetch
  (or Refresh) for one segment. Opening a segment or reading status never
  queries.
- **Tightly bounded.** Each query covers:
  - the segment's UTC ± a configured window (≤ 600 s in total);
  - explicit `hour` partitions;
  - a lat/lon box of a configured radius (≤ 40 nm) around the airport
    reference point from its FAA-derived profile;
  - `time - lastcontact <= 15`;
  - a row limit.
- **Cached snapshots.** Each answer is stored in `context_snapshot` with the
  effective query (SQL included), raw rows, a response hash, a per-aircraft
  summary and provider metadata. Reopening uses the cache. Refresh adds a
  new snapshot, and history is kept. Historical context is therefore
  reproducible and auditable.
- **Provider boundary.** `aerochorus.context.opensky` defines `AdsbProvider`
  and `OpenSkyTrino`. The latter speaks the Trino REST statement protocol
  directly through httpx, with an OpenSky OAuth2 password-grant token. The
  API holds an optional provider in `app.state`, and tests substitute fakes.
- **Secrets stay server-side.** Credentials are read from the environment
  (`SecretStr`) and never serialized. The browser only talks to AeroChorus.
- **Context never writes labels.** The snapshot is kept apart from
  annotations and hypotheses. A provider failure stores nothing and cannot
  affect annotations.

## Consequences

- No ADS-B archive, no map and no GIS. Each lookup costs one small Trino
  query, and repeat views are free.
- Review is fully functional without OpenSky access.
- Adding a provider later (for example ADS-B Exchange or a local receiver)
  means adding another `AdsbProvider` implementation. The UI and cache stay
  the same.
- A live query against OpenSky has not been run yet (no credentials during
  development). The first real fetch validates the auth and endpoint details
  taken from OpenSky's docs and pyopensky.
