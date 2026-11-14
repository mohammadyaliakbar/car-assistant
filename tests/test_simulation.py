import io
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.detectors.event_detector import DetectionEvent, Severity
from app.motion import CrashDetector, CrashMonitor, CrashResponder
from app.motion.console_confirmation import (
    ConsoleAnswerListener,
    make_console_prompt,
    parse_answer,
)
from app.motion.simulator import SCENARIOS as MOTION_SCENARIOS
from app.simulation import (
    SCENARIOS,
    DriverBehavior,
    SimulatedDriver,
    SimulatedOBDClient,
    SimulatedWorld,
    SimulationOptions,
    WorldGpsSource,
    resolve_crash_policy,
)
from app.simulation.world import OBD_FIELDS

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def world(scenario, **kwargs):
    clock = FakeClock()
    return SimulatedWorld(scenario, clock=clock, **kwargs), clock


def at(w, clock, seconds):
    """زمان را به «seconds» ثانیه بعد از شروع می‌برد و عکس OBD را می‌دهد."""
    clock.now = w.start + timedelta(seconds=seconds)
    return w.obd_snapshot()


# ---------------------------------------------------------------- سناریوها

def test_unknown_scenario_raises():
    with pytest.raises(ValueError):
        SimulatedWorld("nope")


def test_every_scenario_maps_to_a_real_motion_scenario():
    for name, definition in SCENARIOS.items():
        assert definition.motion in MOTION_SCENARIOS, name
        assert definition.description


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_snapshot_has_exactly_the_obd_fields_at_any_time(name):
    w, clock = world(name)
    for seconds in (0, 5, 14, 16, 20, 40, 120):
        snap = at(w, clock, seconds)
        assert set(snap) == set(OBD_FIELDS)


def test_normal_snapshot_values():
    w, clock = world("normal")
    snap = at(w, clock, 5)
    assert snap["speed"] == 60.0 and snap["rpm"] >= 500
    assert snap["coolant_temperature_c"] == 90.0
    assert snap["trouble_codes"] == []
    assert snap["diagnostic_status"] == {"mil_on": False, "dtc_count": 0}
    assert snap["control_module_voltage_v"] == 14.1


def test_crash_obd_timeline():
    w, clock = world("crash")
    before = at(w, clock, 14)
    assert before["speed"] == 60.0 and before["rpm"] > 500
    after = at(w, clock, 18)
    assert after["speed"] == 0.0 and after["rpm"] == 0.0
    assert after["control_module_voltage_v"] == 12.4        # موتور خاموش


def test_crash_with_engine_running_keeps_idle_rpm():
    w, clock = world("crash_engine_running")
    after = at(w, clock, 18)
    assert after["speed"] == 0.0 and after["rpm"] >= 500


def test_engine_stall_is_engine_off_while_still_moving():
    w, clock = world("engine_stall")
    snap = at(w, clock, 16)
    assert snap["rpm"] == 0.0 and snap["speed"] > 10.0


def test_overheat_ramps_through_warning_and_critical():
    w, clock = world("overheat")
    assert at(w, clock, 10)["coolant_temperature_c"] == 90.0
    assert 105 <= at(w, clock, 15 + 13)["coolant_temperature_c"] < 115
    assert at(w, clock, 15 + 22)["coolant_temperature_c"] >= 115
    assert at(w, clock, 15 + 600)["coolant_temperature_c"] == 125.0


def test_trouble_code_appears_after_event_time():
    w, clock = world("trouble_code")
    assert at(w, clock, 10)["trouble_codes"] == []
    snap = at(w, clock, 20)
    assert snap["trouble_codes"] == ["P0300"]
    assert snap["diagnostic_status"] == {"mil_on": True, "dtc_count": 1}


def test_low_voltage_after_event_time_only_with_engine_on():
    w, clock = world("low_voltage")
    assert at(w, clock, 10)["control_module_voltage_v"] == 14.1
    assert at(w, clock, 20)["control_module_voltage_v"] == 12.2


def test_obd_unavailable_scenario_is_all_none_not_zero():
    w, clock = world("crash_phone_only")
    snap = at(w, clock, 5)
    assert all(v is None for v in snap.values())


def test_event_time_is_configurable():
    w, clock = world("trouble_code", event_time_s=3.0)
    assert at(w, clock, 4)["trouble_codes"] == ["P0300"]


def test_world_finishes_after_duration():
    w, clock = world("normal", duration_s=10.0)
    at(w, clock, 5)
    assert not w.finished
    at(w, clock, 11)
    assert w.finished


