import math
from datetime import datetime, timezone

from app.location.models import GpsFix
from app.location.offline_provider import (
    OfflineRoadProvider,
    OutsideCoverageError,
    write_database,
)
from app.location.road_analyzer import OFFLINE_NOTE, RoadAnalyzer

LAT0, LON0 = 35.7000, 51.4000


def north(m):
    return m / 111_194.9


def east(m):
    return m / (111_194.9 * math.cos(math.radians(LAT0)))


def fix(lat=LAT0, lon=LON0, accuracy=10.0):
    return GpsFix(
        latitude=lat, longitude=lon, accuracy_m=accuracy,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), source="SIMULATED",
    )


def geom(*pairs):
    return [{"lat": LAT0 + north(n), "lon": LON0 + east(e)} for n, e in pairs]


def motorway_response():
    return {"elements": [{
        "type": "way",
        "tags": {"highway": "motorway", "maxspeed": "100"},
        "geometry": geom((-200, 10), (200, 10)),
    }]}


def residential_response():
    return {"elements": [{
        "type": "way",
        "tags": {"highway": "residential"},
        "geometry": geom((-200, 10), (200, 10)),
    }]}


class FakeOffline:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def fetch(self, lat, lon, radius_m, bay_radius_m):
        self.calls.append((lat, lon, radius_m, bay_radius_m))
        if self.error:
            raise self.error
        return self.response


class FakeOnline:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def __call__(self, query, timeout):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def test_offline_is_used_first_and_online_is_not_called():
    offline = FakeOffline(motorway_response())
    online = FakeOnline(residential_response())
    ctx = RoadAnalyzer(fetch=online, offline=offline).analyze(fix())

    assert ctx.highway_type == "motorway"
    assert OFFLINE_NOTE in ctx.notes
    assert online.calls == 0
    assert len(offline.calls) == 1


def test_offline_receives_radii():
    offline = FakeOffline(motorway_response())
    RoadAnalyzer(fetch=FakeOnline({"elements": []}), offline=offline,
                 bay_radius_m=300.0).analyze(fix(accuracy=10.0))
    _, _, radius, bay_radius = offline.calls[0]
    assert radius == 60.0 and bay_radius == 300.0


def test_missing_database_falls_back_to_online():
    online = FakeOnline(residential_response())
    ctx = RoadAnalyzer(
        fetch=online, offline=FakeOffline(error=FileNotFoundError("x"))
    ).analyze(fix())
    assert ctx.highway_type == "residential"
    assert OFFLINE_NOTE not in ctx.notes
    assert online.calls == 1


def test_outside_coverage_falls_back_to_online():
    online = FakeOnline(residential_response())
    ctx = RoadAnalyzer(
        fetch=online, offline=FakeOffline(error=OutsideCoverageError())
    ).analyze(fix())
    assert ctx.highway_type == "residential"
    assert online.calls == 1


def test_both_sources_failing_is_unavailable_not_exception():
    ctx = RoadAnalyzer(
        fetch=FakeOnline(error=TimeoutError()),
        offline=FakeOffline(error=FileNotFoundError()),
    ).analyze(fix())
    assert ctx.data_quality == "unavailable"


def test_unexpected_offline_error_falls_back_instead_of_crashing():
    online = FakeOnline(residential_response())
    ctx = RoadAnalyzer(
        fetch=online, offline=FakeOffline(error=RuntimeError("corrupt db"))
    ).analyze(fix())
    assert ctx.highway_type == "residential"


def test_empty_offline_result_does_not_trigger_online():
    # «جاده‌ای نیست» جواب معتبر است، نه شکست
    online = FakeOnline(residential_response())
    ctx = RoadAnalyzer(
        fetch=online, offline=FakeOffline({"elements": []})
    ).analyze(fix())
    assert ctx.data_quality == "partial"
    assert online.calls == 0


def test_offline_result_is_cached():
    offline = FakeOffline(motorway_response())
    analyzer = RoadAnalyzer(fetch=FakeOnline({"elements": []}), offline=offline)
    analyzer.analyze(fix())
    analyzer.analyze(fix())
    assert len(offline.calls) == 1


def test_without_offline_behaviour_is_unchanged():
    online = FakeOnline(motorway_response())
    ctx = RoadAnalyzer(fetch=online).analyze(fix())
    assert ctx.highway_type == "motorway"
    assert OFFLINE_NOTE not in ctx.notes
    assert online.calls == 1


def test_real_database_end_to_end(tmp_path):
    path = tmp_path / "roads.sqlite"
    write_database(
        path,
        [{
            "osm_type": "way", "osm_id": 1, "kind": "road",
            "tags": {"highway": "motorway", "maxspeed": "100", "lanes": "3"},
            "points": [(LAT0 - north(300), LON0 + east(10)),
                       (LAT0 + north(300), LON0 + east(10))],
        }],
        {"bbox": [51.30, 35.60, 51.50, 35.80]},
    )
    far_lat = 36.5  # خارج از محدوده‌ی پایگاه‌داده
    far_road = {"elements": [{
        "type": "way",
        "tags": {"highway": "residential"},
        "geometry": [
            {"lat": far_lat + north(5), "lon": LON0 + east(-200)},
            {"lat": far_lat + north(5), "lon": LON0 + east(200)},
        ],
    }]}
    online = FakeOnline(far_road)
    analyzer = RoadAnalyzer(fetch=online, offline=OfflineRoadProvider(path))

    inside = analyzer.analyze(fix())
    assert inside.highway_type == "motorway"
    assert inside.maxspeed_kmh == 100.0
    assert inside.lane_count == 3
    assert online.calls == 0

    outside = analyzer.analyze(fix(lat=far_lat))  # خارج از محدوده → Overpass
    assert outside.highway_type == "residential"
    assert online.calls == 1
