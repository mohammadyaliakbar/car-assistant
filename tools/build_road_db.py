#!/usr/bin/env python
"""
ساخت پایگاه‌داده‌ی آفلاین جاده از فایل PBF اوپن‌استریت‌مپ.

نمونه:
    pip install osmium
    python tools/build_road_db.py iran-latest.osm.pbf \\
        -o data/roads_tehran.sqlite

فقط یک‌بار (و هر بار که بخواهید داده تازه شود) اجرا می‌شود؛ خروجی بدون
اینترنت توسط `OfflineRoadProvider` خوانده می‌شود.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.location.offline_provider import (  # noqa: E402
    classify_feature,
    filter_tags,
    write_database,
)

logger = logging.getLogger("build_road_db")

# min_lon, min_lat, max_lon, max_lat — تقریبی و بزرگ‌تر از شهر تهران؛
# مرز رسمی استان نیست. در صورت نیاز با --bbox عوض شود.
DEFAULT_BBOX = (50.5, 34.9, 52.3, 36.2)


def intersects(points, bbox) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return not (
        max(lats) < min_lat or min(lats) > max_lat
        or max(lons) < min_lon or min(lons) > max_lon
    )


def read_osm_timestamp(path: Path):
    """زمان تازگی داده از سرآیند PBF؛ در صورت نبود None."""
    try:
        import osmium

        reader = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING)
        try:
            value = reader.header().get("osmosis_replication_timestamp")
        finally:
            reader.close()
        return value or None
    except Exception:
        return None


def extract_features(pbf_path: Path, bbox) -> list[dict]:
    import osmium  # فقط اینجا لازم است

    features: list[dict] = []

    def add(osm_type: str, osm_id: int, tags: dict, points: list) -> None:
        kind = classify_feature(osm_type, tags, points)
        if kind is None or not intersects(points, bbox):
            return
        features.append({
            "osm_type": osm_type,
            "osm_id": osm_id,
            "kind": kind,
            "tags": filter_tags(tags),
            "points": [(round(a, 7), round(b, 7)) for a, b in points],
        })

    class Handler(osmium.SimpleHandler):
        def node(self, n):
            if not n.tags:
                return
            tags = {t.k: t.v for t in n.tags}
            if tags.get("highway") != "emergency_bay" or not n.location.valid():
                return
            add("node", n.id, tags, [(n.location.lat, n.location.lon)])

        def way(self, w):
            tags = {t.k: t.v for t in w.tags}
            if "highway" not in tags and tags.get("amenity") != "parking":
                return
            try:
                points = [(n.lat, n.lon) for n in w.nodes]
            except osmium.InvalidLocationError:
                return  # گره‌ای بدون مختصات
            add("way", w.id, tags, points)

    Handler().apply_file(str(pbf_path), locations=True)
    return features


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("pbf", type=Path, help="مسیر فایل .osm.pbf")
    parser.add_argument("-o", "--output", type=Path,
                        default=Path("data/roads_tehran.sqlite"))
    parser.add_argument("--bbox", nargs=4, type=float,
                        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                        default=DEFAULT_BBOX)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.pbf.is_file():
        print(f"فایل پیدا نشد: {args.pbf}", file=sys.stderr)
        return 1
    try:
        import osmium  # noqa: F401
    except ImportError:
        print("کتابخانه‌ی osmium نصب نیست:  pip install osmium", file=sys.stderr)
        return 1

    bbox = tuple(args.bbox)
    start = time.monotonic()
    logger.info("خواندن %s (ممکن است چند دقیقه و چند صد مگابایت RAM لازم باشد)...",
                args.pbf)
    features = extract_features(args.pbf, bbox)
    logger.info("%d عنصر استخراج شد؛ نوشتن پایگاه‌داده...", len(features))

    counts = write_database(
        args.output,
        features,
        {
            "bbox": list(bbox),
            "source_file": args.pbf.name,
            "osm_timestamp": read_osm_timestamp(args.pbf),
        },
    )
    logger.info("تمام شد در %.0f ثانیه: %s", time.monotonic() - start, counts)
    if not any(counts.values()):
        print("هشدار: هیچ عنصری استخراج نشد؛ bbox یا فایل را بررسی کنید.",
              file=sys.stderr)
        return 2
    print(f"خروجی: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
