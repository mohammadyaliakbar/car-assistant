import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.detectors.event_detector import DetectionEvent, Severity
from app.motion import (
    CrashDetector,
    CrashMonitor,
    CrashResponder,
    MotionSample,
    QueueMotionSource,
    SimulatedMotionSource,
    VehicleSignal,
    parse_message,
)
from app.motion.models import G

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def ms(offset_s=0.0):
    return (T0 + timedelta(seconds=offset_s)).timestamp() * 1000.0


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def crash_event(offset=0.0):
    return DetectionEvent(
        detector="crash", event_type="crash_suspected", severity=Severity.CRITICAL,
        message="Possible crash", vehicle_id="car_a",
        timestamp=T0 + timedelta(seconds=offset),
        details={"previous_speed_kmh": 60.0},
    )


def other_event(event_type="roadside_emergency_initial", message="guidance"):
    return DetectionEvent(
        detector="roadside_emergency", event_type=event_type,
        severity=Severity.WARNING, message=message, vehicle_id="car_a",
        timestamp=T0,
    )


# ---------------------------------------------------------------- قالب پیام

def test_parse_motion_message_with_and_without_gyro():
    s = parse_message({"t": ms(), "accel": [1.0, 2.0, 9.8], "gyro": [0.1, 0.2, 0.3]})
    assert isinstance(s, MotionSample)
    assert s.timestamp == T0
    assert (s.accel_x, s.accel_y, s.accel_z) == (1.0, 2.0, 9.8)
    assert (s.gyro_x, s.gyro_y, s.gyro_z) == (0.1, 0.2, 0.3)

    no_gyro = parse_message({"t": ms(), "accel": [0, 0, 9.8]})
    assert no_gyro.gyro_x is None and no_gyro.gyro_magnitude_rad_s is None
    null_gyro = parse_message({"t": ms(), "accel": [0, 0, 9.8], "gyro": None})
    assert null_gyro.gyro_x is None


def test_parse_vehicle_message_keeps_zero_and_none_distinct():
    sig = parse_message({"t": ms(), "speed_kmh": 0.0, "rpm": None})
    assert isinstance(sig, VehicleSignal)
    assert sig.speed_kmh == 0.0      # صفر یعنی ایستاده، نه نامعلوم
    assert sig.rpm is None
    assert parse_message({"t": ms(), "rpm": 800}).speed_kmh is None


def test_parse_accepts_json_text_and_bytes():
    text = json.dumps({"t": ms(), "accel": [0, 0, 9.8]})
    assert isinstance(parse_message(text), MotionSample)
    assert isinstance(parse_message(text.encode()), MotionSample)


@pytest.mark.parametrize("bad", [
    {"accel": [0, 0, 9.8]},                                   # بدون t
    {"t": 1_767_268_800, "accel": [0, 0, 9.8]},               # ثانیه، نه میلی‌ثانیه
    {"t": 1_767_268_800_000_000_000, "accel": [0, 0, 9.8]},   # نانوثانیه
    {"t": True, "accel": [0, 0, 9.8]},
    {"t": ms(), "accel": [0, 9.8]},                           # دو عضو
    {"t": ms(), "accel": ["a", "b", "c"]},
    {"t": ms(), "accel": [0, 0, float("nan")]},
    {"t": ms(), "accel": [0, 0, 9.8], "gyro": [1, 2]},
    {"t": ms()},                                              # نه حسگر نه سرعت
    {"t": ms(), "speed_kmh": None, "rpm": None},              # هیچ مقداری ندارد
    {"t": ms(), "speed_kmh": -5.0},
    {"t": ms(), "rpm": float("inf")},
    "not json",
    [1, 2, 3],
    None,
])
def test_parse_rejects_invalid_messages(bad):
    with pytest.raises(ValueError):
        parse_message(bad)


# ---------------------------------------------------------------- صف

def test_queue_push_message_never_raises_and_counts_rejections():
    q = QueueMotionSource()
    assert q.push_message({"t": 5, "accel": [0, 0, 1]}) is False
    assert q.push_message(None) is False
    assert q.push_message(12345) is False
    assert q.rejected == 3
    assert q.read_samples() == []


