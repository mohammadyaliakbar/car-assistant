from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.location import GpsFix, RoadContext, SimulatedGpsSource


def test_gps_fix_rejects_invalid_latitude():
    with pytest.raises(ValidationError):
        GpsFix(latitude=91, longitude=0,
               timestamp=datetime.now(timezone.utc), source="test")


def test_gps_fix_requires_timezone():
    with pytest.raises(ValidationError):
        GpsFix(latitude=35, longitude=51,
               timestamp=datetime.now(), source="test")


def test_unknown_values_stay_none():
    fix = GpsFix(latitude=35, longitude=51,
                 timestamp=datetime.now(timezone.utc), source="test")
    assert fix.accuracy_m is None
    assert fix.speed_kmh is None


def test_road_context_defaults_are_unknown_not_false():
    road = RoadContext()
    assert road.has_emergency_bay_nearby is None
    assert road.no_stopping_tagged is None
    assert road.data_quality == "unavailable"


def test_simulated_scenarios():
    assert SimulatedGpsSource("no_fix").get_fix() is None
    assert SimulatedGpsSource("good").get_fix().accuracy_m == 5.0
    assert SimulatedGpsSource("poor_accuracy").get_fix().accuracy_m == 150.0
    assert SimulatedGpsSource("stale").get_fix().age_s() > 100


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        SimulatedGpsSource("nope")