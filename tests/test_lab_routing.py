"""تست‌های مسیریابی آفلاین روی جاده‌ها و نمایش جاده‌ها (مسیر واقعی، نه خط مستقیم)."""

import json
import math
import threading
import time
import urllib.error
import urllib.request

import pytest
from lab_roads_fixture import BRIDGE_ROW, LAT0, LON0, RIVER_COL, STEP, make_grid_db, node

from app.simulation.lab_engine import SimulationEngine
from app.simulation.lab_routing import RoadRouter, RoutingError, road_type_of
from app.simulation.lab_server import LabApp, make_server
from app.simulation.routes import Route, haversine_m


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("roads") / "roads.sqlite"
    make_grid_db(path)
    return path


@pytest.fixture
def router(db):
    return RoadRouter(db)


# نقطه‌ی شروع: کمی خارج از جاده (باید به جاده بچسبد). مقصد آن‌سوی رودخانه.
START = (LAT0 + 40 * STEP + 0.00003, LON0 + 5 * STEP + 0.0002)
DEST = node(40, 55)


def on_road(point, tol_m=1.5):
    """نقطه روی یکی از خطوط شطرنجی باشد (طول یا عرض هم‌تراز با یک ردیف/ستون)."""
    r = (point[0] - LAT0) / STEP
    c = (point[1] - LON0) / STEP
    near = lambda x: abs(x - round(x)) * STEP * 111_194 < tol_m  # noqa: E731
    return near(r) or near(c)


def test_route_follows_roads_and_detours_over_the_only_bridge(router):
    p = router.plan(START, DEST)
    straight = haversine_m(START, DEST)
    assert p.length_m > 2 * straight                      # رودخانه دور زدن اجباری می‌کند
    assert all(on_road(pt) for pt in p.points)            # هر نقطه روی جاده است، نه خط مستقیم
    bridge_lat = round(LAT0 + BRIDGE_ROW * STEP, 7)
    crossing = [(a, b) for a, b in zip(p.points, p.points[1:])
                if a[1] < LON0 + RIVER_COL * STEP <= b[1] or b[1] < LON0 + RIVER_COL * STEP <= a[1]]
    assert crossing and all(abs(a[0] - bridge_lat) < 1e-6 and abs(b[0] - bridge_lat) < 1e-6
                            for a, b in crossing)         # فقط از ردیف پل عبور کرده
    assert len(p.road_types) == len(p.points) - 1
    Route("t", "t", [tuple(x) for x in p.points], p.road_types)   # برای Route معتبر است


def test_start_is_snapped_onto_the_road(router):
    p = router.plan(START, DEST)
    assert haversine_m(START, p.snapped_start) < 30 and on_road(p.snapped_start)


def test_point_in_the_middle_of_a_segment_and_same_segment_route(router):
    mid_a = (node(20, 10)[0], LON0 + 10.4 * STEP)
    mid_b = (node(20, 10)[0], LON0 + 10.7 * STEP)         # هر دو روی همان قطعه‌ی بین دو تقاطع
    p = router.plan(mid_a, mid_b)
    expected = 0.3 * STEP * 111_194.9 * math.cos(math.radians(mid_a[0]))     # ۰٫۳ طول قطعه
    assert p.length_m == pytest.approx(expected, rel=0.02)                 # نه دور زدن
    assert all(on_road(pt) for pt in p.points)


def test_highway_class_maps_to_road_type(router):
    assert road_type_of("motorway") == "highway" and road_type_of("trunk_link") == "highway"
    assert road_type_of("residential") == "urban" and road_type_of("primary") == "urban"
    p = router.plan(node(0, 5), node(0, 20))              # ردیف ۰ آزادراه است
    assert set(p.road_types) == {"highway"}
    q = router.plan(node(20, 5), node(20, 20))
    assert set(q.road_types) == {"urban"}


def test_service_roads_are_not_used_as_shortcuts(router):
    a, b = node(40, 28), node(40, 31)                       # دو طرف رودخانه، ≈ ۵۴۰ متر از هم
    p = router.plan(a, b)
    pairs = list(zip(p.points, p.points[1:]))
    assert not any({tuple(x), tuple(y)} == {node(40, 29), node(40, 30)} for x, y in pairs)
    assert p.length_m > 5000                                # دور از پل (ردیف ۱۰)، نه میان‌بر service


def test_clear_errors_for_unroutable_requests(router):
    cases = {
        "outside": ((34.0, 51.3), DEST, "خارج از محدوده"),
        "too close": (START, (START[0] + 0.0001, START[1]), "یکی"),
        "far from roads": ((35.9, 51.7), DEST, "۴۰۰ متر"),
        "disconnected island": (START, (36.0, 51.505), "وصل نیستند"),
        "invalid": ((95.0, 0.0), DEST, "نامعتبر"),
    }
    for label, (a, b, text) in cases.items():
        with pytest.raises(RoutingError) as exc:
            router.plan(a, b)
        assert text in str(exc.value), label


