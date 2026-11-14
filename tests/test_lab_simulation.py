"""تست‌های موتور آزمایشگاه شبیه‌سازی (مرحله ۱)."""

import time
from datetime import datetime, timezone

import pytest

from app.detectors import CoolantOverheatDetector, DetectorEngine, StoppageDetector, VoltageDetector
from app.simulation.lab_controller import SimulationController
from app.simulation.lab_engine import SimulationEngine, build_detector_engine
from app.simulation.routes import ROUTES, Route
from app.simulation.scenarios import LAB_SCENARIOS

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run(name, **kw):
    engine = SimulationEngine(name, start_time=T0, **kw)
    engine.run_to_end()
    return engine


# ------------------------------------------------------------ مسیر

def test_route_positions_and_road_types():
    route = ROUTES["mixed_urban_highway_rural"]
    assert route.position_at(0).road_type == "urban"
    assert route.position_at(route.length_m * 0.5).road_type == "highway"
    assert route.position_at(route.length_m).road_type == "rural"
    assert route.position_at(route.length_m + 1000).arrived
    assert route.position_at(-5).distance_m == 0


def test_custom_route_and_validation():
    r = Route.from_points((35.70, 51.40), (35.71, 51.41), "urban")
    assert r.length_m > 1000
    with pytest.raises(ValueError):
        Route("x", "x", [(100.0, 0.0), (0.0, 0.0)])          # عرض جغرافیایی نامعتبر
    with pytest.raises(ValueError):
        Route("x", "x", [(35.0, 51.0), (35.0, 51.0)])        # قطعه‌ی با طول صفر
    with pytest.raises(ValueError):
        Route("x", "x", [(35.0, 51.0), (35.1, 51.1)], "space")


# ------------------------------------------------------------ معماری

def test_thresholds_are_the_project_defaults():
    names = {type(d): d for d in build_detector_engine().detectors}
    cool, volt, stop = (names[CoolantOverheatDetector], names[VoltageDetector], names[StoppageDetector])
    assert (cool.warning_c, cool.critical_c, cool.hysteresis_c, cool.required_samples) == (105, 115, 5, 3)
    assert (volt.charging_min_v, volt.charging_max_v, volt.battery_min_v) == (13.0, 15.2, 11.8)
    assert (stop.engine_on_rpm, stop.moving_speed_kmh) == (500, 10)


def test_real_detector_engine_processes_every_record(monkeypatch):
    calls = []
    original = DetectorEngine.process

    def spy(self, record):
        calls.append(record)
        return original(self, record)

    monkeypatch.setattr(DetectorEngine, "process", spy)
    engine = run("ecu_trouble_code")
    assert len(calls) == len(engine.frames) == 120
    assert {r.source for r in calls} == {"SIMULATOR"}


def test_frames_are_labeled_simulated_and_use_obd_keys():
    engine = run("normal_driving")
    f = engine.frames[10]
    assert f["simulated"] is True and f["source"] == "SIMULATOR"
    assert set(f["obd"]) == {
        "rpm", "speed", "coolant_temperature_c", "engine_load_percent",
        "throttle_position_percent", "intake_temperature_c", "intake_pressure_kpa",
        "trouble_codes", "diagnostic_status", "control_module_voltage_v",
    }


# ------------------------------------------------------------ سناریوها

def test_every_scenario_has_expected_verdict():
    expected = {
        "normal_driving": "no_events_as_expected",
        "highway_stoppage": "detected_by_detector",
        "coolant_overheat": "detected_by_detector",
        "battery_drop": "detected_by_detector",
        "ecu_trouble_code": "detected_by_detector",
        "gps_signal_loss": "status_only_no_detector",
        "multiple_failures": "detected_by_detector",
        "road_changes": "no_events_as_expected",
    }
    assert set(expected) == set(LAB_SCENARIOS)
    for name, verdict in expected.items():
        s = run(name).summary()
        assert s["verdict"] == verdict, name
        assert s["false_alarms"] == 0, name
        assert s["validation_errors"] == 0, name


def test_highway_stoppage_full_chain():
    e = run("highway_stoppage")
    ev = [x for x in e.events if x["event_type"] == "engine_stalled_while_moving"]
    assert len(ev) == 1 and ev[0]["detector"] == "stoppage"
    stop_t = ev[0]["sim_t"]
    assert stop_t > e.event_time_s and e.frames[int(stop_t) - 1]["obd"]["speed"] >= 10

    last = e.frames[-1]
    assert last["obd"]["speed"] == 0 and last["obd"]["rpm"] == 0
    assert last["engine_on"] is False and last["danger_level"] == "warning"

    # GPS بعد از ایستادن ثابت می‌ماند
    stopped_at = next(f["t"] for f in e.frames if f["obd"]["speed"] == 0)
    tail = [f["gps"] for f in e.frames if f["t"] >= stopped_at]
    assert len({(g["latitude"], g["longitude"]) for g in tail}) == 1

    # پاسخ اضطراری: فاز فوری و نهایی، با خطر بزرگراه
    phases = [r["phase"] for r in e.emergency_log]
    assert phases == ["initial", "final"]
    assert e.emergency_log[1]["assessment"]["level"] in ("high", "critical")
    assert e.emergency_log[0]["generation_ms"] >= 0


def test_coolant_overheat_escalates_warning_then_critical():
    e = run("coolant_overheat")
    sev = [x["severity"] for x in e.events if x["event_type"] == "engine_overheat"]
    assert sev == ["warning", "critical"]
    assert e.danger_level() == "critical"
    assert [a["severity"] for a in e.active_alerts()] == ["critical"]   # یک هشدار، نه دو


