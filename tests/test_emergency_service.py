import math
from datetime import datetime, timedelta, timezone

from app.detectors.event_detector import DetectionEvent, Severity
from app.emergency import (
    EmergencyContacts,
    RecommendationEngine,
    RiskAnalyzer,
    RiskLevel,
    RoadsideEmergencyService,
)
from app.location import GpsFix, RoadAnalyzer

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
LAT, LON = 35.0, 51.0
M_PER_DEG = 111_194.93


def pt(east, north):
    return {
        "lat": LAT + north / M_PER_DEG,
        "lon": LON + east / (M_PER_DEG * math.cos(math.radians(LAT))),
    }


def way(points, **tags):
    return {"type": "way", "tags": tags, "geometry": [pt(*p) for p in points]}


STRAIGHT = [(-100, 3), (100, 3)]
MOTORWAY = {"elements": [way(STRAIGHT, highway="motorway")]}
SECONDARY = {"elements": [way(STRAIGHT, highway="secondary")]}


def make_fix(age=0.0):
    return GpsFix(
        latitude=LAT, longitude=LON, accuracy_m=5.0, speed_kmh=0.0,
        heading_deg=90.0, timestamp=NOW - timedelta(seconds=age), source="test",
    )


class FakeGps:
    def __init__(self, fix="default"):
        self.fix = make_fix() if fix == "default" else fix
        self.raises = False

    def get_fix(self):
        if self.raises:
            raise OSError("gps broke")
        return self.fix


def ev(offset=0, speed=100.0):
    return DetectionEvent(
        detector="stoppage", event_type="engine_stalled_while_moving",
        severity=Severity.WARNING, message="t", vehicle_id="car_a",
        timestamp=NOW + timedelta(seconds=offset),
        details={"previous_speed_kmh": speed, "rpm": 0.0},
    )


def build(fetch=None, gps=None, recommender=None, publish=None, submit=None, **kw):
    published = []
    service = RoadsideEmergencyService(
        gps=gps or FakeGps(),
        road_analyzer=RoadAnalyzer(fetch=fetch or (lambda q, t: MOTORWAY)),
        risk_analyzer=RiskAnalyzer(),
        recommender=recommender or RecommendationEngine(),
        contacts=EmergencyContacts(),
        vehicle="Saipa Pride",
        publish=publish or published.append,
        submit=submit or (lambda task: task()),
        clock=lambda: NOW,
        **kw,
    )
    return service, published


# --- مسیر اصلی ---
def test_two_phases_initial_unknown_then_final_motorway():
    service, out = build()
    service.handle(ev())
    assert [r.phase for r in out] == ["initial", "final"]
    assert out[0].assessment.level == RiskLevel.UNKNOWN
    assert out[0].instruction.id == "unknown_location_engine_stall"
    assert out[1].assessment.level == RiskLevel.HIGH
    assert out[1].instruction.id == "highway_engine_stall"
    assert out[1].report is not None and out[1].message


def test_initial_guidance_is_published_before_map_request():
    seen = []
    out = []

    def fetch(q, t):
        seen.append(len(out))
        return MOTORWAY

    service, out = build(fetch=fetch)
    service.handle(ev())
    assert seen == [1]


def test_initial_report_has_coordinates_when_gps_is_fresh():
    service, out = build()
    service.handle(ev())
    assert out[0].report.latitude == LAT


def test_handle_returns_initial_response():
    service, _ = build()
    assert service.handle(ev()).phase == "initial"


# --- رویدادهای پشتیبانی‌نشده و تکراری ---
def test_other_events_are_ignored():
    service, out = build()
    other = ev().model_copy(update={"event_type": "long_idle"})
    assert service.handle(other) is None
    assert out == []


def test_duplicate_within_cooldown_ignored_then_allowed_after():
    service, out = build()
    assert service.handle(ev(0)) is not None
    assert service.handle(ev(0)) is None
    assert service.handle(ev(30)) is None
    assert service.handle(ev(120)) is not None
    assert [r.phase for r in out] == ["initial", "final"] * 2


def test_stale_final_of_older_event_is_discarded():
    tasks = []
    service, out = build(submit=tasks.append)
    service.handle(ev(0))
    service.handle(ev(120))
    for task in tasks:
        task()
    assert [r.phase for r in out] == ["initial", "initial", "final"]
    assert out[2].event_timestamp == NOW + timedelta(seconds=120)


# --- اطلاعات ناقص ---
def test_map_failure_keeps_unknown_and_prepares_report():
    def boom(q, t):
        raise ConnectionError("offline")

    service, out = build(fetch=boom)
    service.handle(ev())
    assert out[1].assessment.level == RiskLevel.UNKNOWN
    assert out[1].instruction.id == "unknown_location_engine_stall"
    assert out[1].report is not None


def test_no_gps_is_unknown_and_report_has_no_coordinates():
    service, out = build(gps=FakeGps(fix=None))
    service.handle(ev())
    assert out[0].assessment.level == RiskLevel.UNKNOWN
    assert out[0].report.latitude is None
    assert out[1].assessment.level == RiskLevel.UNKNOWN


def test_stale_gps_is_unknown_with_gps_reason():
    service, out = build(gps=FakeGps(make_fix(age=120)))
    service.handle(ev())
    assert out[0].assessment.level == RiskLevel.UNKNOWN
    assert "قدیمی" in out[0].assessment.reasons[0]
    assert out[1].assessment.level == RiskLevel.UNKNOWN


def test_gps_that_arrives_late_is_used_in_final_phase():
    gps = FakeGps(fix=None)
    tasks = []
    service, out = build(gps=gps, submit=tasks.append)
    service.handle(ev())
    gps.fix = make_fix()
    tasks[0]()
    assert out[1].assessment.level == RiskLevel.HIGH


# --- خطر متوسط و crash ---
def test_medium_risk_prepares_no_report():
    service, out = build(fetch=lambda q, t: SECONDARY)
    service.handle(ev(speed=40))
    assert out[1].assessment.level == RiskLevel.MEDIUM
    assert out[1].report is None


def test_crash_flag_raises_risk_and_prepares_report():
    service, out = build(fetch=lambda q, t: SECONDARY)
    service.handle(ev(speed=40), crash_suspected=True)
    assert out[1].assessment.level == RiskLevel.HIGH
    assert out[1].report is not None


# --- پایداری ---
def test_publish_failure_does_not_propagate():
    def bad_publish(response):
        raise RuntimeError("ui crashed")

    service, _ = build(publish=bad_publish)
    assert service.handle(ev()) is not None


def test_gps_exception_does_not_propagate():
    gps = FakeGps()
    gps.raises = True
    service, out = build(gps=gps)
    service.handle(ev())
    assert out[0].assessment.level == RiskLevel.UNKNOWN


def test_internal_failure_still_publishes_safe_guidance():
    class Broken:
        def select(self, *args, **kwargs):
            raise RuntimeError("boom")

    service, out = build(recommender=Broken())
    service.handle(ev())
    assert len(out) == 1                       # فاز دوم هم خراب شد، ولی بیرون نیامد
    assert out[0].instruction.id == "last_resort"
    assert out[0].assessment.level == RiskLevel.UNKNOWN