def test_queue_drains_in_order_and_clears():
    q = QueueMotionSource()
    for i in range(3):
        assert q.push_message({"t": ms(i), "accel": [0, 0, 9.8]})
    q.push_message({"t": ms(1), "speed_kmh": 50.0})
    assert [s.timestamp for s in q.read_samples()] == [T0 + timedelta(seconds=i) for i in range(3)]
    assert q.read_samples() == []
    assert len(q.read_vehicle_signals()) == 1
    assert q.read_vehicle_signals() == []


def test_queue_overflow_drops_oldest():
    q = QueueMotionSource(max_samples=3)
    for i in range(5):
        q.push_message({"t": ms(i), "accel": [0, 0, 9.8]})
    samples = q.read_samples()
    assert [s.timestamp for s in samples] == [T0 + timedelta(seconds=i) for i in (2, 3, 4)]
    assert q.dropped == 2


def test_queue_convenience_pushers():
    q = QueueMotionSource()
    assert q.push_sensor(ms(), 0.0, 0.0, 9.8)
    assert q.push_sensor(ms(1), 0.0, 0.0, 9.8, 0.1, 0.2, 0.3)
    assert q.push_vehicle(ms(), speed_kmh=40.0)
    first, second = q.read_samples()
    assert first.gyro_x is None and second.gyro_z == 0.3
    assert q.read_vehicle_signals()[0].speed_kmh == 40.0


# ---------------------------------------------------------------- شبیه‌ساز درون‌برنامه‌ای

def test_simulated_source_releases_samples_in_real_time():
    clock = FakeClock()
    src = SimulatedMotionSource("normal_driving", clock=clock)
    assert src.read_samples() == [] or all(s.timestamp <= clock() for s in src.read_samples())
    clock.advance(1.0)
    samples = src.read_samples()
    assert 45 <= len(samples) <= 55          # حدود ۵۰ هرتز
    assert all(s.timestamp <= clock() for s in samples)
    assert src.read_samples() == []          # تکراری تحویل نمی‌شود
    assert not src.finished


def test_simulated_source_provides_vehicle_signals_and_finishes():
    clock = FakeClock()
    src = SimulatedMotionSource("normal_driving", clock=clock)
    clock.advance(5.0)
    signals = src.read_vehicle_signals()
    assert signals and signals[0].speed_kmh == 60.0 and signals[0].rpm is not None
    clock.advance(60.0)
    src.read_samples()
    assert src.finished


def test_simulated_source_without_obd_has_no_vehicle_signals():
    clock = FakeClock()
    src = SimulatedMotionSource("crash_no_speed", clock=clock)
    clock.advance(40.0)
    assert src.read_samples()
    assert src.read_vehicle_signals() == []


def test_simulated_source_unknown_scenario_raises():
    with pytest.raises(ValueError):
        SimulatedMotionSource("nope", clock=FakeClock()).read_samples()


# ---------------------------------------------------------------- CrashResponder

def make_responder(**kwargs):
    clock = FakeClock()
    log = {"emergency": [], "sent": [], "prompts": []}
    responder = CrashResponder(
        emergency_handler=log["emergency"].append,
        send_alert=log["sent"].append,
        prompt=log["prompts"].append,
        clock=clock,
        **kwargs,
    )
    return responder, log, clock


def test_crash_activates_emergency_and_prompts_but_sends_nothing():
    responder, log, _ = make_responder()
    request = responder.on_crash(crash_event())
    assert log["emergency"] == [crash_event()]
    assert log["prompts"] == [request]
    assert log["sent"] == []


def test_confirmation_sends_held_messages():
    responder, log, _ = make_responder()
    responder.on_crash(crash_event())
    responder.submit_alert(other_event("roadside_emergency_initial"))
    assert log["sent"] == []
    assert responder.answer(True) is True
    assert {e.event_type for e in log["sent"]} == {"crash_suspected", "roadside_emergency_initial"}


def test_after_confirmation_new_alerts_are_sent_immediately():
    responder, log, _ = make_responder()
    responder.on_crash(crash_event())
    responder.answer(True)
    log["sent"].clear()
    responder.submit_alert(other_event("roadside_emergency_final"))
    assert [e.event_type for e in log["sent"]] == ["roadside_emergency_final"]