# ---------------------------------------------------------------- آداپتورها

def test_obd_client_matches_real_interface():
    w, clock = world("normal")
    client = SimulatedOBDClient(w)
    clock.advance(5)
    data = client.read_vehicle_data()
    assert isinstance(data, dict) and set(data) == set(OBD_FIELDS)
    client.close()


def test_gps_follows_world_speed_and_position():
    w, clock = world("crash", latitude=35.5, longitude=51.5)
    gps = WorldGpsSource(w)
    clock.advance(5)
    fix = gps.get_fix()
    assert (fix.latitude, fix.longitude) == (35.5, 51.5)
    assert fix.speed_kmh == 60.0 and fix.source == "SIMULATED"
    clock.advance(20)
    assert gps.get_fix().speed_kmh == 0.0


@pytest.mark.parametrize("mode", ["good", "poor_accuracy", "stale", "no_fix"])
def test_gps_modes_are_preserved(mode):
    w, _ = world("normal", gps_mode=mode)
    fix = WorldGpsSource(w).get_fix()
    if mode == "no_fix":
        assert fix is None
    elif mode == "poor_accuracy":
        assert fix.accuracy_m == 150.0
    elif mode == "stale":
        assert fix.age_s() >= 100
    else:
        assert fix.accuracy_m == 5.0


def test_motion_source_is_anchored_to_world_start_and_has_no_vehicle_signals():
    w, clock = world("crash")
    source = w.motion_source()
    clock.advance(1.0)
    samples = source.read_samples()
    assert 45 <= len(samples) <= 55
    assert samples[0].timestamp >= w.start
    assert source.read_vehicle_signals() == []


# ---------------------------------------------------------------- سیاست تأیید

def test_policy_defaults_to_send_after_sixty_seconds():
    assert resolve_crash_policy() == (60.0, "send")


def test_policy_priority_cli_over_simulation_over_settings():
    sim = SimulationOptions("crash", confirm_timeout_s=20.0, on_timeout="skip")
    assert resolve_crash_policy(simulation=sim) == (20.0, "skip")
    assert resolve_crash_policy(simulation=sim, settings_timeout_s=99.0,
                                settings_on_timeout="send") == (20.0, "skip")
    assert resolve_crash_policy(cli_timeout_s=5.0, cli_on_timeout="send",
                                simulation=sim) == (5.0, "send")
    assert resolve_crash_policy(settings_timeout_s=45.0,
                                settings_on_timeout="skip") == (45.0, "skip")


def test_policy_partial_overrides_mix_sources():
    sim = SimulationOptions("crash", confirm_timeout_s=20.0)
    assert resolve_crash_policy(cli_on_timeout="skip", simulation=sim) == (20.0, "skip")


def test_policy_rejects_invalid_values():
    with pytest.raises(ValueError):
        resolve_crash_policy(cli_timeout_s=0)
    with pytest.raises(ValueError):
        resolve_crash_policy(cli_timeout_s=-3)
    with pytest.raises(ValueError):
        resolve_crash_policy(cli_on_timeout="maybe")


def test_driver_behavior_validation():
    with pytest.raises(ValueError):
        DriverBehavior("maybe")
    with pytest.raises(ValueError):
        DriverBehavior("confirm", delay_s=-1)


# ---------------------------------------------------------------- راننده‌ی شبیه‌سازی‌شده

class FakeResponder:
    def __init__(self):
        self.calls = []
        self.event = threading.Event()

    def answer(self, confirmed, request_id=None):
        self.calls.append((confirmed, request_id))
        self.event.set()
        return True


class Req:
    id = "r1"


def test_simulated_driver_confirms_after_delay():
    responder = FakeResponder()
    driver = SimulatedDriver(DriverBehavior("confirm", delay_s=0.05))
    driver.attach(responder)
    driver.on_prompt(Req())
    assert responder.calls == []                      # هنوز پاسخ نداده
    assert responder.event.wait(2.0)
    assert responder.calls == [(True, "r1")]


def test_simulated_driver_declines():
    responder = FakeResponder()
    driver = SimulatedDriver(DriverBehavior("decline", delay_s=0.01))
    driver.attach(responder)
    driver.on_prompt(Req())
    assert responder.event.wait(2.0)
    assert responder.calls == [(False, "r1")]


def test_simulated_driver_none_never_answers():
    responder = FakeResponder()
    driver = SimulatedDriver(DriverBehavior("none"))
    driver.attach(responder)
    driver.on_prompt(Req())
    assert not responder.event.wait(0.2)


