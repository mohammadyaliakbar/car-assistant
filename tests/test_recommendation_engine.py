from datetime import datetime, timezone

from app.detectors.event_detector import DetectionEvent, Severity
from app.emergency import (
    Instruction,
    RecommendationEngine,
    RiskAssessment,
    RiskLevel,
    TriggerConditions,
    load_instructions,
)
from app.location import RoadContext

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
engine = RecommendationEngine()


def ev(event_type="engine_stalled_while_moving"):
    return DetectionEvent(
        detector="stoppage", event_type=event_type,
        severity=Severity.WARNING, message="t",
        vehicle_id="car_a", timestamp=NOW,
    )


def risk(level):
    return RiskAssessment(level=level, reasons=[], confidence="medium")


def road(**kw):
    kw.setdefault("data_quality", "ok")
    return RoadContext(**kw)


def pick(level, r, event_type="engine_stalled_while_moving"):
    return engine.select(ev(event_type), risk(level), r).id


# --- انتخاب بر اساس سناریو ---
def test_motorway_high_selects_highway():
    r = road(highway_type="motorway")
    assert pick(RiskLevel.HIGH, r) == "highway_engine_stall"


def test_motorway_reduced_to_medium_still_highway():
    r = road(highway_type="motorway")
    assert pick(RiskLevel.MEDIUM, r) == "highway_engine_stall"


def test_trunk_selects_highway():
    assert pick(RiskLevel.HIGH, road(highway_type="trunk")) == "highway_engine_stall"


def test_primary_selects_main_road():
    assert pick(RiskLevel.MEDIUM, road(highway_type="primary")) == "main_road_engine_stall"


def test_residential_selects_urban():
    r = road(highway_type="residential")
    assert pick(RiskLevel.LOW, r) == "urban_street_engine_stall"


def test_parking_wins_over_street():
    r = road(highway_type="service", is_parking_area=True)
    assert pick(RiskLevel.LOW, r) == "parking_engine_stall"


def test_winding_secondary_selects_winding():
    r = road(highway_type="secondary", is_winding=True)
    assert pick(RiskLevel.HIGH, r) == "winding_road_engine_stall"


def test_winding_motorway_still_selects_highway():
    r = road(highway_type="motorway", is_winding=True)
    assert pick(RiskLevel.CRITICAL, r) == "highway_engine_stall"


def test_unmatched_road_type_gets_generic():
    r = road(highway_type="track")
    assert pick(RiskLevel.MEDIUM, r) == "generic_engine_stall"


# --- UNKNOWN ---
def test_unknown_gets_unknown_instruction():
    assert pick(RiskLevel.UNKNOWN, None) == "unknown_location_engine_stall"


def test_unknown_ignores_road_info():
    r = road(highway_type="motorway")
    assert pick(RiskLevel.UNKNOWN, r) == "unknown_location_engine_stall"


def test_known_risk_never_gets_unknown_instruction():
    assert pick(RiskLevel.MEDIUM, None) == "generic_engine_stall"


def test_none_road_does_not_match_road_conditions():
    assert pick(RiskLevel.HIGH, None) == "generic_engine_stall"


# --- ایمنی و پایداری ---
def test_unknown_event_type_returns_last_resort():
    assert pick(RiskLevel.HIGH, road(highway_type="motorway"), "other") == "last_resort"


def test_empty_instruction_list_returns_last_resort():
    result = RecommendationEngine([]).select(
        ev(), risk(RiskLevel.HIGH), road(highway_type="motorway")
    )
    assert result.id == "last_resort"


def test_priority_breaks_ties():
    def make(i, p):
        return Instruction(
            id=i, title=i, headline=i,
            trigger_conditions=TriggerConditions(event_type="x"),
            risk_level=RiskLevel.MEDIUM, priority=p, steps=["s"],
        )
    e = RecommendationEngine([make("low", 1), make("high", 9)])
    assert e.select(ev("x"), risk(RiskLevel.HIGH), None).id == "high"


# --- اعتبار خود فایل داده ---
def test_json_loads_with_unique_ids():
    items = load_instructions()
    assert len(items) == len({i.id for i in items})


def test_all_instructions_are_draft_until_reviewed():
    assert all(i.status == "draft" for i in load_instructions())


def test_headlines_are_short_for_driver():
    for i in load_instructions():
        assert len(i.headline) <= 70, i.id


def test_every_instruction_is_short_enough_to_follow():
    for i in load_instructions():
        assert 3 <= len(i.steps) <= 8, i.id


def test_unknown_level_instruction_exists():
    assert any(i.risk_level == RiskLevel.UNKNOWN for i in load_instructions())