# Corpus source: `home_atc_archive`

These are facts observed from the collector's output, not assumptions. They
were gathered from a full read-only profile of the share on 2026-09-26.

## Location

| machine | root |
| --- | --- |
| Linux collector (SMB export) | `\\192.168.68.84\bwi` |
| Windows dev box | `\\192.168.68.84\bwi` |
| M1 MacBook Air | mount read-only, e.g. `/Volumes/ATC` |

## Collector

The collector is RTLSDR-Airband with `split_on_transmission` (one file per
transmission), `include_freq`, `dated_subdirectories` and `localtime = true`.

## Layout

```
<root>/
  BWI_*_20260716_*.mp3          2,205 files from 2026-07-16, before dated
                                 subdirectories were enabled
  YYYY/MM/DD/                   local-date directories
    <STATION>_<POSITION>_<YYYYMMDD>_<HHMMSS>_<FREQ_HZ>.mp3
```

Example: `2026/09/08/BWI_GND_20260908_000024_121900000.mp3`.

- 447,967 files, all `.mp3`. Every name matches the pattern. No empty files.
  There are 66 directories and 4.81 GB in total.
- Median file 7.6 KB. The 99th percentile is 49.7 KB. The largest is 2.8 MB,
  a 16-minute stuck transmission on 2026-07-17.
- Coverage is 2026-07-16 → present, with a gap from 2026-07-29 to 2026-08-09.
  Some days are unusually large: 07-24 (47k files), 07-25 (30k), 08-13 (19k).
- There are no sidecar files. Station, position and frequency come only from
  the filename.

## Channels

| label (station_position) | frequency (Hz) | files |
| --- | --- | --- |
| BWI_GND | 121,900,000 | 238,060 |
| BWI_TWR | 119,400,000 | 209,396 |
| BWI_CLNC | 118,050,000 | 267 |
| BWI_APP_FS | 119,700,000 | 178 |
| BWI_APP_FN | 119,000,000 | 46 |
| BWI_TRSA | 123,750,000 | 11 |
| BWI_GND_ALT | 120,200,000 | 9 |

The label is split at its first underscore into `station` (`BWI`) and
`channel` (`GND`, `APP_FS`, `GND_ALT`, …).

## Audio

MPEG-2.5 Layer III, 8 kHz, mono, LAME VBR (~17–23 kbps) with a Xing header,
so durations are exact from the header.

## Time

- The filename time is the **transmission start in America/New_York local
  time**. The date directory is the same local date.
- Evidence: assuming America/New_York, `mtime − filename_start` is ≥ 0 for
  every file, and 85% fall within 10 s. Assuming UTC, all but one file would
  have closed about four hours *before* it started (the whole archive so far
  is in EDT). Traffic also peaks at 06:00–21:00 in local-time filenames.
- For the 4,913 files of 2026-09-08, `mtime − (start + duration)` averages
  1.1 s, with a maximum of 4.8 s.
- Known anomaly: `2026/07/18/BWI_TWR_20260718_141532_119400000.mp3` has an
  mtime 9.5 h after its name. It indexes as `unverified`.
- DST: 2026-11-01 01:00–02:00 local occurs twice. mtime disambiguates it
  (ADR-012).

## Source registration

```
aerochorus source add home_atc_archive --name "Home ATC archive (BWI)" \
    --parser rtlsdr_airband --timezone America/New_York --sentinel 2026
```
