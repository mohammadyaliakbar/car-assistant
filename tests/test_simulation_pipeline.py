"""
داده‌ی شبیه‌ساز از مسیر واقعی پروژه می‌گذرد: DataValidator → VehicleRecord → DetectorEngine.
(بدون thread و بدون زمان واقعی؛ ساعت ساختگی است.)
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.data_models import VehicleRecord
from app.data_validation import DataValidator
from app.detectors import (
    CoolantOverheatDetector,
    DetectorEngine,
    Severity,
    StoppageDetector,
    TroubleCodeDetector,
    VoltageDetector,
)
from app.simulation import SCENARIOS, SimulatedOBDClient, SimulatedWorld
from app.vehicle_profiles import get_vehicle_profile

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def run_pipeline(scenario, seconds, **kwargs):
    clock = FakeClock()
    world = SimulatedWorld(scenario, clock=clock, **kwargs)
    client = SimulatedOBDClient(world)
    validator = DataValidator(get_vehicle_profile("car_a"))
    engine = DetectorEngine([
        CoolantOverheatDetector(), TroubleCodeDetector(),
        VoltageDetector(), StoppageDetector(),
    ])
    events = []
    for second in range(seconds + 1):
        clock.now = world.start + timedelta(seconds=second)
        report = validator.validate(client.read_vehicle_data())
        assert report.errors == [] and report.data is not None
        assert report.warnings == []
        record = VehicleRecord(
            vehicle_id="car_a", source="SIMULATOR", timestamp=clock(), data=report.data
        )
        events += engine.process(record)
    return events


def types(events):
    return [e.event_type for e in events]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_scenario_passes_validation_cleanly(name):
    run_pipeline(name, 60)


def test_normal_scenario_produces_no_events():
    assert run_pipeline("normal", 60) == []


def test_crash_makes_the_engine_stall_event():
    events = run_pipeline("crash", 40)
    assert types(events) == ["engine_stalled_while_moving"]
    assert events[0].details["previous_speed_kmh"] >= 10


def test_crash_with_engine_running_has_no_stall_event():
    assert run_pipeline("crash_engine_running", 40) == []


def test_engine_stall_scenario():
    assert types(run_pipeline("engine_stall", 40)) == ["engine_stalled_while_moving"]


def test_overheat_goes_through_warning_then_critical():
    events = run_pipeline("overheat", 60)
    assert types(events) == ["engine_overheat", "engine_overheat"]
    assert [e.severity for e in events] == [Severity.WARNING, Severity.CRITICAL]


def test_trouble_code_scenario():
    events = run_pipeline("trouble_code", 40)
    assert types(events) == ["new_trouble_code"]
    assert events[0].details["code"] == "P0300"


def test_low_voltage_scenario():
    assert types(run_pipeline("low_voltage", 40)) == ["low_charging_voltage"]


def test_unavailable_obd_produces_no_events():
    assert run_pipeline("crash_phone_only", 40) == []