def test_simulated_driver_close_cancels_pending_answer():
    responder = FakeResponder()
    driver = SimulatedDriver(DriverBehavior("confirm", delay_s=0.3))
    driver.attach(responder)
    driver.on_prompt(Req())
    driver.close()
    assert not responder.event.wait(0.6)


# ---------------------------------------------------------------- کنسول

@pytest.mark.parametrize("text,expected", [
    ("y", True), ("YES", True), (" بله ", True), ("آره", True),
    ("n", False), ("No", False), ("خیر", False),
    ("maybe", None), ("", None),
])
def test_parse_answer(text, expected):
    assert parse_answer(text) is expected


def test_console_listener_forwards_answers_and_ignores_noise():
    responder = FakeResponder()
    listener = ConsoleAnswerListener(responder, stream=io.StringIO("what\ny\nn\n"))
    listener.start()
    listener.join(2.0)
    assert [c[0] for c in responder.calls] == [True, False]


def test_console_prompt_mentions_timeout_and_outcome():
    out = io.StringIO()
    event = DetectionEvent(
        detector="crash", event_type="crash_suspected", severity=Severity.CRITICAL,
        message="m", vehicle_id="car_a", timestamp=T0, details={"confidence": "high"},
    )
    request = type("R", (), {"event": event, "id": "x"})()
    make_console_prompt(60.0, "send", out=out)(request)
    text = out.getvalue()
    assert "60" in text and "خودکار" in text and "high" in text
    out2 = io.StringIO()
    make_console_prompt(30.0, "skip", out=out2)(request)
    assert "ارسال نمی‌شود" in out2.getvalue()


# ---------------------------------------------------------------- سرتاسری با دنیای شبیه‌سازی

def run_world(scenario, seconds, policy=None, step_s=0.1):
    """مثل main: هر ثانیه OBD به monitor می‌رسد؛ حسگر گوشی از world می‌آید."""
    w, clock = world(scenario)
    log = {"emergency": [], "sent": [], "prompts": []}
    responder = CrashResponder(
        emergency_handler=log["emergency"].append,
        send_alert=log["sent"].append,
        prompt=log["prompts"].append,
        clock=clock,
        **(policy or {}),
    )
    monitor = CrashMonitor(
        w.motion_source(), CrashDetector("car_a"), responder, clock=clock
    )
    client = SimulatedOBDClient(w)
    detected, sent_at = [], []
    steps_per_obd = round(1.0 / step_s)
    for i in range(int(seconds / step_s)):
        clock.advance(step_s)
        if i % steps_per_obd == 0:
            data = client.read_vehicle_data()
            monitor.update_vehicle(clock(), data["speed"], data["rpm"])
        before = len(log["sent"])
        detected += monitor.step()
        if len(log["sent"]) > before:
            sent_at.append(w.elapsed_s())
    return log, detected, sent_at, responder


def test_world_crash_is_detected_with_obd_and_phone_together():
    log, detected, _, _ = run_world("crash", 40, {"on_timeout": "skip"})
    assert len(detected) == 1
    d = detected[0].details
    assert d["confidence"] == "high" and d["speed_drop"] is True and d["engine_off"] is True
    assert log["emergency"] == detected and len(log["prompts"]) == 1
    assert log["sent"] == []


def test_world_crash_default_policy_sends_after_sixty_seconds():
    log, detected, sent_at, _ = run_world("crash", 100)
    assert len(detected) == 1
    assert len(sent_at) == 1
    # شناسایی در حدود ثانیه‌ی ۲۳؛ ارسال ۶۰ ثانیه بعد
    assert 80 <= sent_at[0] <= 90


def test_world_crash_driver_confirmation_sends_immediately():
    log, _, _, responder = run_world("crash", 40, {"on_timeout": "skip"})
    assert responder.answer(True)
    assert [e.event_type for e in log["sent"]] == ["crash_suspected"]


def test_world_crash_phone_only_is_low_confidence():
    _, detected, _, _ = run_world("crash_phone_only", 40, {"on_timeout": "skip"})
    assert len(detected) == 1
    assert detected[0].details["confidence"] == "low"
    assert detected[0].details["speed_drop"] is None


@pytest.mark.parametrize("name", [
    "normal", "overheat", "engine_stall", "trouble_code", "low_voltage",
    "hard_brake", "pothole", "phone_drop",
])
def test_world_non_crash_scenarios_never_trigger_crash_response(name):
    log, detected, _, _ = run_world(name, 60)
    assert detected == [] and log["prompts"] == []
