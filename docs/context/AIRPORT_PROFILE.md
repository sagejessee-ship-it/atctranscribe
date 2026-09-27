# Airport profiles

Annotators see airport context next to each segment. It is context only and
never becomes gold text.

## Schema (generic, one row set per airport)

| table | contents |
| --- | --- |
| `airport` | ICAO (PK), FAA id, IATA, name, city, reference lat/lon, elevation, magnetic variation, **timezone**, `provenance` JSONB |
| `airport_runway` | one row per runway **end**: pair (`15R/33L`), end (`33L`), length, width, true alignment, spoken forms |
| `airport_frequency` | service category (TWR, GND, CD, ATIS, APP, DEP, CLASS_B, PROC, EMERG, OTHER), Hz, facility, spoken call ("Baltimore Tower"), FAA use/sectorization, spoken digit forms |
| `airport_alias` | alias + kind: `icao`, `faa`, `iata`, `station`, `name`, `spoken` |

Segment → airport resolution happens on the server. `segment.station` is
looked up in aliases of kind `station`, `faa` or `icao`. Four-letter stations
(ATCO2) are taken as ICAO codes. The channel's service line ("Baltimore Tower
(LCL/P)") is found by matching `segment.frequency_hz` against the profile.

A new airport needs **no schema change**. Bootstrap it the same way.

## KBWI bootstrap

Source: the FAA NASR 28-Day Subscription, CSV edition. It is the
authoritative airport/runway/frequency database behind the Chart Supplement.

- `<DD_Mon_YYYY>_APT_CSV.zip`: `APT_BASE`, `APT_RWY`, `APT_RWY_END`
- `<DD_Mon_YYYY>_FRQ_CSV.zip`: `FRQ`
- URL pattern: `https://nfdc.faa.gov/webContent/28DaySub/extra/{stamp}_{APT|FRQ}_CSV.zip`
- Cycles are 28 days, anchored at 2026-09-03 (`faa_nasr.cycle_for`).

```bash
# download the current cycle (cached under ~/.aerochorus/cache/faa_nasr/<cycle>/)
uv run aerochorus airport bootstrap KBWI --timezone America/New_York --station BWI
# or from archives you already have
uv run aerochorus airport bootstrap KBWI --timezone America/New_York --station BWI \
    --apt 03_Sep_2026_APT_CSV.zip --frq 03_Sep_2026_FRQ_CSV.zip --cycle 2026-09-27
uv run aerochorus airport bootstrap KBWI --timezone America/New_York --dry-run   # print only
uv run aerochorus airport show KBWI
```

The timezone must be given. NASR does not carry an IANA zone, and the parser
never guesses one. `--station` names the collector's station label (the
archive files say `BWI_…`).

Stored provenance records:

- source name and index URL;
- cycle and next cycle;
- per-file URL or name, SHA-256 and size;
- fetch time;
- the NASR `EFF_DATE`.

The UI shows source, cycle, effective date and fetch date.

**Result for the 2026-09-03 cycle** (bootstrapped 2026-09-27): runway ends
10, 28, 15L, 33R, 15R, 33L, which gives pairs **10/28, 15L/33R, 15R/33L**.
That matches the Phase 5 acceptance sanity check. There are 36 VHF voice
frequencies. The aliases are KBWI, BWI, the airport name, Baltimore, and
Baltimore Tower/Ground/Clearance Delivery and Potomac Approach/Departure.

## Spoken forms

| input | forms |
| --- | --- |
| runway `33L` | "runway three three left" |
| runway `10` | "runway one zero" |
| runway `9` | "runway niner", "runway nine" |
| 119.4 MHz | "one one niner point four", "one one nine point four", "one one niner decimal four" |

Only VHF voice frequencies (108–137 MHz) are kept. UHF military and
remark-only rows are dropped. Calls come from NASR: `TOWER_OR_COMM_CALL` for
the tower/ground/clearance family, and `PRIMARY_APPROACH_RADIO_CALL` for
TRACON services.

## Refreshing

Re-run the bootstrap in a later cycle. The upsert replaces the runway,
frequency and alias rows for that airport and updates the provenance.
