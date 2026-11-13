#!/usr/bin/env python
"""
بررسی دستی نقشه‌ی آفلاین با مختصات واقعی (بدون اینترنت).

یک نقطه:
    python tools/check_location.py 35.7000 51.4000

چند نقطه از فایل CSV (ستون‌ها: name,lat,lon[,expected_highway]):
    python tools/check_location.py --csv tools/points.csv

برای هر نقطه نتیجه‌ی RoadAnalyzer و نزدیک‌ترین راه‌های خام پایگاه‌داده
(با نام و فاصله) چاپ می‌شود تا بتوان با نقشه‌ی واقعی مقایسه کرد.
اگر ستون expected_highway پر باشد، ✅/❌ هم نمایش داده می‌شود.
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.location.models import GpsFix  # noqa: E402
from app.location.offline_provider import (  # noqa: E402
    OfflineRoadProvider,
    OutsideCoverageError,
)
from app.location.road_analyzer import (  # noqa: E402
    ROAD_TYPES,
    RoadAnalyzer,
    _distance_to_origin,
    _project,
)

FIELDS = (
    "data_quality", "highway_type", "is_limited_access", "maxspeed_kmh",
    "lane_count", "is_winding", "is_parking_area",
    "has_emergency_bay_nearby", "no_stopping_tagged",
)


def offline_only(query, timeout):
    raise ConnectionError("check_location فقط از نقشه‌ی آفلاین استفاده می‌کند")


def nearest_roads(provider, lat, lon, radius, limit=3):
    data = provider.fetch(lat, lon, radius, 300.0)
    rows = []
    for e in data["elements"]:
        tags = e.get("tags", {})
        if e["type"] != "way" or tags.get("highway") not in ROAD_TYPES:
            continue
        pts = [_project(g["lat"], g["lon"], lat, lon) for g in e["geometry"]]
        rows.append((_distance_to_origin(pts), tags))
    rows.sort(key=lambda r: r[0])
    return rows[:limit]


def check_point(provider, analyzer, name, lat, lon, accuracy, expected):
    print(f"\n=== {name}  ({lat:.6f}, {lon:.6f}) ===")
    fix = GpsFix(
        latitude=lat, longitude=lon, accuracy_m=accuracy,
        timestamp=datetime.now(timezone.utc), source="CHECK",
    )
    ctx = analyzer.analyze(fix)
    for field in FIELDS:
        print(f"  {field:26} {getattr(ctx, field)}")
    for note in ctx.notes:
        print(f"  note: {note}")

    try:
        roads = nearest_roads(provider, lat, lon, max(accuracy, 100.0))
    except (OutsideCoverageError, FileNotFoundError) as exc:
        print(f"  (راه‌های خام: {type(exc).__name__})")
        roads = []
    if roads:
        print("  نزدیک‌ترین راه‌ها در پایگاه‌داده:")
        for dist, tags in roads:
            print(
                f"    {dist:6.1f} m  {tags.get('highway'):14} "
                f"maxspeed={tags.get('maxspeed', '-'):6} "
                f"lanes={tags.get('lanes', '-'):3} name={tags.get('name', '-')}"
            )

    if expected:
        ok = ctx.highway_type == expected
        print(f"  انتظار: {expected} → {'✅' if ok else '❌'}")
        return ok
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("lat", nargs="?", type=float)
    parser.add_argument("lon", nargs="?", type=float)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/roads_tehran.sqlite"))
    parser.add_argument("--accuracy", type=float, default=10.0,
                        help="دقت GPS به متر (پیش‌فرض ۱۰)")
    args = parser.parse_args()

    points = []
    if args.csv:
        with open(args.csv, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(
                line for line in f if line.strip() and not line.startswith("#")
            ):
                points.append((
                    row["name"], float(row["lat"]), float(row["lon"]),
                    (row.get("expected_highway") or "").strip() or None,
                ))
    elif args.lat is not None and args.lon is not None:
        points.append(("point", args.lat, args.lon, None))
    else:
        parser.error("یا lat و lon بدهید، یا --csv")

    provider = OfflineRoadProvider(args.db)
    if not args.db.is_file():
        print(f"پایگاه‌داده پیدا نشد: {args.db}", file=sys.stderr)
        return 1
    meta = provider.meta()
    print(f"DB: {args.db}  bbox={meta.get('bbox')}  "
          f"osm_timestamp={meta.get('osm_timestamp')}  counts={meta.get('counts')}")

    analyzer = RoadAnalyzer(fetch=offline_only, offline=provider)
    results = [
        check_point(provider, analyzer, n, la, lo, args.accuracy, exp)
        for n, la, lo, exp in points
    ]
    checked = [r for r in results if r is not None]
    if checked:
        print(f"\nخلاصه: {sum(checked)}/{len(checked)} مطابق انتظار")
        return 0 if all(checked) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
