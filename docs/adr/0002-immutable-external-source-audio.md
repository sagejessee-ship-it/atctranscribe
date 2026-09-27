# ADR-002: Source audio is immutable and external

Status: Accepted (2026-09-26)

## Context

The Linux collector (SDR → RTLSDR-Airband → segmented MP3) already works.
AeroChorus must never require it to change, and must never damage its
archive.

## Decision

- A corpus source is identified logically: `source_id = home_atc_archive`,
  `relative_path = 2026/09/08/BWI_GND_20260908_000024_121900000.mp3`.
- Where the source is mounted is **machine-local worker configuration**
  (`/Volumes/ATC` on the Mac, `\\192.168.68.84\bwi` on Windows). Absolute
  paths never become identity.
- Relative paths are `/`-separated and NFC-normalised. They never contain
  `..`, empty components, backslashes, or an absolute prefix. Both
  `aerochorus.corpus.paths` and a database CHECK enforce this.
- `corpus_source.read_only` is constrained to `true`.
- All source access goes through `ReadOnlyCorpusReader`. It lists, stats, and
  opens files with `"rb"`, and never follows symlinks.

## Consequences

- `test_source_audio_cannot_be_modified` fails the build if any code calls a
  mutating filesystem API or opens a file in a write mode.
- Integration tests scan read-only fixture trees and compare
  hash/size/mtime snapshots before and after.
- For defence in depth, mount the share read-only on the Mac and use an SMB
  account with read-only rights on the collector.
