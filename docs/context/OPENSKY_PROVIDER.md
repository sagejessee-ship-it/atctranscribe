# OpenSky ADS-B context provider

ADS-B context helps an annotator check a callsign or an aircraft's state near
the moment of a transmission. It is **context only**: it never changes a
transcript or a label. It is fetched **on demand**, never in bulk. The decision
is recorded in [ADR-020](../adr/0020-on-demand-cached-adsb-context.md).

## Why Trino, not `/states/all`

OpenSky's REST `/states/all` is effectively current-only, and authenticated
REST state vectors reach back only about an hour. Archived segments need the
**historical Trino interface**: table `minio.osky.state_vectors_data4`, which
has unlimited retention since 2013. AeroChorus never calls `/states/all`, and
a unit test guards that.

References:

- <https://openskynetwork.github.io/opensky-api/trino.html>
- <https://openskynetwork.github.io/opensky-api/rest.html>
- <https://opensky-network.org/data/trino>

## Access

| item | value |
| --- | --- |
| Trino | `https://trino.opensky-network.org` (REST statement protocol), catalog `minio`, schema `osky` |
| Auth | Password-grant token from `https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token` (`client_id=trino-client`), sent as `Authorization: Bearer …`. `X-Trino-User` is the account **username** from the token's `preferred_username` claim, so you may log in with your e-mail address. |
| Credentials | `AEROCHORUS_OPENSKY_USERNAME` (username or e-mail), `AEROCHORUS_OPENSKY_PASSWORD` in the control plane's environment. Put them in `./.env` (gitignored); Compose passes them to the `api` service. Recreate the container after changing them (`docker compose up -d api`). |
| Browser | Never sees credentials. The flow is always browser → AeroChorus API → OpenSky. The password is a `SecretStr` and is never serialized; a test covers this. |

Without credentials, the ADS-B panel says "unavailable: not configured" and
everything else works as before.

**Trino access must be granted by OpenSky.** A valid login is not enough:
until OpenSky enables historical access for the account, Trino answers
`Access Denied: Cannot execute query`, and the panel shows that message.
Request access at <https://opensky-network.org/data/trino>. As of 2026-09-27
the configured account logs in successfully but is still waiting for access.

### REST fallback (API client credentials)

The OpenSky REST API accepts only **OAuth2 client credentials**; basic auth
was removed. Create an API client on your OpenSky account page ("API
client"), then set:

```bash
AEROCHORUS_OPENSKY_CLIENT_ID=...
AEROCHORUS_OPENSKY_CLIENT_SECRET=...
```

Tokens come from the same token endpoint (`grant_type=client_credentials`)
and last 30 minutes. REST has no historical state vectors, so the REST
provider reconstructs positions at the segment time:

1. `/flights/arrival` and `/flights/departure` for the airport, over bounded
   windows around the segment. These come from OpenSky's nightly batch, so
   only days before today.
2. Keep flights active within ±10 minutes.
3. Fetch `/tracks/all` for at most `AEROCHORUS_ADSB_REST_MAX_TRACKS` of them
   (default 8). Tracks reach back 30 days.
4. Interpolate each track at the segment time. Nearby waypoints become the
   trail.

The metadata records `interpolated: true`. A standard account has 4000 REST
credits per day, and a flights or track call costs about 30, so the track
cap matters.

### Provider selection

`AEROCHORUS_ADSB_PROVIDER`:

| value | behaviour |
| --- | --- |
| `auto` (default) | Trino when it works. If Trino is unavailable (access not granted, rejected), falls back to REST when client credentials are set. The snapshot's metadata records the provider used and why it fell back. |
| `trino` | Trino only |
| `rest` | REST only |

## Query model

When you press **Fetch ADS-B context** on a segment, the provider sends one
query:

```sql
SELECT time, icao24, callsign, lat, lon, baroaltitude, geoaltitude, velocity, heading, vertrate,
       onground, squawk, lastcontact
FROM minio.osky.state_vectors_data4
WHERE hour IN (<every hour partition the window touches>)      -- required partition filter
  AND time BETWEEN <utc − before> AND <utc + after>              -- exact window
  AND lat BETWEEN <lat ± r/60> AND lon BETWEEN <lon ± r/(60·cos lat)>
  AND time - lastcontact <= 15                                   -- no stale vectors
ORDER BY time LIMIT 20000
```

| setting | default | hard limit |
| --- | --- | --- |
| `AEROCHORUS_ADSB_WINDOW_BEFORE_S` / `_AFTER_S` | 60 / 60 s | total ≤ 600 s |
| `AEROCHORUS_ADSB_RADIUS_NM` | 10 nm around the airport reference point (from the FAA profile) | ≤ 40 nm |
| `AEROCHORUS_OPENSKY_TIMEOUT_S` | 120 s | |

The rules:

- The segment must have an absolute UTC time (ADR-010).
- Its airport must have a reference point.
- Only validated numbers are interpolated into the SQL.

## Snapshot cache

Every fetch stores one `context_snapshot` row, holding:

| field | contents |
| --- | --- |
| `segment_id`, `provider`, `source` | which segment, and where the data came from |
| `query` | the effective parameters, including the exact SQL, airport and hours |
| `query_sha256` | hash of the query |
| `t_start`, `t_end`, `radius_nm` | the window and radius |
| `raw` | columns + rows |
| `response_sha256` | hash of the response |
| `summary` | nearest state vector per aircraft, in aviation units |
| `provider_meta` | Trino query id, state, processed bytes, elapsed time |
| `fetched_at` | when it was fetched |

Reopening a segment shows the cached snapshot **without** an external call.
**Refresh context** performs a new query and stores a new snapshot; older
snapshots are kept.

## API

| call | behaviour |
| --- | --- |
| `GET /api/v1/context/adsb/{segment_id}` | Whether it is configured, the window and radius, and the latest cached snapshot. Never contacts OpenSky. |
| `POST /api/v1/context/adsb/{segment_id}` | Returns the cached snapshot if one exists; otherwise queries once and stores the result. |
| `POST …?refresh=true` | Always queries, and stores a new snapshot. |

Errors:

- 503: not configured, or credentials rejected;
- 502: the provider failed; nothing is stored, and annotations are untouched;
- 422: no UTC, or no airport reference point.

## UI

The **ADS-B context** section in the inspector is collapsed, and shows
"on demand", "cached" or "unavailable". When expanded it shows a compact
table sorted by |Δt|:

- callsign, ICAO24, Δt from segment start, distance (nm);
- lat/lon, baro altitude (ft), heading, speed (kt), vertical rate (fpm);
- on-ground.

Below the table: a provenance line (source table, window, radius, hours,
vector count, response hash, snapshot id, fetch time) and **Refresh context**.

There is no map. A map is future work, as is bulk ADS-B storage.

## Tests

| file | covers |
| --- | --- |
| `tests/unit/test_opensky.py` | the query stays bounded in time, space and partitions; broad scans are refused; token form fields; lowercase user; Bearer, catalog and schema headers; `nextUri` paging; token reuse; explicit errors; the summary; no `/states/all` |
| `tests/integration/test_context.py` | not configured, so review still works; fetch only on request; cached reopen; explicit refresh; provider failure stores nothing and loses no annotation; UTC and airport required; credentials never appear in responses |
| `ui/e2e/adsb.spec.ts` | the browser flow against a fake provider (call counter proves caching), and the unconfigured stack |

**Not verified against the live OpenSky service.** No credentials were
available while this was built. The protocol follows OpenSky's docs and the
pyopensky client. Before relying on it, try one segment with real
credentials: set them in `.env`, run `docker compose up -d`, and press
Fetch.
