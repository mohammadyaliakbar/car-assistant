import math
from datetime import datetime, timezone

import pytest

from app.detectors.event_detector import DetectionEvent, Severity
from app.emergency import RiskAnalyzer, RiskLevel
from app.location import (
    GpsFix,
    RoadAnalyzer,
    RoadContext,
    build_query,
    parse_maxspeed,
)

BASE_LAT, BASE_LON = 35.0, 51.0
M_PER_DEG = 111_194.93  # برابر شعاع ۶۳۷۱ کیلومتر در کد


def pt(east, north):
    return {
        "lat": BASE_LAT + north / M_PER_DEG,
        "lon": BASE_LON + east / (M_PER_DEG * math.cos(math.radians(BASE_LAT))),
    }


def way(points, **tags):
    return {"type": "way", "tags": tags, "geometry": [pt(*p) for p in points]}


def node(east, north, **tags):
    return {"type": "node", "tags": tags, **pt(east, north)}


def fix(accuracy=5.0):
    return GpsFix(
        latitude=BASE_LAT, longitude=BASE_LON, accuracy_m=accuracy,
        speed_kmh=0.0, heading_deg=90.0,
        timestamp=datetime.now(timezone.utc), source="test",
    )


def parse(*elements, accuracy=5.0, **response_extra):
    analyzer = RoadAnalyzer(fetch=lambda q, t: {"elements": []})
    data = {"elements": list(elements), **response_extra}
    return analyzer.parse_response(data, fix(accuracy))


STRAIGHT = [(-100, 3), (100, 3)]
ZIGZAG = [(-200 + 40 * k, 15 if k % 2 else -15) for k in range(11)]


# --- تشخیص جاده ---
def test_motorway_with_tags():
    ctx = parse(way(STRAIGHT, highway="motorway", maxspeed="120", lanes="3"))
    assert ctx.highway_type == "motorway"
    assert ctx.is_limited_access is True
    assert ctx.maxspeed_kmh == 120
    assert ctx.lane_count == 3
    assert ctx.data_quality == "ok"


def test_trunk_is_not_claimed_limited_access():
    ctx = parse(way(STRAIGHT, highway="trunk"))
    assert ctx.highway_type == "trunk"
    assert ctx.is_limited_access is None


def test_motorroad_yes_is_limited_access():
    ctx = parse(way(STRAIGHT, highway="primary", motorroad="yes"))
    assert ctx.is_limited_access is True


def test_non_vehicle_ways_are_ignored():
    ctx = parse(
        way([(-100, 2), (100, 2)], highway="footway"),
        way([(-100, 10), (100, 10)], highway="residential"),
    )
    assert ctx.highway_type == "residential"


def test_two_carriageways_of_same_type_stay_ok():
    ctx = parse(
        way([(-100, 5), (100, 5)], highway="motorway"),
        way([(-100, 15), (100, 15)], highway="motorway"),
    )
    assert ctx.data_quality == "ok"


# --- ابهام ---
def test_fast_road_within_gps_error_preferred_over_nearer_street():
    ctx = parse(
        way([(-100, 5), (100, 5)], highway="service"),
        way([(-100, 20), (100, 20)], highway="motorway"),
    )
    assert ctx.highway_type == "motorway"
    assert ctx.data_quality == "partial"


def test_two_different_streets_is_partial():
    ctx = parse(
        way([(-100, 5), (100, 5)], highway="residential"),
        way([(-100, 15), (100, 15)], highway="secondary"),
    )
    assert ctx.highway_type == "residential"
    assert ctx.data_quality == "partial"


def test_road_farther_than_gps_error_is_not_assigned():
    ctx = parse(way([(-100, 55), (100, 55)], highway="residential"))
    assert ctx.highway_type is None
    assert ctx.data_quality == "partial"


def test_no_roads_is_partial_with_unknowns():
    ctx = parse()
    assert ctx.data_quality == "partial"
    assert ctx.highway_type is None
    assert ctx.has_emergency_bay_nearby is None


