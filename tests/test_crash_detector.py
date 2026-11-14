from datetime import datetime, timedelta, timezone

import pytest

from app.detectors.event_detector import Severity
from app.motion import (
    SCENARIOS,
    CrashConfig,
    CrashDetector,
    MotionSample,
    MotionSimulator,
)
from app.motion.models import G

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def run_scenario(name, detector=None, **sim_kwargs):
    """سناریو را به detector می‌دهد؛ (رویدادها، زمان‌های انتشار) را برمی‌گرداند."""
    detector = detector or CrashDetector("car_a")
    events, emitted_at = [], []
    for step in MotionSimulator(**sim_kwargs).scenario(name):
        detector.update_vehicle(step.sample.timestamp, step.speed_kmh, step.rpm)
        new = detector.process(step.sample)
        events += new
        emitted_at += [step.sample.timestamp] * len(new)
    return events, emitted_at


def sample(t_s, mag_g=1.0, gyro=None):
    """نمونه‌ی ساده؛ بزرگی شتاب = mag_g * G روی محور z."""
    kwargs = {}
    if gyro is not None:
        kwargs = {"gyro_x": gyro, "gyro_y": 0.0, "gyro_z": 0.0}
    return MotionSample(
        timestamp=T0 + timedelta(seconds=t_s),
        accel_x=0.0, accel_y=0.0, accel_z=mag_g * G, **kwargs,
    )


def at(t_s):
    return T0 + timedelta(seconds=t_s)


# ---------------------------------------------------------------- مدل

def test_sample_requires_timezone():
    with pytest.raises(ValueError):
        MotionSample(datetime(2026, 1, 1), 0.0, 0.0, 9.8)


def test_sample_rejects_non_finite():
    with pytest.raises(ValueError):
        MotionSample(T0, float("nan"), 0.0, 9.8)
    with pytest.raises(ValueError):
        MotionSample(T0, 0.0, 0.0, 9.8, gyro_x=float("inf"), gyro_y=0.0, gyro_z=0.0)


def test_sample_gyro_is_all_or_nothing():
    with pytest.raises(ValueError):
        MotionSample(T0, 0.0, 0.0, 9.8, gyro_x=1.0)


def test_sample_magnitudes():
    s = MotionSample(T0, 3.0, 4.0, 0.0, gyro_x=3.0, gyro_y=4.0, gyro_z=0.0)
    assert s.accel_magnitude_ms2 == pytest.approx(5.0)
    assert s.accel_magnitude_g == pytest.approx(5.0 / G)
    assert s.gyro_magnitude_rad_s == pytest.approx(5.0)
    assert MotionSample(T0, 0.0, 0.0, 9.8).gyro_magnitude_rad_s is None


# ---------------------------------------------------------------- سناریوها

@pytest.mark.parametrize(
    "name",
    [
        "normal_driving", "hard_brake", "pothole", "phone_drop",
        "low_speed_bump", "engine_stall",
    ],
)
def test_non_crash_scenarios_produce_no_event(name):
    events, _ = run_scenario(name)
    assert events == []


def test_crash_scenario_is_detected_as_critical():
    events, _ = run_scenario("crash")
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "crash_suspected"
    assert event.detector == "crash"
    assert event.vehicle_id == "car_a"
    assert event.severity == Severity.CRITICAL
    d = event.details
    assert d["confidence"] == "high"
    assert d["speed_drop"] is True and d["spin"] is True and d["still"] is True
    assert d["speed_before_kmh"] == 60.0 and d["speed_after_kmh"] == 0.0
    assert d["engine_off"] is True
    assert d["rpm_before"] == 2300.0 and d["rpm_after"] == 0.0
    assert d["peak_g"] >= 9.0


def test_crash_without_speed_is_low_confidence_warning():
    events, _ = run_scenario("crash_no_speed")
    assert len(events) == 1
    event = events[0]
    assert event.severity == Severity.WARNING
    d = event.details
    assert d["confidence"] == "low"
    assert d["speed_drop"] is None  # نامعلوم، نه False
    assert d["speed_before_kmh"] is None
    assert d["engine_off"] is None


def test_engine_off_with_unknown_speed_is_medium_confidence():
    events, _ = run_scenario("crash_engine_off")
    assert len(events) == 1
    d = events[0].details
    assert events[0].severity == Severity.WARNING
    assert d["confidence"] == "medium"
    assert d["speed_drop"] is None and d["engine_off"] is True
    assert d["rpm_before"] == 2300.0 and d["rpm_after"] == 0.0