def test_missing_database_is_an_error_not_a_crash(tmp_path):
    r = RoadRouter(tmp_path / "nope.sqlite")
    assert not r.available
    with pytest.raises(RoutingError):
        r.plan(START, DEST)
    with pytest.raises(RoutingError):
        r.roads_in_bbox(35.7, 51.3, 35.8, 51.4, 14)


def test_roads_in_bbox_tiers_zoom_and_limit(router):
    box = (35.70, 51.30, 35.74, 51.36)
    assert router.roads_in_bbox(*box, zoom=11)["zoom_needed"] is True
    assert router.roads_in_bbox(*box, zoom=11)["features"] == []
    z12 = router.roads_in_bbox(*box, zoom=12)               # فقط motorway/primary/secondary
    assert {f["t"] for f in z12["features"]} <= {"motorway", "secondary", "primary", "trunk"}
    z15 = router.roads_in_bbox(*box, zoom=15)
    types = [f["t"] for f in z15["features"]]
    assert "residential" in types
    assert types.index("motorway") < types.index("residential")      # مهم‌ترین‌ها اول
    small = router.roads_in_bbox(*box, zoom=15, limit=5)
    assert len(small["features"]) == 5 and small["truncated"] is True
    assert router.roads_in_bbox(10, 10, 11, 11, zoom=15)["features"] == []


# ------------------------------------------------------------ سرور و موتور

@pytest.fixture
def lab(tmp_path, db):
    app = LabApp(history_dir=tmp_path / "h", roads_db=db)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{srv.server_address[1]}"
    app.close()
    srv.shutdown()
    srv.server_close()


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_route_and_roads_endpoints(lab):
    app, base = lab
    q = f"start={START[0]},{START[1]}&dest={DEST[0]},{DEST[1]}&road_type=urban"
    status, r = get(base, "/api/route?" + q)
    assert status == 200 and r["method"] == "roads" and r["warning"] is None
    assert len(r["points"]) > 20 and r["length_m"] > 2 * haversine_m(START, DEST)
    assert get(base, "/api/route?start=1,2")[0] == 400
    assert get(base, "/api/route?start=a,b&dest=1,2")[0] == 400
    status, roads = get(base, "/api/roads?south=35.70&west=51.30&north=35.72&east=51.33&zoom=15")
    assert status == 200 and roads["features"]
    assert get(base, "/api/roads?south=x")[0] == 400
    assert get(base, "/api/config")[1]["roads_available"] is True


def test_no_database_falls_back_to_straight_line_with_explicit_warning(tmp_path):
    app = LabApp(history_dir=tmp_path, roads_db=tmp_path / "missing.sqlite")
    r = app.route_preview(START, DEST, "urban")
    assert r["method"] == "straight" and "خط مستقیم" in r["warning"]
    assert len(r["points"]) == 2
    info = app.start({"scenario": "normal_driving", "start": list(START),
                      "destination": list(DEST), "road_type": "urban", "duration_s": 5})
    assert info["route_method"] == "straight" and info["warning"]
    app.close()
    assert app.router.available is False


def test_simulated_vehicle_stays_on_the_road_and_speed_follows_road_type(tmp_path, db):
    app = LabApp(history_dir=tmp_path, roads_db=db)
    planned = app.plan_route(START, DEST, "urban")
    assert planned["method"] == "roads"
    e = SimulationEngine("normal_driving", route=planned["route"], duration_s=120)
    e.run_to_end()
    pts = [(f["gps"]["latitude"], f["gps"]["longitude"]) for f in e.frames]
    assert all(on_road(p, tol_m=3.0) for p in pts)         # خودرو هیچ‌وقت از جاده بیرون نمی‌رود
    assert max(f["obd"]["speed"] for f in e.frames) < 60    # شهری ≈ ۴۲، نه ۹۸ بزرگراهی
    assert {f["road_type"] for f in e.frames} == {"urban"}
    app.close()


def test_route_is_cached_and_deterministic(tmp_path, db):
    app = LabApp(history_dir=tmp_path, roads_db=db)
    a = app.plan_route(START, DEST, "urban")
    t = time.time()
    b = app.plan_route(START, DEST, "urban")
    assert a is b and time.time() - t < 0.05
    for i in range(10):                                    # حافظه‌ی نهان محدود است
        app.plan_route(node(20, 5 + i), node(20, 20 + i), "urban")
    assert len(app._route_cache) <= 8
    app.close()