def test_only_latest_held_message_per_type_is_sent():
    responder, log, _ = make_responder()
    responder.on_crash(crash_event())
    responder.submit_alert(other_event(message="older"))
    responder.submit_alert(other_event(message="newer"))
    responder.answer(True)
    initials = [e for e in log["sent"] if e.event_type == "roadside_emergency_initial"]
    assert [e.message for e in initials] == ["newer"]


def test_declined_sends_nothing_and_drops_later_alerts():
    responder, log, _ = make_responder()
    responder.on_crash(crash_event())
    assert responder.answer(False) is True
    responder.submit_alert(other_event())
    assert log["sent"] == []


def test_timeout_skip_sends_nothing():
    responder, log, clock = make_responder(timeout_s=30.0, on_timeout="skip")
    responder.on_crash(crash_event())
    clock.advance(31)
    responder.tick()
    assert log["sent"] == []
    assert responder.pending_requests == []


def test_late_confirmation_after_timeout_still_sends():
    responder, log, clock = make_responder(
        timeout_s=30.0, on_timeout="skip", late_confirm_window_s=600.0
    )
    responder.on_crash(crash_event())
    clock.advance(31)
    responder.tick()
    clock.advance(100)
    responder.tick()
    assert responder.answer(True) is True
    assert [e.event_type for e in log["sent"]] == ["crash_suspected"]


def test_confirmation_after_late_window_is_ignored():
    responder, log, clock = make_responder(
        timeout_s=30.0, on_timeout="skip", late_confirm_window_s=60.0
    )
    responder.on_crash(crash_event())
    clock.advance(31)
    responder.tick()
    clock.advance(120)
    responder.tick()
    assert responder.answer(True) is False
    assert log["sent"] == []


def test_default_policy_is_send_after_sixty_seconds():
    responder, log, clock = make_responder()
    assert responder.on_timeout == "send" and responder.timeout_s == 60.0
    responder.on_crash(crash_event())
    clock.advance(59)
    responder.tick()
    assert log["sent"] == []
    clock.advance(2)
    responder.tick()
    assert [e.event_type for e in log["sent"]] == ["crash_suspected"]


def test_timeout_send_policy_sends_without_confirmation():
    responder, log, clock = make_responder(timeout_s=30.0, on_timeout="send")
    responder.on_crash(crash_event())
    clock.advance(31)
    responder.tick()
    assert [e.event_type for e in log["sent"]] == ["crash_suspected"]
    log["sent"].clear()
    responder.submit_alert(other_event())
    assert len(log["sent"]) == 1


def test_confirmation_before_timeout_is_not_repeated_by_tick():
    responder, log, clock = make_responder(timeout_s=30.0, on_timeout="send")
    responder.on_crash(crash_event())
    responder.answer(True)
    sent = len(log["sent"])
    clock.advance(31)
    responder.tick()
    assert len(log["sent"]) == sent


def test_answer_without_request_returns_false():
    responder, log, _ = make_responder()
    assert responder.answer(True) is False
    assert log["sent"] == []


def test_answer_with_specific_request_id():
    responder, log, _ = make_responder()
    request = responder.on_crash(crash_event())
    assert responder.answer(True, request_id="wrong") is False
    assert responder.answer(True, request_id=request.id) is True


def test_new_incident_resets_permission():
    responder, log, _ = make_responder()
    responder.on_crash(crash_event())
    responder.answer(True)
    log["sent"].clear()
    responder.on_crash(crash_event(100))        # حادثه‌ی تازه، تأیید تازه لازم است
    responder.submit_alert(other_event())
    assert log["sent"] == []


def test_failures_in_callbacks_do_not_propagate():
    def boom(_):
        raise RuntimeError("x")

    responder = CrashResponder(boom, boom, prompt=boom, clock=FakeClock())
    responder.on_crash(crash_event())           # بدون استثنا
    responder.answer(True)
    responder.submit_alert(other_event())


def test_invalid_timeout_policy_raises():
    with pytest.raises(ValueError):
        CrashResponder(lambda e: None, lambda e: None, on_timeout="maybe")
    with pytest.raises(ValueError):
        CrashResponder(lambda e: None, lambda e: None, timeout_s=0)


# ---------------------------------------------------------------- CrashMonitor

