"""FAA NASR CSV → airport profile (synthetic miniature of the real layout)."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

import pytest

from aerochorus.context import faa_nasr


def _zip(path, members: dict[str, list[dict[str, str]]]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, rows in members.items():
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            zf.writestr(name, buffer.getvalue())
    return path


@pytest.fixture
def nasr(tmp_path):
    base = {
        "ARPT_ID": "BWI",
        "ICAO_ID": "KBWI",
        "CITY": "BALTIMORE",
        "ARPT_NAME": "BALTIMORE/WASHINGTON INTL THURGOOD MARSHALL",
        "LAT_DECIMAL": "39.17540",
        "LONG_DECIMAL": "-76.66830",
        "ELEV": "143",
        "MAG_VARN": "11",
        "MAG_HEMIS": "W",
        "MAG_VARN_YEAR": "2020",
        "EFF_DATE": "2026/09/03",
    }
    other = base | {"ARPT_ID": "DCA", "ICAO_ID": "KDCA", "CITY": "WASHINGTON"}
    runways = [
        {"ARPT_ID": "BWI", "RWY_ID": pair, "RWY_LEN": length, "RWY_WIDTH": "150"}
        for pair, length in (("10/28", "10503"), ("15L/33R", "5000"), ("15R/33L", "9501"))
    ]
    ends = [
        {"ARPT_ID": "BWI", "RWY_ID": pair, "RWY_END_ID": end, "TRUE_ALIGNMENT": bearing}
        for pair, end, bearing in (
            ("10/28", "10", "93"),
            ("10/28", "28", "273"),
            ("15L/33R", "15L", "140"),
            ("15L/33R", "33R", "320"),
            ("15R/33L", "15R", "140"),
            ("15R/33L", "33L", "320"),
        )
    ]
    freq = {
        "SERVICED_FACILITY": "BWI",
        "FACILITY": "BWI",
        "SECTORIZATION": "",
        "PRIMARY_APPROACH_RADIO_CALL": "POTOMAC",
        "TOWER_OR_COMM_CALL": "BALTIMORE",
    }
    frequencies = [
        freq | {"FREQ": "119.4", "FREQ_USE": "LCL/P"},
        freq | {"FREQ": "121.9", "FREQ_USE": "GND/P"},
        freq | {"FREQ": "118.05", "FREQ_USE": "CD/P"},
        freq | {"FREQ": "127.8", "FREQ_USE": "D-ATIS"},
        freq | {"FREQ": "119.7", "FREQ_USE": "APCH/P", "SECTORIZATION": "SOUTH"},
        freq | {"FREQ": "257.8", "FREQ_USE": "LCL/P"},  # UHF military: dropped
        freq | {"FREQ": "", "FREQ_USE": "APT REMARK"},
        freq | {"SERVICED_FACILITY": "DCA", "FREQ": "119.1", "FREQ_USE": "LCL/P"},
    ]
    return {
        "APT": _zip(
            tmp_path / "APT.zip",
            {
                "APT_BASE.csv": [base, other],
                "APT_RWY.csv": runways,
                "APT_RWY_END.csv": ends,
            },
        ),
        "FRQ": _zip(tmp_path / "FRQ.zip", {"FRQ.csv": frequencies}),
    }


def test_profile_from_nasr(nasr):
    profile = faa_nasr.parse_profile(
        nasr,
        "BWI",
        timezone="America/New_York",
        station_labels=("BWI",),
        provenance={"cycle": "2026-09-03"},
    )
    assert profile.icao == "KBWI" and profile.iata == "BWI"
    assert profile.name == "Baltimore/Washington Intl Thurgood Marshall"
    assert profile.timezone == "America/New_York"
    assert sorted({r.pair for r in profile.runways}) == ["10/28", "15L/33R", "15R/33L"]
    ends = {r.end_ident: r for r in profile.runways}
    assert ends["33L"].spoken == ["runway three three left"]
    assert ends["10"].length_ft == 10503 and ends["10"].true_alignment == 93.0

    services = {f.service: f for f in profile.frequencies}
    assert set(services) == {"TWR", "GND", "CD", "ATIS", "APP"}  # UHF, remarks, DCA dropped
    assert services["TWR"].call == "Baltimore Tower" and services["TWR"].frequency_hz == 119_400_000
    assert services["TWR"].spoken[0] == "one one niner point four"
    assert services["APP"].call == "Potomac Approach"
    assert services["APP"].sectorization == "APCH/P SOUTH"
    aliases = {(a.alias, a.kind) for a in profile.aliases}
    assert {
        ("KBWI", "icao"),
        ("BWI", "faa"),
        ("BWI", "station"),
        ("Baltimore Tower", "spoken"),
    } <= aliases
    assert profile.provenance == {"cycle": "2026-09-03", "nasr_effective_date": "2026/09/03"}


def test_unknown_airport_is_an_error(nasr):
    with pytest.raises(LookupError):
        faa_nasr.parse_profile(nasr, "XYZ", timezone="UTC")


def test_cycles_and_spoken_forms():
    assert faa_nasr.cycle_for(date(2026, 9, 27)) == date(2026, 9, 3)
    assert faa_nasr.cycle_for(date(2026, 10, 1)) == date(2026, 10, 1)
    assert faa_nasr.cycle_for(date(2026, 9, 2)) == date(2026, 8, 6)
    assert faa_nasr.cycle_stamp(date(2026, 9, 3)) == "03_Sep_2026"
    assert faa_nasr.spoken_runway("9") == ["runway niner", "runway nine"]
    assert faa_nasr.spoken_runway("15C") == ["runway one five center"]
    assert faa_nasr.spoken_frequency("121.90") == [
        "one two one point niner",
        "one two one point nine",
        "one two one decimal niner",
    ]