def test_engine_still_running_does_not_reject_a_crash():
    events, _ = run_scenario("crash_engine_running")
    assert len(events) == 1
    assert events[0].severity == Severity.CRITICAL
    assert events[0].details["engine_off"] is False


def test_event_timestamp_is_impact_time_but_emitted_after_window():
    events, emitted_at = run_scenario("crash")
    impact = MotionSimulator().start + timedelta(seconds=15.0)
    assert events[0].timestamp == impact
    assert emitted_at[0] >= impact + timedelta(seconds=CrashConfig().confirm_window_s)


def test_unknown_scenario_raises():
    with pytest.raises(ValueError):
        list(MotionSimulator().scenario("nope"))


def test_all_declared_scenarios_exist():
    for name in SCENARIOS:
        assert next(iter(MotionSimulator().scenario(name))) is not None


def test_simulation_is_deterministic():
    a = [s.sample for s in MotionSimulator(seed=3).scenario("crash")]
    b = [s.sample for s in MotionSimulator(seed=3).scenario("crash")]
    assert a == b


# ---------------------------------------------------------------- منطق detector

def test_single_spike_without_speed_info_is_ignored():
    det = CrashDetector("car_a")
    events = [e for s in (sample(0), sample(0.02, 9.0), sample(0.04), sample(20.0))
              for e in det.process(s)]
    assert events == []


def test_known_speed_that_does_not_drop_rejects_impact():
    det = CrashDetector("car_a")
    for t in (0, 2, 4, 6, 8, 10, 12):
        det.update_speed(60.0, at(t))
    events = []
    events += det.process(sample(5.0, 9.0, gyro=6.0))
    for i in range(1, 400):
        events += det.process(sample(5.0 + i * 0.05, 1.0, gyro=0.0))
    assert events == []


def test_speed_drop_alone_gives_medium_warning():
    det = CrashDetector("car_a")
    det.update_speed(60.0, at(0.0))
    det.process(sample(1.0, 5.0))          # ضربه‌ی متوسط، بدون ژیروسکوپ
    det.update_speed(0.0, at(3.0))
    # نمونه‌های بعدی ناآرام‌اند (سکون ثابت نمی‌شود)
    events = []
    for i in range(1, 200):
        events += det.process(sample(1.0 + i * 0.05, 1.0 + (0.5 if i % 2 else -0.5)))
    assert len(events) == 1
    assert events[0].severity == Severity.WARNING
    assert events[0].details["confidence"] == "medium"
    assert events[0].details["still"] is False
    assert events[0].details["spin"] is None  # ژیروسکوپ نبود: نامعلوم


def test_stale_speed_before_impact_is_unknown():
    det = CrashDetector("car_a")
    det.update_speed(60.0, at(0.0))        # ۱۰ ثانیه قبل از ضربه: کهنه
    det.process(sample(10.0, 9.0))
    det.update_speed(0.0, at(12.0))
    events = []
    for i in range(1, 300):
        events += det.process(sample(10.0 + i * 0.05, 1.0))
    # speed_drop نامعلوم، ژیروسکوپ نامعلوم ⇒ رویداد نمی‌دهد
    assert events == []


def test_poll_closes_window_when_samples_stop():
    det = CrashDetector("car_a")
    det.update_speed(60.0, at(0.0))
    det.process(sample(1.0, 10.0, gyro=5.0))
    det.update_speed(0.0, at(3.0))
    assert det.poll(at(5.0)) == []          # هنوز در مهلت
    events = det.poll(at(1.0 + CrashConfig().confirm_window_s + 0.1))
    assert len(events) == 1
    assert events[0].severity == Severity.CRITICAL
    assert events[0].details["still"] is None  # نمونه‌ای بعد از ضربه نرسید


def test_cooldown_blocks_repeated_event():
    det = CrashDetector("car_a")
    det.update_speed(60.0, at(0.0))
    det.process(sample(1.0, 10.0, gyro=5.0))
    det.update_speed(0.0, at(3.0))
    assert len(det.poll(at(10.0))) == 1
    det.process(sample(12.0, 10.0, gyro=5.0))        # ضربه‌ی تکراری در cooldown
    assert det.poll(at(30.0)) == []


def test_out_of_order_sample_is_ignored():
    det = CrashDetector("car_a")
    det.process(sample(10.0))
    assert det.process(sample(5.0, 10.0)) == []
    assert det._pending is None


def test_reset_clears_state():
    det = CrashDetector("car_a")
    det.update_speed(60.0, at(0.0))
    det.process(sample(1.0, 10.0, gyro=5.0))
    det.reset()
    assert det._pending is None
    assert det.poll(at(60.0)) == []