# --- محل توقف اضطراری ---
def test_missing_bay_is_unknown_not_false():
    ctx = parse(way(STRAIGHT, highway="motorway"))
    assert ctx.has_emergency_bay_nearby is None
    assert ctx.emergency_bay_distance_m is None


def test_bay_node_distance():
    ctx = parse(
        way(STRAIGHT, highway="motorway"),
        node(100, 0, highway="emergency_bay"),
    )
    assert ctx.has_emergency_bay_nearby is True
    assert ctx.emergency_bay_distance_m == pytest.approx(100, abs=1)


def test_bay_way_and_nearest_bay_used():
    ctx = parse(
        way(STRAIGHT, highway="motorway"),
        node(250, 0, highway="emergency_bay"),
        way([(80, 0), (120, 0)], highway="emergency_bay"),
    )
    assert ctx.emergency_bay_distance_m == pytest.approx(80, abs=1)


def test_emergency_bay_is_not_treated_as_a_road():
    ctx = parse(way([(-10, 1), (10, 1)], highway="emergency_bay"))
    assert ctx.highway_type is None


# --- تگ‌ها ---
def test_no_stopping_true_only_when_tagged():
    tagged = parse(way(STRAIGHT, highway="motorway", **{"parking:condition": "no_stopping"}))
    plain = parse(way(STRAIGHT, highway="motorway"))
    assert tagged.no_stopping_tagged is True
    assert plain.no_stopping_tagged is None


def test_parse_maxspeed():
    assert parse_maxspeed("50") == 50
    assert parse_maxspeed("60 mph") == pytest.approx(96.6, abs=0.1)
    for bad in ("none", "signals", "IR:urban", "50;30", "0", "500", None, 50):
        assert parse_maxspeed(bad) is None


# --- پارکینگ ---
def test_inside_parking_polygon():
    ring = [(-50, -50), (50, -50), (50, 50), (-50, 50), (-50, -50)]
    ctx = parse(way(ring, amenity="parking"))
    assert ctx.is_parking_area is True


def test_outside_parking_polygon_is_unknown_not_false():
    ring = [(100, 100), (200, 100), (200, 200), (100, 200), (100, 100)]
    assert parse(way(ring, amenity="parking")).is_parking_area is None


def test_unclosed_parking_way_is_not_a_polygon():
    open_way = [(-50, -50), (50, -50), (50, 50), (-50, 50)]
    assert parse(way(open_way, amenity="parking")).is_parking_area is None


# --- جاده‌ی پیچ‌دار ---
def test_zigzag_road_is_winding():
    assert parse(way(ZIGZAG, highway="secondary")).is_winding is True


def test_straight_road_is_not_claimed_winding_or_straight():
    assert parse(way([(-300, 0), (-100, 0), (100, 0), (300, 0)], highway="secondary")).is_winding is None


def test_single_corner_is_not_winding():
    corner = [(-100, 0), (-50, 0), (0, 0), (0, 50), (0, 100)]
    assert parse(way(corner, highway="secondary")).is_winding is None


def test_roundabout_is_not_winding():
    assert parse(way(ZIGZAG, highway="secondary", junction="roundabout")).is_winding is None


# --- پاسخ‌های خراب ---
def test_remark_means_unavailable():
    ctx = parse(way(STRAIGHT, highway="motorway"), remark="runtime error: Query timed out")
    assert ctx.data_quality == "unavailable"
    assert ctx.highway_type is None


def test_malformed_response_is_unavailable():
    analyzer = RoadAnalyzer(fetch=lambda q, t: {})
    for bad in (None, [], {}, {"elements": "x"}):
        assert analyzer.parse_response(bad, fix()).data_quality == "unavailable"


def test_malformed_elements_are_skipped():
    ctx = parse("junk", {"type": "way"}, {"type": "way", "tags": [], "geometry": [{"x": 1}]},
                way(STRAIGHT, highway="motorway"))
    assert ctx.highway_type == "motorway"


