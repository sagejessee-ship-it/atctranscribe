# ADR-013: Segment lifecycle and incremental scanning

Status: Accepted (2026-09-26)

## Context

The archive grows by about 5,000 files a day across about 100 day directories.
It is reached over SMB, which may disconnect, and the collector may still be
writing the newest files. Network unavailability must never be confused with
deleted audio. A rescan must never create duplicates.

## Decision

**Identity and idempotency.** `UNIQUE (source_id, relative_path)`. Rescans
update rows and never insert duplicates.

**Segments are never deleted.** Two independent states:

- `presence_status`: `present` | `missing`. A segment becomes `missing` only
  when its directory was listed **completely** during a running scan and the
  file was absent. It returns to `present` if it reappears.
- `integrity_status`: `ok` | `changed`. If a re-read hash differs from the
  stored one, the segment is flagged `changed` and the previous facts go into
  `metadata.integrity_history`.

**Availability is checked, not assumed.** Before and during a scan, the reader
checks that the root exists, is a non-empty directory (an unmounted mount point
is empty), and that the configured `sentinel_paths` exist. If the source is
unavailable, or disappears mid-scan, the scan ends as `source_unavailable` and
nothing is marked missing.

**Settling.** Files, and directories, modified within `min_file_age_seconds`
(default 120 s) count as possibly still being written. Such files are not read
yet. Their directory is not recorded as settled, so the next scan revisits it.

**Scan modes.**

| mode | directories | files |
| --- | --- | --- |
| `incremental` | skip directories whose mtime equals their last *settled* listing | read new or stat-changed files |
| `full` | list every directory | read new or stat-changed files |
| `verify` | list every directory | re-read and re-hash every file |

An unchanged directory mtime means no entries were added, removed or renamed,
so incremental scans stay cheap. In-place content edits don't change the
directory mtime; `full` (stat) and `verify` (hash) scans catch those. The
worker loop runs incremental scans every 15 minutes and a full scan daily.

**One scan at a time per source.** A partial unique index enforces it. A
running scan with no progress for `scan_stale_after_seconds` is marked
`abandoned` when the next scan starts.

## Consequences

- An interrupted scan loses nothing. Committed batches stay, and the next
  incremental scan re-lists any directory that was not finished.
- Files in a directory that vanished entirely are not marked missing. That is
  the conservative choice, and it should be revisited when the UI shows
  coverage.
