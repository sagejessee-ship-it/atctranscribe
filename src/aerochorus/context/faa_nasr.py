"""FAA NASR 28-Day Subscription (CSV edition) → generic airport profile.

Source: https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/
Files: ``<DD_Mon_YYYY>_APT_CSV.zip`` (APT_BASE, APT_RWY, APT_RWY_END) and
``<DD_Mon_YYYY>_FRQ_CSV.zip`` (FRQ). Cycles are 28 days.

Everything here is pure except ``download_cycle``. Parsing never guesses
facts the data does not carry: the timezone must be given explicitly.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from aerochorus.review_contracts import (
    AirportAliasIn,
    AirportFrequencyView,
    AirportProfileIn,
    AirportRunwayView,
)

INDEX_URL = "https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/"
FILE_URL = "https://nfdc.faa.gov/webContent/28DaySub/extra/{stamp}_{name}_CSV.zip"
# A known cycle start; cycles repeat every 28 days.
ANCHOR_CYCLE = date(2026, 9, 3)
CYCLE_DAYS = 28
FILES = ("APT", "FRQ")

DIGITS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "niner",
}
SIDE = {"L": "left", "R": "right", "C": "center"}

# FAA FREQ_USE prefix -> (service category, spoken facility suffix)
SERVICES = {
    "LCL": ("TWR", "Tower"),
    "GND": ("GND", "Ground"),
    "CD": ("CD", "Clearance Delivery"),
    "D-ATIS": ("ATIS", "ATIS"),
    "ATIS": ("ATIS", "ATIS"),
    "APCH": ("APP", "Approach"),
    "DEP": ("DEP", "Departure"),
    "CLASS B": ("CLASS_B", "Approach"),
    "EMERG": ("EMERG", "Emergency"),
}


def cycle_for(day: date) -> date:
    """Start of the NASR cycle that contains ``day``."""
    return ANCHOR_CYCLE + timedelta(days=((day - ANCHOR_CYCLE).days // CYCLE_DAYS) * CYCLE_DAYS)


def cycle_stamp(cycle: date) -> str:
    return cycle.strftime("%d_%b_%Y")


def spoken_digits(text: str) -> str:
    return " ".join(DIGITS[c] for c in text if c.isdigit())


def spoken_runway(ident: str) -> list[str]:
    number = "".join(c for c in ident if c.isdigit())
    side = SIDE.get(ident[len(number) :].strip().upper(), "")
    words = spoken_digits(number)
    forms = [f"runway {words}" + (f" {side}" if side else "")]
    plain = words.replace("niner", "nine")
    if plain != words:
        forms.append(f"runway {plain}" + (f" {side}" if side else ""))
    return forms


def spoken_frequency(mhz: str) -> list[str]:
    whole, _, fraction = mhz.partition(".")
    fraction = fraction.rstrip("0") or "0"
    icao = f"{spoken_digits(whole)} point {spoken_digits(fraction)}"
    forms = [icao, icao.replace("niner", "nine"), icao.replace(" point ", " decimal ")]
    return list(dict.fromkeys(forms))


def _rows(archive: Path, member: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(archive) as zf, zf.open(member) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline="")))


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _title(value: str) -> str:
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), " ".join(value.lower().split()))


def parse_profile(
    files: dict[str, Path],
    faa_id: str,
    *,
    timezone: str,
    station_labels: tuple[str, ...] = (),
    provenance: dict[str, Any] | None = None,
) -> AirportProfileIn:
    base = next((r for r in _rows(files["APT"], "APT_BASE.csv") if r["ARPT_ID"] == faa_id), None)
    if base is None:
        raise LookupError(f"{faa_id} not found in APT_BASE.csv")
    icao = base.get("ICAO_ID") or f"K{faa_id}"

    pairs = {r["RWY_ID"]: r for r in _rows(files["APT"], "APT_RWY.csv") if r["ARPT_ID"] == faa_id}
    runways = []
    for end in sorted(
        (r for r in _rows(files["APT"], "APT_RWY_END.csv") if r["ARPT_ID"] == faa_id),
        key=lambda r: (r["RWY_ID"], r["RWY_END_ID"]),
    ):
        pair = pairs.get(end["RWY_ID"], {})
        runways.append(
            AirportRunwayView(
                pair=end["RWY_ID"],
                end_ident=end["RWY_END_ID"],
                length_ft=_int(pair.get("RWY_LEN")),
                width_ft=_int(pair.get("RWY_WIDTH")),
                true_alignment=_float(end.get("TRUE_ALIGNMENT")),
                spoken=spoken_runway(end["RWY_END_ID"]),
                latitude=_float(end.get("LAT_DECIMAL")),
                longitude=_float(end.get("LONG_DECIMAL")),
                elevation_ft=_float(end.get("RWY_END_ELEV")),
                displaced_latitude=_float(end.get("LAT_DISPLACED_THR_DECIMAL")),
                displaced_longitude=_float(end.get("LONG_DISPLACED_THR_DECIMAL")),
            )
        )

    tower_call = _title(base.get("CITY") or faa_id)
    frequencies: dict[tuple, AirportFrequencyView] = {}
    for row in _rows(files["FRQ"], "FRQ.csv"):
        if row.get("SERVICED_FACILITY") != faa_id:
            continue
        freq = _float(row.get("FREQ"))
        use = (row.get("FREQ_USE") or "").strip()
        # Voice-relevant VHF only (UHF military and remark-only rows are skipped).
        if freq is None or not 108.0 <= freq < 137.0 or use.startswith("APT REMARK"):
            continue
        prefix = next((p for p in SERVICES if use.upper().startswith(p)), None)
        if prefix is None:
            category, suffix = (
                ("PROC", "Approach") if use.upper().endswith((" STAR", " DP")) else ("OTHER", "")
            )
        else:
            category, suffix = SERVICES[prefix]
        is_tracon = category in ("APP", "DEP", "CLASS_B", "PROC")
        call_name = (
            _title(row.get("PRIMARY_APPROACH_RADIO_CALL") or "")
            if is_tracon
            else (_title(row.get("TOWER_OR_COMM_CALL") or "") or tower_call)
        )
        call = f"{call_name} {suffix}".strip() if suffix and category != "EMERG" else None
        sector = " ".join(x for x in (use, (row.get("SECTORIZATION") or "").strip()) if x)
        key = (category, round(freq * 1000), sector)
        frequencies[key] = AirportFrequencyView(
            service=category,
            frequency_hz=round(freq * 1_000_000),
            facility=row.get("FACILITY") or None,
            call=call,
            sectorization=sector or None,
            spoken=spoken_frequency(row["FREQ"]),
        )

    name = base.get("ARPT_NAME") or faa_id
    aliases = [
        AirportAliasIn(alias=icao, kind="icao"),
        AirportAliasIn(alias=faa_id, kind="faa"),
        *(AirportAliasIn(alias=s, kind="station") for s in (station_labels or (faa_id,))),
        AirportAliasIn(alias=_title(name), kind="name"),
        AirportAliasIn(alias=tower_call, kind="spoken"),
        *(
            AirportAliasIn(alias=f.call, kind="spoken")
            for f in frequencies.values()
            if f.call and f.service in ("TWR", "GND", "CD", "APP", "DEP")
        ),
    ]
    unique_aliases = list({(a.alias, a.kind): a for a in aliases}.values())

    variation = base.get("MAG_VARN")
    return AirportProfileIn(
        icao=icao,
        faa_id=faa_id,
        iata=faa_id if len(faa_id) == 3 and faa_id.isalpha() else None,
        name=_title(name),
        city=_title(base.get("CITY") or "") or None,
        latitude=_float(base.get("LAT_DECIMAL")),
        longitude=_float(base.get("LONG_DECIMAL")),
        elevation_ft=_float(base.get("ELEV")),
        magnetic_variation=(
            f"{variation}{base.get('MAG_HEMIS', '')} ({base.get('MAG_VARN_YEAR', '')})"
            if variation
            else None
        ),
        timezone=timezone,
        runways=runways,
        frequencies=sorted(frequencies.values(), key=lambda f: (f.service, f.frequency_hz)),
        aliases=unique_aliases,
        provenance=(provenance or {}) | {"nasr_effective_date": base.get("EFF_DATE")},
    )


def download_cycle(cycle: date, cache_dir: Path, http) -> tuple[dict[str, Path], dict[str, Any]]:
    """Fetch the APT and FRQ CSV archives for one cycle (cached, hashed)."""
    stamp = cycle_stamp(cycle)
    target = Path(cache_dir) / cycle.isoformat()
    target.mkdir(parents=True, exist_ok=True)
    files, records = {}, {}
    for name in FILES:
        url = FILE_URL.format(stamp=stamp, name=name)
        path = target / f"{stamp}_{name}_CSV.zip"
        if not path.exists():
            response = http.get(url, follow_redirects=True, timeout=120)
            response.raise_for_status()
            tmp = path.with_suffix(".part")
            tmp.write_bytes(response.content)
            tmp.replace(path)
        data = path.read_bytes()
        files[name] = path
        records[name] = {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    provenance = {
        "source": "FAA NASR 28-Day Subscription (CSV edition)",
        "source_url": INDEX_URL,
        "cycle": cycle.isoformat(),
        "next_cycle": (cycle + timedelta(days=CYCLE_DAYS)).isoformat(),
        "files": records,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    return files, provenance
