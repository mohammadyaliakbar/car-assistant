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
SECONDARY = {"elements": [way(STRAIGHT, highway="secondary")]}


class FakeGps:
    def get_fix(self):
        return GpsFix(
            latitude=LAT, longitude=LON, accuracy_m=5.0, speed_kmh=0.0,
            heading_deg=90.0, timestamp=NOW, source="test",
        )


def build():
    published = []
    service = RoadsideEmergencyService(
        gps=FakeGps(),
        road_analyzer=RoadAnalyzer(fetch=lambda q, t: SECONDARY),
        risk_analyzer=RiskAnalyzer(),
        recommender=RecommendationEngine(),
        contacts=EmergencyContacts(),
        vehicle="Saipa Pride",
        publish=published.append,
        submit=lambda task: task(),
        clock=lambda: NOW,
    )
    return service, published


def stall(offset=0, speed=40.0):
    return DetectionEvent(
        detector="stoppage", event_type="engine_stalled_while_moving",
        severity=Severity.WARNING, message="t", vehicle_id="car_a",
        timestamp=NOW + timedelta(seconds=offset),
        details={"previous_speed_kmh": speed, "rpm": 0.0},
    )


def crash(offset=0, speed=40.0):
    return DetectionEvent(
        detector="crash", event_type="crash_suspected",
        severity=Severity.CRITICAL, message="t", vehicle_id="car_a",
        timestamp=NOW + timedelta(seconds=offset),
        details={"previous_speed_kmh": speed},
    )


def test_crash_event_is_handled_in_two_phases():
    service, out = build()
    assert service.handle(crash()) is not None
    assert [r.phase for r in out] == ["initial", "final"]
    assert all(r.event_type == "crash_suspected" for r in out)


def test_crash_event_implies_crash_flag_without_passing_it():
    # جاده‌ی secondary با ۴۰ km/h بدون crash فقط MEDIUM است
    service, out = build()
    service.handle(crash(speed=40.0))
    assert out[1].assessment.level == RiskLevel.HIGH
    assert out[1].report is not None


def test_stall_event_alone_is_still_only_medium():
    service, out = build()
    service.handle(stall(speed=40.0))
    assert out[1].assessment.level == RiskLevel.MEDIUM


def test_crash_is_not_suppressed_by_a_recent_stall():
    service, out = build()
    assert service.handle(stall(0)) is not None
    assert service.handle(crash(5)) is not None
    assert [r.event_type for r in out].count("crash_suspected") == 2


def test_stall_is_not_suppressed_by_a_recent_crash():
    service, _ = build()
    assert service.handle(crash(0)) is not None
    assert service.handle(stall(5)) is not None


def test_repeated_crash_within_cooldown_is_ignored():
    service, out = build()
    assert service.handle(crash(0)) is not None
    assert service.handle(crash(30)) is None
    assert service.handle(crash(120)) is not None