def test_invalid_speed_values_are_ignored():
    det = CrashDetector("car_a")
    det.update_speed(None, at(0.0))
    det.update_speed(float("nan"), at(1.0))
    det.update_speed(-5.0, at(2.0))
    assert len(det._speeds) == 0


def test_includes_gravity_false_uses_zero_reference():
    cfg = CrashConfig(includes_gravity=False)
    det = CrashDetector("car_a", cfg)
    det.update_speed(60.0, at(0.0))
    det.process(sample(1.0, 10.0, gyro=5.0))
    det.update_speed(0.0, at(3.0))
    events = []
    for i in range(1, 200):
        events += det.process(sample(1.0 + i * 0.05, 0.0, gyro=0.0))  # شتاب خطی ≈ ۰
    assert events and events[0].details["still"] is True


# ---------------------------------------------------------------- خاموشی موتور

def noisy(det, start_s, count=200):
    """نمونه‌های ناآرام بعد از ضربه تا «سکون» ثابت نشود."""
    events = []
    for i in range(1, count):
        events += det.process(sample(start_s + i * 0.05, 1.0 + (0.5 if i % 2 else -0.5)))
    return events


def test_engine_off_alone_is_not_enough():
    # ضربه‌ی متوسط، سرعت معلوم و ثابت، موتور خاموش شد ⇒ رویداد نه
    det = CrashDetector("car_a")
    det.update_vehicle(at(0.0), 60.0, 2300.0)
    det.process(sample(1.0, 5.0))
    det.update_vehicle(at(3.0), 60.0, 0.0)
    assert noisy(det, 1.0) == []


def test_severe_impact_with_engine_off_but_no_speed_drop_is_low():
    det = CrashDetector("car_a")
    det.update_vehicle(at(0.0), 60.0, 2300.0)
    det.process(sample(1.0, 9.0))
    det.update_vehicle(at(3.0), 60.0, 0.0)      # سرعت نیفتاد (مثلاً برخورد خراشی)
    events = noisy(det, 1.0)
    assert len(events) == 1
    assert events[0].severity == Severity.WARNING
    assert events[0].details["confidence"] == "low"
    assert events[0].details["speed_drop"] is False


def test_speed_drop_plus_engine_off_alone_is_critical():
    det = CrashDetector("car_a")
    det.update_vehicle(at(0.0), 60.0, 2300.0)
    det.process(sample(1.0, 5.0))               # بدون ژیروسکوپ، ضربه‌ی شدید نیست
    det.update_vehicle(at(3.0), 0.0, 0.0)
    events = noisy(det, 1.0)
    assert len(events) == 1
    assert events[0].severity == Severity.CRITICAL
    d = events[0].details
    assert d["spin"] is None and d["still"] is False and d["engine_off"] is True


def test_engine_already_off_before_impact_is_unknown_not_true():
    det = CrashDetector("car_a")
    det.update_vehicle(at(0.0), None, 0.0)      # مثلاً خودروی برقی یا ایستاده
    det.process(sample(1.0, 10.0, gyro=5.0))
    det.update_vehicle(at(3.0), None, 0.0)
    events = det.poll(at(12.0))
    assert events == []                          # بدون سرعت، فقط یک سیگنال گوشی کافی نیست


def test_stale_rpm_before_impact_is_unknown():
    det = CrashDetector("car_a")
    det.update_engine(2300.0, at(0.0))           # ۱۰ ثانیه قبل از ضربه
    det.process(sample(10.0, 5.0))
    det.update_engine(0.0, at(12.0))
    det.update_speed(0.0, at(12.0))
    events = noisy(det, 10.0)
    # سرعت پیش از ضربه نامعلوم و rpm کهنه ⇒ رویدادی نیست
    assert events == []


def test_no_rpm_after_impact_is_unknown():
    det = CrashDetector("car_a")
    det.update_vehicle(at(0.0), 60.0, 2300.0)
    det.process(sample(1.0, 9.0, gyro=5.0))
    det.update_speed(0.0, at(3.0))               # فقط سرعت رسید؛ rpm دیگر نیامد
    events = det.poll(at(12.0))
    assert events[0].details["engine_off"] is None


def test_update_vehicle_and_invalid_rpm():
    det = CrashDetector("car_a")
    det.update_vehicle(at(0.0), 50.0, 2000.0)
    assert len(det._speeds) == 1 and len(det._rpms) == 1
    det.update_engine(None, at(1.0))
    det.update_engine(float("nan"), at(2.0))
    det.update_engine(-1.0, at(3.0))
    assert len(det._rpms) == 1
