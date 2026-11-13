import math
import sqlite3
from datetime import datetime, timezone

import pytest

from app.location.models import GpsFix
from app.location.offline_provider import (
    OfflineRoadProvider,
    OutsideCoverageError,
    classify_feature,
    filter_tags,
    write_database,
)
from app.location.road_analyzer import RoadAnalyzer

LAT0, LON0 = 35.7000, 51.4000
BBOX = [51.30, 35.60, 51.50, 35.80]  # min_lon, min_lat, max_lon, max_lat


def north(m):
    return m / 111_194.9


def east(m):
    return m / (111_194.9 * math.cos(math.radians(LAT0)))


def pt(n_m=0.0, e_m=0.0):
    return (LAT0 + north(n_m), LON0 + east(e_m))


def way(osm_id, kind, tags, points):
    return {"osm_type": "way", "osm_id": osm_id, "kind": kind,
            "tags": tags, "points": points}


def sample_features():
    return [
        # آزادراه: ۲۰ متر شرق مبدأ، شمالی-جنوبی
        way(1, "road",
            {"highway": "motorway", "maxspeed": "100", "lanes": "4"},
            [pt(-500, 20), pt(0, 20), pt(500, 20)]),
        # خیابان فرعی: ۱۰۰ متر غرب
        way(2, "road", {"highway": "residential"},
            [pt(-200, -100), pt(200, -100)]),
        # خیابان دور: ۲ کیلومتر شمال
        way(3, "road", {"highway": "residential"},
            [pt(2000, -50), pt(2000, 50)]),
        # پارکینگ شامل مبدأ
        way(4, "parking", {"amenity": "parking"},
            [pt(-30, -30), pt(-30, 30), pt(30, 30), pt(30, -30), pt(-30, -30)]),
        # پارکینگ دور
        way(5, "parking", {"amenity": "parking"},
            [pt(800, 800), pt(800, 860), pt(860, 860), pt(860, 800), pt(800, 800)]),
        # محل توقف اضطراری (گره) ۱۰۰ متر شمال
        {"osm_type": "node", "osm_id": 6, "kind": "bay",
         "tags": {"highway": "emergency_bay"}, "points": [pt(100, 0)]},
        # محل توقف اضطراری دور (۱ کیلومتر)
        {"osm_type": "node", "osm_id": 7, "kind": "bay",
         "tags": {"highway": "emergency_bay"}, "points": [pt(1000, 0)]},
    ]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "roads.sqlite"
    write_database(path, sample_features(), {"bbox": BBOX, "source_file": "t.pbf"})
    return path


def ids(data, type_=None):
    return {e["id"] for e in data["elements"]
            if type_ is None or e["type"] == type_}


# ---------------------------------------------------------------- ساخت

def test_classify_feature():
    p = [(1.0, 1.0), (2.0, 2.0)]
    ring = [(0, 0), (0, 1), (1, 1), (0, 0)]
    assert classify_feature("way", {"highway": "motorway"}, p) == "road"
    assert classify_feature("way", {"highway": "footway"}, p) is None
    assert classify_feature("node", {"highway": "emergency_bay"}, [(1.0, 1.0)]) == "bay"
    assert classify_feature("way", {"highway": "emergency_bay"}, p) == "bay"
    assert classify_feature("way", {"amenity": "parking"}, ring) == "parking"
    assert classify_feature("way", {"amenity": "parking"}, p) is None  # باز
    assert classify_feature("node", {"highway": "motorway"}, [(1.0, 1.0)]) is None
    assert classify_feature("way", {"highway": "motorway"}, []) is None


def test_filter_tags_keeps_only_needed():
    kept = filter_tags({
        "highway": "motorway", "maxspeed": "100", "surface": "asphalt",
        "parking:both": "no_stopping", "note": "x",
    })
    assert kept == {
        "highway": "motorway", "maxspeed": "100", "parking:both": "no_stopping",
    }


def test_meta_and_counts(db):
    meta = OfflineRoadProvider(db).meta()
    assert meta["bbox"] == BBOX
    assert meta["counts"] == {"road": 3, "parking": 2, "bay": 2}
    assert meta["schema_version"] == 1
    assert meta["source_file"] == "t.pbf"
    assert "OpenStreetMap" in meta["attribution"]


def test_write_is_atomic_and_overwrites(tmp_path):
    path = tmp_path / "x" / "roads.sqlite"
    write_database(path, sample_features(), {"bbox": BBOX})
    write_database(path, sample_features()[:1], {"bbox": BBOX})
    assert OfflineRoadProvider(path).meta()["counts"]["road"] == 1
    assert not list(path.parent.glob("*.tmp"))


