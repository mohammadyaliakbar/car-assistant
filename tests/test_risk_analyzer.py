from datetime import datetime, timedelta, timezone

from app.detectors.event_detector import DetectionEvent, Severity
from app.emergency import RiskAnalyzer, RiskLevel
from app.location import GpsFix, RoadContext

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
analyzer = RiskAnalyzer()


def ev(prev_speed=100.0):
    details = {"rpm": 0.0}
    if prev_speed is not None:
        details["previous_speed_kmh"] = prev_speed
    return DetectionEvent(
        detector="stoppage",
        event_type="engine_stalled_while_moving",
        severity=Severity.WARNING,
        message="test",
        vehicle_id="car_a",
        timestamp=NOW,
        details=details,
    )


def fix(accuracy=5.0, age=0.0, speed=0.0):
    return GpsFix(
        latitude=35.0, longitude=51.0,
        accuracy_m=accuracy, speed_kmh=speed,
        timestamp=NOW - timedelta(seconds=age), source="test",
    )


def road(**kw):
    kw.setdefault("data_quality", "ok")
    return RoadContext(**kw)


def run(event, f, r, **kw):
    return analyzer.assess(event, f, r, now=NOW, **kw)


# --- سناریوی ۱: پارکینگ ---
def test_parking_is_low():
    r = road(highway_type="service", is_parking_area=True)
    result = run(ev(10), fix(), r)
    assert result.level == RiskLevel.LOW


def test_parking_with_mediocre_accuracy_is_not_low():
    r = road(highway_type="service", is_parking_area=True)
    assert run(ev(10), fix(accuracy=35), r).level == RiskLevel.MEDIUM


# --- سناریوی ۲: خیابان شهری ---
def test_urban_street_is_medium():
    r = road(highway_type="secondary", maxspeed_kmh=50)
    assert run(ev(40), fix(), r).level == RiskLevel.MEDIUM


def test_quiet_residential_street_at_low_speed_is_low():
    r = road(highway_type="residential")
    assert run(ev(15), fix(speed=0), r).level == RiskLevel.LOW


# --- سناریوی ۳: آزادراه با محل توقف اضطراری نزدیک ---
def test_motorway_with_close_bay_reduced_one_level_only():
    r = road(highway_type="motorway", is_limited_access=True,
             has_emergency_bay_nearby=True, emergency_bay_distance_m=100)
    assert run(ev(100), fix(accuracy=5), r).level == RiskLevel.MEDIUM


def test_motorway_bay_does_not_reduce_with_poor_gps():
    r = road(highway_type="motorway", is_limited_access=True,
             has_emergency_bay_nearby=True, emergency_bay_distance_m=100)
    result = run(ev(100), fix(accuracy=150), r)
    assert result.level == RiskLevel.HIGH
    assert result.confidence == "low"


def test_far_bay_does_not_reduce():
    r = road(highway_type="motorway", is_limited_access=True,
             has_emergency_bay_nearby=True, emergency_bay_distance_m=900)
    assert run(ev(100), fix(), r).level == RiskLevel.HIGH


# --- سناریوی ۴: آزادراه بدون اطلاعات بیشتر (امن فرض نمی‌شود) ---
def test_motorway_without_bay_is_high():
    r = road(highway_type="motorway", is_limited_access=True)
    assert run(ev(110), fix(), r).level == RiskLevel.HIGH


def test_motorway_no_stopping_is_critical():
    r = road(highway_type="motorway", is_limited_access=True,
             no_stopping_tagged=True,
             has_emergency_bay_nearby=True, emergency_bay_distance_m=50)
    assert run(ev(110), fix(), r).level == RiskLevel.CRITICAL


def test_missing_bay_tag_is_not_treated_as_safe_or_unsafe():
    r = road(highway_type="motorway", is_limited_access=True,
             has_emergency_bay_nearby=None)
    assert run(ev(110), fix(), r).level == RiskLevel.HIGH


def test_trunk_with_unknown_access_treated_conservatively():
    r = road(highway_type="trunk", is_limited_access=None)
    assert run(ev(100), fix(), r).level == RiskLevel.HIGH


# --- سناریوی ۵: جاده‌ی کوهستانی ---
def test_winding_road_raises_one_level():
    r = road(highway_type="secondary", is_winding=True)
    assert run(ev(50), fix(), r).level == RiskLevel.HIGH


# --- سناریوی ۶: موقعیت نامشخص ---
def test_no_gps_is_unknown():
    r = road(highway_type="motorway", is_limited_access=True)
    assert run(ev(), None, r).level == RiskLevel.UNKNOWN


def test_stale_gps_is_unknown():
    r = road(highway_type="motorway", is_limited_access=True)
    assert run(ev(), fix(age=120), r).level == RiskLevel.UNKNOWN


def test_map_unavailable_is_unknown():
    assert run(ev(), fix(), RoadContext.unavailable("timeout")).level == RiskLevel.UNKNOWN
    assert run(ev(), fix(), None).level == RiskLevel.UNKNOWN


def test_poor_gps_on_street_is_unknown_not_low():
    r = road(highway_type="residential")
    assert run(ev(10), fix(accuracy=150), r).level == RiskLevel.UNKNOWN


def test_unknown_accuracy_on_street_is_unknown():
    r = road(highway_type="secondary")
    assert run(ev(40), fix(accuracy=None), r).level == RiskLevel.UNKNOWN


def test_unknown_highway_type_is_unknown():
    assert run(ev(), fix(), road(highway_type=None)).level == RiskLevel.UNKNOWN


# --- سایر قواعد ---
def test_high_speed_on_street_is_high():
    r = road(highway_type="secondary")
    assert run(ev(100), fix(), r).level == RiskLevel.HIGH


def test_crash_signal_raises_one_level_but_is_optional():
    r = road(highway_type="secondary")
    assert run(ev(40), fix(), r).level == RiskLevel.MEDIUM
    assert run(ev(40), fix(), r, crash_suspected=True).level == RiskLevel.HIGH


def test_missing_previous_speed_still_works():
    r = road(highway_type="motorway", is_limited_access=True)
    assert run(ev(None), fix(speed=None), r).level == RiskLevel.HIGH


def test_speed_mismatch_is_reported_and_higher_value_used():
    r = road(highway_type="secondary")
    result = run(ev(100), fix(speed=0), r)
    assert result.level == RiskLevel.HIGH
    assert any("اختلاف" in reason for reason in result.reasons)