# --- شبکه و کش ---
def test_network_failure_is_unavailable_not_exception():
    def boom(query, timeout):
        raise TimeoutError("slow")

    ctx = RoadAnalyzer(fetch=boom).analyze(fix())
    assert ctx.data_quality == "unavailable"
    assert "TimeoutError" in ctx.notes[0]


def test_no_fix_is_unavailable():
    assert RoadAnalyzer(fetch=lambda q, t: {"elements": []}).analyze(None).data_quality == "unavailable"


def test_cache_avoids_repeated_requests():
    calls = []

    def fetch(query, timeout):
        calls.append(query)
        return {"elements": [way(STRAIGHT, highway="motorway")]}

    analyzer = RoadAnalyzer(fetch=fetch, clock=lambda: 0.0)
    first = analyzer.analyze(fix())
    second = analyzer.analyze(fix())
    assert len(calls) == 1
    assert first == second


def test_cache_expires():
    now = [0.0]
    calls = []

    def fetch(query, timeout):
        calls.append(1)
        return {"elements": []}

    analyzer = RoadAnalyzer(fetch=fetch, cache_ttl_s=600, clock=lambda: now[0])
    analyzer.analyze(fix())
    now[0] = 601
    analyzer.analyze(fix())
    assert len(calls) == 2


def test_failures_are_not_cached():
    calls = []

    def fetch(query, timeout):
        calls.append(1)
        raise ConnectionError("down")

    analyzer = RoadAnalyzer(fetch=fetch)
    analyzer.analyze(fix())
    analyzer.analyze(fix())
    assert len(calls) == 2


def test_query_contents():
    q = build_query(35.5, -51.25, 60, 300)
    assert "around:60,35.500000,-51.250000" in q
    assert "around:300,35.500000,-51.250000" in q
    assert "emergency_bay" in q and "motorway" in q and "amenity=parking" in q
    assert q.rstrip().endswith("out geom;")


def test_search_radius_grows_with_poor_accuracy_but_is_capped():
    seen = []

    def fetch(query, timeout):
        seen.append(query)
        return {"elements": []}

    analyzer = RoadAnalyzer(fetch=fetch)
    analyzer.analyze(fix(accuracy=100))
    analyzer.analyze(GpsFix(latitude=36, longitude=52, accuracy_m=900,
                            timestamp=datetime.now(timezone.utc), source="t"))
    assert "around:100," in seen[0]
    assert "around:150," in seen[1]


# --- هماهنگی با Risk Analyzer ---
def event():
    return DetectionEvent(
        detector="stoppage", event_type="engine_stalled_while_moving",
        severity=Severity.WARNING, message="t", vehicle_id="car_a",
        timestamp=datetime.now(timezone.utc), details={"previous_speed_kmh": 100.0},
    )


def test_motorway_flows_into_risk_analyzer_as_high():
    f = fix()
    ctx = RoadAnalyzer(
        fetch=lambda q, t: {"elements": [way(STRAIGHT, highway="motorway")]}
    ).analyze(f)
    assert RiskAnalyzer().assess(event(), f, ctx).level == RiskLevel.HIGH


def test_close_bay_reduces_one_level_end_to_end():
    f = fix()
    ctx = RoadAnalyzer(fetch=lambda q, t: {"elements": [
        way(STRAIGHT, highway="motorway"),
        node(100, 0, highway="emergency_bay"),
    ]}).analyze(f)
    assert RiskAnalyzer().assess(event(), f, ctx).level == RiskLevel.MEDIUM


def test_failed_map_flows_into_unknown():
    def boom(q, t):
        raise ConnectionError("offline")

    f = fix()
    ctx = RoadAnalyzer(fetch=boom).analyze(f)
    assert isinstance(ctx, RoadContext)
    assert RiskAnalyzer().assess(event(), f, ctx).level == RiskLevel.UNKNOWN