def build_monitor(source, clock, **responder_kwargs):
    responder, log, _ = make_responder(**responder_kwargs)
    responder._clock = clock
    monitor = CrashMonitor(source, CrashDetector("car_a"), responder, clock=clock)
    return monitor, responder, log


def run_simulation(scenario, seconds=35.0, step_s=0.1, **kwargs):
    clock = FakeClock()
    monitor, responder, log = build_monitor(
        SimulatedMotionSource(scenario, clock=clock), clock, **kwargs
    )
    detected = []
    for _ in range(int(seconds / step_s)):
        clock.advance(step_s)
        detected += monitor.step()
    return monitor, responder, log, detected


def test_simulated_crash_activates_emergency_and_waits_for_driver():
    _, responder, log, detected = run_simulation("crash", seconds=35.0)
    assert len(detected) == 1 and detected[0].event_type == "crash_suspected"
    assert log["emergency"] == detected
    assert len(log["prompts"]) == 1
    assert log["sent"] == []                     # بدون تأیید چیزی نرفته است
    assert responder.answer(True)
    assert [e.event_type for e in log["sent"]] == ["crash_suspected"]


def test_simulated_crash_declined_sends_nothing():
    _, responder, log, _ = run_simulation("crash")
    responder.answer(False)
    assert log["sent"] == []


def test_simulated_phone_drop_does_not_activate_anything():
    _, _, log, detected = run_simulation("phone_drop")
    assert detected == [] and log["emergency"] == [] and log["prompts"] == []


def test_idle_poll_closes_window_when_samples_stop():
    clock = FakeClock()
    q = QueueMotionSource()
    monitor, _, log = build_monitor(q, clock)

    q.push_vehicle(ms(0), speed_kmh=60.0, rpm=2300.0)
    q.push_sensor(ms(1), 0.0, 0.0, 10 * G, 5.0, 0.0, 0.0)
    q.push_vehicle(ms(3), speed_kmh=0.0, rpm=0.0)
    clock.advance(1.2)
    assert monitor.step() == []                 # هنوز در مهلت و داده تازه است

    clock.advance(11.0)                          # ۱۲٫۲ ثانیه؛ داده‌ای نمی‌آید
    events = monitor.step()
    assert len(events) == 1 and events[0].severity == Severity.CRITICAL
    assert log["emergency"] == events


def test_poll_is_not_used_while_samples_keep_arriving():
    clock = FakeClock()
    q = QueueMotionSource()
    monitor, _, log = build_monitor(q, clock)
    q.push_vehicle(ms(0), speed_kmh=60.0, rpm=2300.0)
    q.push_sensor(ms(1), 0.0, 0.0, 10 * G, 5.0, 0.0, 0.0)
    q.push_vehicle(ms(3), speed_kmh=0.0, rpm=0.0)
    clock.advance(1.2)
    monitor.step()
    # ساعت دیواری جلو می‌رود ولی نمونه‌ی تازه (با زمان‌نگاری قدیمی) می‌رسد
    for _ in range(20):
        clock.advance(0.5)
        q.push_sensor(ms(1.5), 0.0, 0.0, 9.8, 0.0, 0.0, 0.0)
        assert monitor.step() == []


def test_update_vehicle_from_another_thread_reaches_detector():
    clock = FakeClock()
    monitor, _, _ = build_monitor(QueueMotionSource(), clock)
    monitor.update_vehicle(T0, 50.0, 2000.0)
    assert len(monitor.detector._speeds) == 1 and len(monitor.detector._rpms) == 1


def test_thread_lifecycle_and_survives_step_errors():
    class FlakySource:
        def __init__(self):
            self.calls = 0
            self.closed = False

        def read_vehicle_signals(self):
            return []

        def read_samples(self):
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError("sensor glitch")
            return []

        def close(self):
            self.closed = True

    source = FlakySource()
    responder, _, _ = make_responder()
    monitor = CrashMonitor(source, CrashDetector("car_a"), responder, poll_interval_s=0.005)
    monitor.start()
    deadline = time.monotonic() + 3.0
    while source.calls < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()
    assert source.calls >= 5                     # بعد از خطا ادامه داد
    assert source.closed
    assert not monitor._thread.is_alive()
