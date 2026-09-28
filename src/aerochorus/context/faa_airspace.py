"""FAA Class Airspace (ADDS open data) → simplified polygons for the airport map.

Source: FAA Aeronautical Data Delivery Service, "Class Airspace" feature layer
(ArcGIS REST). Queried once per airport with a small bounding box; only the
classes that matter for an ATC annotator are kept (B, C, D by default). Polygons
are simplified (Ramer-Douglas-Peucker) so they are light to store and draw.
Context only: never used to change a transcript or label.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

SERVICE_URL = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "Class_Airspace/FeatureServer/0/query"
)
INFO_URL = "https://adds-faa.opendata.arcgis.com/datasets/faa::class-airspace"
KEEP_TYPES = ("CLASS_B", "CLASS_C", "CLASS_D")


def bbox(lat: float, lon: float, radius_nm: float) -> tuple[float, float, float, float]:
    dlat = radius_nm / 60.0
    dlon = radius_nm / (60.0 * max(math.cos(math.radians(lat)), 0.01))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def _perpendicular(p, a, b) -> float:
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(x - x1, y - y1)
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / math.hypot(dx, dy)


def simplify(points: list[list[float]], tolerance: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker (iterative), keeping the ring closed."""
    if len(points) < 4:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        best, index = 0.0, None
        for i in range(start + 1, end):
            d = _perpendicular(points[i], points[start], points[end])
            if d > best:
                best, index = d, i
        if index is not None and best > tolerance:
            keep[index] = True
            stack += [(start, index), (index, end)]
    return [[round(x, 5), round(y, 5)] for (x, y), k in zip(points, keep, strict=True) if k]


def _feet(value: Any, uom: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= -9000:  # the dataset's "unlimited/unspecified" sentinel
        return None
    return int(number * 100) if uom == "FL" else int(number)


def parse_features(
    geojson: dict[str, Any], *, keep_types: tuple[str, ...] = KEEP_TYPES, tolerance: float = 0.0015
) -> list[dict[str, Any]]:
    out = []
    for feature in geojson.get("features") or []:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if props.get("LOCAL_TYPE") not in keep_types or geometry.get("type") not in (
            "Polygon",
            "MultiPolygon",
        ):
            continue
        polygons = (
            [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        )
        rings = [simplify(poly[0], tolerance) for poly in polygons if poly and poly[0]]
        out.append(
            {
                "name": props.get("NAME") or "",
                "airspace_class": props.get("CLASS") or props.get("LOCAL_TYPE"),
                "local_type": props.get("LOCAL_TYPE"),
                "lower_ft": _feet(props.get("LOWER_VAL"), props.get("LOWER_UOM")),
                "lower_ref": props.get("LOWER_CODE"),
                "upper_ft": _feet(props.get("UPPER_VAL"), props.get("UPPER_UOM")),
                "upper_ref": props.get("UPPER_CODE"),
                "rings": [r for r in rings if len(r) >= 4],
                "source_id": props.get("GLOBAL_ID"),
            }
        )
    out.sort(key=lambda a: (a["local_type"], a["lower_ft"] or 0, a["name"]))
    return out


def fetch_airspace(lat: float, lon: float, radius_nm: float, http) -> tuple[list[dict], dict]:
    west, south, east, north = bbox(lat, lon, radius_nm)
    params = {
        "where": "1=1",
        "geometry": f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
    }
    response = http.get(SERVICE_URL, params=params, timeout=120)
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(f"FAA airspace query failed: {body['error']}")
    provenance = {
        "source": "FAA ADDS Class Airspace (ArcGIS feature service)",
        "source_url": INFO_URL,
        "query_url": SERVICE_URL,
        "bbox": [round(v, 4) for v in (west, south, east, north)],
        "radius_nm": radius_nm,
        "features_returned": len(body.get("features") or []),
        "response_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
        "fetched_at": datetime.now(UTC).isoformat(),
        "simplified": "Ramer-Douglas-Peucker, tolerance 0.0015 deg (~150 m)",
    }
    return parse_features(body), provenance