def test_failed_write_leaves_no_files(tmp_path):
    path = tmp_path / "roads.sqlite"
    bad = [{"osm_type": "way", "osm_id": 1, "kind": "road", "tags": {}}]  # بدون points
    with pytest.raises(KeyError):
        write_database(path, bad, {"bbox": BBOX})
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------- خواندن

def test_fetch_roads_respect_radius(db):
    provider = OfflineRoadProvider(db)

    near = provider.fetch(LAT0, LON0, 60, 300)
    assert ids(near, "way") & {1, 2, 3} == {1}  # فقط آزادراهِ ۲۰ متری

    wider = provider.fetch(LAT0, LON0, 150, 300)
    assert ids(wider, "way") & {1, 2, 3} == {1, 2}  # فرعیِ ۱۰۰ متری هم می‌آید


def test_fetch_way_element_has_overpass_shape(db):
    data = OfflineRoadProvider(db).fetch(LAT0, LON0, 60, 300)
    road = next(e for e in data["elements"] if e["id"] == 1)
    assert road["type"] == "way"
    assert road["tags"] == {"highway": "motorway", "maxspeed": "100", "lanes": "4"}
    assert len(road["geometry"]) == 3
    assert set(road["geometry"][0]) == {"lat", "lon"}


def test_fetch_bay_uses_bay_radius(db):
    provider = OfflineRoadProvider(db)
    data = provider.fetch(LAT0, LON0, 60, 300)
    bays = [e for e in data["elements"] if e["type"] == "node"]
    assert [b["id"] for b in bays] == [6]
    assert set(bays[0]) == {"type", "id", "lat", "lon", "tags"}

    far = provider.fetch(LAT0, LON0, 60, 1500)
    assert ids(far, "node") == {6, 7}


def test_fetch_parking_only_when_point_inside(db):
    provider = OfflineRoadProvider(db)
    inside = provider.fetch(LAT0, LON0, 60, 300)
    assert 4 in ids(inside) and 5 not in ids(inside)

    elsewhere = provider.fetch(*pt(400, 0), 60, 300)
    assert 4 not in ids(elsewhere)


def test_fetch_outside_coverage_raises(db):
    provider = OfflineRoadProvider(db)
    with pytest.raises(OutsideCoverageError):
        provider.fetch(36.5, 51.4, 60, 300)  # بیرون از bbox
    with pytest.raises(OutsideCoverageError):
        provider.fetch(35.6001, 51.4, 60, 300)  # داخل bbox ولی در حاشیه‌ی ۳۰۰ متری لبه


def test_fetch_missing_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        OfflineRoadProvider(tmp_path / "nope.sqlite").fetch(LAT0, LON0, 60, 300)


def test_database_is_opened_read_only(db):
    provider = OfflineRoadProvider(db)
    conn = provider._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM features")
    finally:
        conn.close()


# ---------------------------------------------------------------- با RoadAnalyzer

def make_fix(accuracy=10.0):
    return GpsFix(
        latitude=LAT0, longitude=LON0, accuracy_m=accuracy,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), source="SIMULATED",
    )


def test_provider_output_feeds_parse_response(db):
    provider = OfflineRoadProvider(db)
    analyzer = RoadAnalyzer(fetch=lambda q, t: {"elements": []})

    data = provider.fetch(LAT0, LON0, 60, 300)
    ctx = analyzer.parse_response(data, make_fix(), 60)

    assert ctx.data_quality == "ok"
    assert ctx.highway_type == "motorway"
    assert ctx.is_limited_access is True
    assert ctx.maxspeed_kmh == 100.0
    assert ctx.lane_count == 4
    assert ctx.has_emergency_bay_nearby is True
    assert abs(ctx.emergency_bay_distance_m - 100.0) < 2.0
    assert ctx.is_parking_area is True


def test_empty_area_gives_partial_not_crash(db):
    provider = OfflineRoadProvider(db)
    analyzer = RoadAnalyzer(fetch=lambda q, t: {"elements": []})
    data = provider.fetch(*pt(1000, 1000), 60, 300)  # جایی که چیزی نیست
    fix = GpsFix(
        latitude=pt(1000, 1000)[0], longitude=pt(1000, 1000)[1], accuracy_m=10.0,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), source="SIMULATED",
    )
    ctx = analyzer.parse_response(data, fix, 60)
    assert ctx.data_quality == "partial"
    assert ctx.highway_type is None
