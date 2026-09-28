"""ICAO airline designators -> radiotelephony callsigns, for context hints only.

A small table of carriers common in the US Northeast (and at BWI in particular).
It turns an ADS-B callsign such as ``SWA456`` into the hint "Southwest 456" so an
adjudicating model can recognise what was said. It never rewrites a transcript.
"""

from __future__ import annotations

import re

TELEPHONY = {
    "AAL": "American",
    "AAY": "Allegiant",
    "ACA": "Air Canada",
    "AFR": "Airfrance",
    "ASA": "Alaska",
    "ASH": "Air Shuttle",
    "ATN": "Air Transport",
    "BAW": "Speedbird",
    "CJC": "Colgan",
    "CKS": "Connie",
    "CMP": "Copa",
    "DAL": "Delta",
    "DLH": "Lufthansa",
    "EDV": "Endeavor",
    "EJA": "Execjet",
    "ENY": "Envoy",
    "FDX": "FedEx",
    "FFT": "Frontier Flight",
    "GJS": "Lindbergh",
    "GTI": "Giant",
    "ICE": "Iceair",
    "JBU": "JetBlue",
    "JIA": "Blue Streak",
    "JZA": "Jazz",
    "KLM": "KLM",
    "LXJ": "Flexjet",
    "MXY": "Moxy",
    "NKS": "Spirit Wings",
    "PDT": "Piedmont",
    "QXE": "Horizon",
    "RPA": "Brickyard",
    "SCX": "Sun Country",
    "SKW": "SkyWest",
    "SWA": "Southwest",
    "UAL": "United",
    "UPS": "UPS",
    "VIR": "Virgin",
    "VXP": "Avelo",
    "WJA": "WestJet",
}

_AIRLINE = re.compile(r"^([A-Z]{3})(\d[0-9A-Z]*)$")
_N_NUMBER = re.compile(r"^N\d[0-9A-Z]{0,4}$")


def telephony_hint(callsign: str | None) -> str | None:
    """'SWA456' -> 'Southwest 456'; 'N123AB' -> 'November 123AB'; unknown -> None."""
    if not callsign:
        return None
    callsign = callsign.strip().upper()
    if _N_NUMBER.match(callsign):
        return f"November {callsign[1:]}"
    match = _AIRLINE.match(callsign)
    if match and match.group(1) in TELEPHONY:
        return f"{TELEPHONY[match.group(1)]} {match.group(2)}"
    return None