def test_battery_drop_detected_below_charging_threshold():
    e = run("battery_drop")
    ev = e.events[0]
    assert ev["event_type"] == "low_charging_voltage"
    assert ev["details"]["voltage_v"] < 13.0


def test_trouble_code_event_and_description():
    e = run("ecu_trouble_code")
    assert e.events[0]["details"]["code"] == "P0301"
    assert "misfire" in e.frames[-1]["dtc_descriptions"]["P0301"].lower()


def test_gps_loss_never_shows_stale_position_as_live():
    e = run("gps_signal_loss")
    t0 = int(e.event_time_s)
    before, lost, after = e.frames[t0 - 1]["gps"], e.frames[t0 + 20]["gps"], e.frames[t0 + 90]["gps"]
    assert before["status"] == "connected" and before["live"] is True
    assert lost["status"] == "disconnected" and lost["live"] is False
    assert lost["speed_kmh"] is None and lost["age_s"] > 15
    # آخرین موقعیت معتبر حفظ می‌شود و جابه‌جا نمی‌شود
    assert (lost["latitude"], lost["longitude"]) == (before["latitude"], before["longitude"])
    assert after["status"] == "connected" and after["live"] is True
    assert e.events == []                                  # Detectorی وجود ندارد
    assert any(f["status_alerts"] for f in e.frames)
    assert e.frames[t0 + 1]["alerts"][0]["source"] == "simulator_status"
    assert e.alerts == {}                                  # بعد از برقراری، هشدار پاک شده


def test_multiple_failures_alerts_are_merged_without_contradiction():
    e = run("multiple_failures")
    s = e.summary()
    assert s["verified_by_detector"] and len(s["expected"]) == 3
    alerts = e.active_alerts()
    keys = [a["key"] for a in alerts]
    assert len(keys) == len(set(keys)) == 3
    assert alerts[0]["severity"] == "critical" and alerts[0]["detector"] == "coolant_overheat"
    assert e.danger_level() == "critical"


def test_road_changes_cover_all_road_types_with_matching_speed():
    e = run("road_changes")
    speeds = {}
    for f in e.frames:
        speeds.setdefault(f["road_type"], []).append(f["obd"]["speed"])
    assert set(speeds) == {"urban", "highway", "rural"}
    mean = {k: sum(v) / len(v) for k, v in speeds.items()}
    assert mean["urban"] < mean["rural"] < mean["highway"]


# ------------------------------------------------------------ تکرارپذیری و عدم تداخل

def test_scenarios_are_repeatable():
    a, b = run("multiple_failures"), run("multiple_failures")
    assert [f["obd"] for f in a.frames] == [f["obd"] for f in b.frames]
    assert a.events == b.events


def test_runs_do_not_share_state():
    first = run("coolant_overheat")
    second = run("normal_driving")
    assert first.events and second.events == []
    assert second.alerts == {} and second.danger_level() == "normal"


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        SimulationEngine("no_such_scenario")
    with pytest.raises(ValueError):
        SimulationEngine("normal_driving", vehicle_id="unknown_car")
    with pytest.raises(ValueError):
        SimulationEngine("normal_driving", event_time_s=-1)


# ------------------------------------------------------------ کنترلر

def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_controller_start_pause_resume_finish():
    c = SimulationController()
    c.set_speed(50)
    c.start("ecu_trouble_code", start_time=T0)
    assert wait_for(lambda: c.snapshot()["frame_count"] >= 5)
    c.pause()
    assert c.state == "paused"
    n = c.snapshot()["frame_count"]
    time.sleep(0.2)
    assert c.snapshot()["frame_count"] == n          # در حالت Pause هیچ گامی برداشته نمی‌شود
    c.resume()
    assert c.wait(20)
    snap = c.snapshot()
    assert snap["state"] == "finished" and snap["frame_count"] == 120
    assert snap["summary"]["verdict"] == "detected_by_detector"


def test_controller_stop_keeps_data_reset_clears_restart_is_fresh():
    c = SimulationController()
    c.set_speed(50)
    c.start("coolant_overheat", start_time=T0)
    assert wait_for(lambda: c.snapshot()["frame_count"] >= 5)
    t = time.time()
    c.stop()
    assert time.time() - t < 1.0 and c.state == "stopped"
    kept = c.snapshot()["frame_count"]
    assert kept >= 5
    time.sleep(0.1)
    assert c.snapshot()["frame_count"] == kept

    c.restart()
    assert wait_for(lambda: c.snapshot()["frame_count"] >= 1)
    assert c.snapshot()["frames"][0]["t"] == 0       # اجرای تازه از صفر

    c.reset()
    snap = c.snapshot()
    assert snap["state"] == "idle" and snap["frame_count"] == 0 and snap["scenario"] is None
    with pytest.raises(RuntimeError):
        c.restart()


def test_controller_speed_multiplier_and_validation():
    c = SimulationController()
    for bad in (0, -1, 51):
        with pytest.raises(ValueError):
            c.set_speed(bad)
    c.set_speed(10)
    c.start("normal_driving", start_time=T0)
    time.sleep(1.0)
    fast = c.snapshot()["frame_count"]
    c.reset()
    c.set_speed(1)
    c.start("normal_driving", start_time=T0)
    time.sleep(1.0)
    slow = c.snapshot()["frame_count"]
    c.reset()
    assert fast >= 5 * slow


def test_controller_survives_failing_listener_and_bad_scenario():
    c = SimulationController()
    c.set_speed(50)
    c.subscribe(lambda frame: 1 / 0)
    c.start("ecu_trouble_code", start_time=T0)
    assert c.wait(20) and c.snapshot()["frame_count"] == 120
    with pytest.raises(ValueError):
        c.start("nope")
