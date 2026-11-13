# app/location/offline_provider.py
"""
منبع آفلاین اطلاعات جاده (SQLite + R-tree) به‌جای Overpass.

خروجی `OfflineRoadProvider.fetch()` همان قالب پاسخ Overpass (`out geom`) است،
پس `RoadAnalyzer.parse_response` و منطق تحلیل بدون تغییر کار می‌کنند.

اصول:
- اگر نقطه (به‌اضافه‌ی شعاع جست‌وجو) خارج از محدوده‌ی پایگاه‌داده باشد،
  `OutsideCoverageError` پرتاب می‌شود. جواب خالی در آن حالت به معنی
  «جاده‌ای نیست» تفسیر می‌شد و این گمراه‌کننده است.
- نبودِ پایگاه‌داده هم خطا است (FileNotFoundError)، نه جواب خالی.
- داده از OpenStreetMap است (مجوز ODbL): © OpenStreetMap contributors.
"""

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.location.road_analyzer import ROAD_TYPES, _distance_to_origin, _project

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
METERS_PER_DEG_LAT = 111_194.9  # با شعاع زمین ۶٬۳۷۱ کیلومتر

# فقط تگ‌هایی که RoadAnalyzer واقعاً می‌خواند (به‌اضافه‌ی name برای عیب‌یابی)
KEPT_TAGS = frozenset({
    "highway", "maxspeed", "lanes", "junction", "motorroad", "amenity", "name",
})

KINDS = ("road", "parking", "bay")


class OutsideCoverageError(Exception):
    """نقطه خارج از محدوده‌ی پایگاه‌داده‌ی آفلاین است."""


# ---------------------------------------------------------------- ساخت

def filter_tags(tags: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in tags.items()
        if k in KEPT_TAGS or k.startswith("parking:")
    }


def classify_feature(
    osm_type: str,
    tags: dict[str, str],
    points: list[tuple[float, float]],
) -> str | None:
    """
    نوع عنصر را مشخص می‌کند: "road" / "parking" / "bay" یا None (نامربوط).
    ترتیب و شروط مطابق `RoadAnalyzer.parse_response` است.
    """
    if not points:
        return None
    if tags.get("highway") == "emergency_bay":
        return "bay"
    if osm_type != "way":
        return None
    if tags.get("highway") in ROAD_TYPES:
        return "road"
    if tags.get("amenity") == "parking":
        # فقط چندضلعی بسته (مثل `way(pivot)` در query قبلی)
        if len(points) >= 4 and points[0] == points[-1]:
            return "parking"
    return None


def write_database(
    path: str | Path,
    features: Iterable[dict[str, Any]],
    meta: dict[str, Any],
) -> dict[str, int]:
    """
    پایگاه‌داده را می‌سازد (اتمی: اول فایل موقت، بعد جایگزینی).

    هر feature: {"osm_type", "osm_id", "kind", "tags", "points": [(lat, lon), ...]}
    تعداد هر نوع را برمی‌گرداند.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)

    counts = {kind: 0 for kind in KINDS}
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(
            """
            CREATE TABLE features (
                id       INTEGER PRIMARY KEY,
                osm_type TEXT NOT NULL,
                osm_id   INTEGER NOT NULL,
                kind     TEXT NOT NULL,
                tags     TEXT NOT NULL,
                geom     TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE features_rtree USING rtree(
                id, min_lat, max_lat, min_lon, max_lon
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        for f in features:
            points = f["points"]
            lats = [p[0] for p in points]
            lons = [p[1] for p in points]
            cur = conn.execute(
                "INSERT INTO features (osm_type, osm_id, kind, tags, geom) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f["osm_type"],
                    f["osm_id"],
                    f["kind"],
                    json.dumps(f["tags"], ensure_ascii=False),
                    json.dumps([[p[0], p[1]] for p in points]),
                ),
            )
            conn.execute(
                "INSERT INTO features_rtree VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, min(lats), max(lats), min(lons), max(lons)),
            )
            counts[f["kind"]] += 1

        full_meta = {
            **meta,
            "schema_version": SCHEMA_VERSION,
            "counts": counts,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "attribution": "© OpenStreetMap contributors (ODbL)",
        }
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in full_meta.items()],
        )
        conn.commit()
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp, path)
    return counts


# ---------------------------------------------------------------- خواندن

class OfflineRoadProvider:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"پایگاه‌داده‌ی آفلاین جاده پیدا نشد: {self.db_path}"
            )
        # اتصال تازه برای هر فراخوانی: ایمن در برابر thread و ارزان در SQLite
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)

    @staticmethod
    def _read_meta(conn: sqlite3.Connection) -> dict[str, Any]:
        return {
            key: json.loads(value)
            for key, value in conn.execute("SELECT key, value FROM meta")
        }

    def meta(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._read_meta(conn)
        finally:
            conn.close()

    def fetch(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        bay_radius_m: float,
    ) -> dict:
        """پاسخی هم‌قالب Overpass (`out geom`) برمی‌گرداند."""
        margin_m = max(radius_m, bay_radius_m)
        dlat = margin_m / METERS_PER_DEG_LAT
        dlon = margin_m / (METERS_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))

        conn = self._connect()
        try:
            meta = self._read_meta(conn)
            self._check_coverage(meta, lat, lon, dlat, dlon)

            rows = conn.execute(
                """
                SELECT f.osm_type, f.osm_id, f.kind, f.tags, f.geom
                FROM features_rtree r
                JOIN features f ON f.id = r.id
                WHERE r.min_lat <= ? AND r.max_lat >= ?
                  AND r.min_lon <= ? AND r.max_lon >= ?
                """,
                (lat + dlat, lat - dlat, lon + dlon, lon - dlon),
            ).fetchall()
        finally:
            conn.close()

        elements: list[dict] = []
        for osm_type, osm_id, kind, tags_json, geom_json in rows:
            coords = json.loads(geom_json)
            projected = [_project(p[0], p[1], lat, lon) for p in coords]

            if kind == "road":
                keep = _distance_to_origin(projected) <= radius_m
            elif kind == "bay":
                keep = _distance_to_origin(projected) <= bay_radius_m
            else:  # parking: فقط اگر نقطه داخل جعبه‌ی محدودکننده باشد
                lats = [p[0] for p in coords]
                lons = [p[1] for p in coords]
                keep = min(lats) <= lat <= max(lats) and min(lons) <= lon <= max(lons)
            if not keep:
                continue

            tags = json.loads(tags_json)
            if osm_type == "node":
                elements.append({
                    "type": "node",
                    "id": osm_id,
                    "lat": coords[0][0],
                    "lon": coords[0][1],
                    "tags": tags,
                })
            else:
                elements.append({
                    "type": "way",
                    "id": osm_id,
                    "tags": tags,
                    "geometry": [{"lat": p[0], "lon": p[1]} for p in coords],
                })

        return {"elements": elements}

    @staticmethod
    def _check_coverage(
        meta: dict[str, Any],
        lat: float,
        lon: float,
        dlat: float,
        dlon: float,
    ) -> None:
        bbox = meta.get("bbox")
        if not bbox:
            return
        min_lon, min_lat, max_lon, max_lat = bbox
        if not (
            min_lat + dlat <= lat <= max_lat - dlat
            and min_lon + dlon <= lon <= max_lon - dlon
        ):
            raise OutsideCoverageError(
                "نقطه خارج از محدوده‌ی نقشه‌ی آفلاین (یا بسیار نزدیک به لبه‌ی آن) است."
            )
