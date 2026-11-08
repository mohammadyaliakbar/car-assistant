from datetime import datetime, timezone

from app.detectors.event_detector import DetectionEvent, Severity
from app.emergency import (
    EmergencyContacts,
    RiskAssessment,
    RiskLevel,
    format_message,
    load_contacts,
    load_instructions,
)
from app.location import GpsFix, RoadContext

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
contacts = EmergencyContacts()


def ev():
    return DetectionEvent(
        detector="stoppage", event_type="engine_stalled_while_moving",
        severity=Severity.WARNING, message="t", vehicle_id="car_a",
        timestamp=NOW, details={"previous_speed_kmh": 100.0},
    )


def fix():
    return GpsFix(
        latitude=35.689200, longitude=51.389000, accuracy_m=8.0,
        speed_kmh=0.0, heading_deg=90.0, timestamp=NOW, source="test",
    )


def risk(level=RiskLevel.HIGH):
    return RiskAssessment(level=level, reasons=["دلیل آزمایشی"], confidence="high")


def build(f=None, r=None, level=RiskLevel.HIGH, country=None):
    instruction = load_instructions()[0]
    return contacts.build_report(
        ev(), f, r, risk(level), instruction, "Saipa Pride", country
    )


# --- فایل داده ---
def test_contacts_file_loads_and_has_default():
    data = load_contacts()
    assert "IR" in data and "DEFAULT" in data
    assert all(c.number.strip() for e in data.values() for c in e.contacts)


def test_placeholder_numbers_are_not_marked_verified():
    data = load_contacts()
    assert all(not c.verified for e in data.values() for c in e.contacts)


# --- انتخاب کشور ---
def test_country_lookup_is_case_insensitive():
    assert contacts.get_contacts("ir") == contacts.get_contacts("IR")


def test_unknown_country_falls_back_not_empty():
    assert len(contacts.get_contacts("ZZ")) > 0


def test_none_country_uses_default_country():
    assert contacts.get_contacts(None) == contacts.get_contacts("IR")


def test_empty_data_returns_no_contacts_without_crash():
    assert EmergencyContacts({}).get_contacts("IR") == []


# --- چه زمانی گزارش آماده شود ---
def test_report_prepared_for_dangerous_and_unknown_levels():
    assert contacts.should_prepare(risk(RiskLevel.HIGH))
    assert contacts.should_prepare(risk(RiskLevel.CRITICAL))
    assert contacts.should_prepare(risk(RiskLevel.UNKNOWN))


def test_report_not_prepared_for_low_and_medium():
    assert not contacts.should_prepare(risk(RiskLevel.LOW))
    assert not contacts.should_prepare(risk(RiskLevel.MEDIUM))


# --- محتوای گزارش ---
def test_report_has_all_required_fields():
    r = build(fix(), RoadContext(highway_type="motorway", data_quality="ok"))
    assert r.latitude == 35.6892 and r.longitude == 51.389
    assert r.road_type == "motorway"
    assert r.heading_deg == 90.0
    assert r.event_type == "engine_stalled_while_moving"
    assert r.vehicle == "Saipa Pride"
    assert r.stopped_at == NOW
    assert r.risk_level == RiskLevel.HIGH
    assert r.instruction_id is not None
    assert r.gps_timestamp == NOW
    assert len(r.contacts) > 0


def test_report_without_gps_keeps_unknown_as_none_not_zero():
    r = build(None, None, level=RiskLevel.UNKNOWN)
    assert r.latitude is None and r.longitude is None
    assert r.gps_timestamp is None and r.road_type is None


# --- متن پیام ---
def test_message_contains_coordinates_and_risk():
    text = format_message(build(fix(), RoadContext(highway_type="motorway", data_quality="ok")))
    assert "35.689200" in text and "51.389000" in text
    assert "motorway" in text
    assert "زیاد" in text


def test_message_with_unknown_position_says_unknown():
    text = format_message(build(None, None, level=RiskLevel.UNKNOWN))
    assert "مختصات: نامشخص" in text
    assert "None" not in text


def test_message_warns_when_numbers_are_unverified():
    assert "تأییدنشده" in format_message(build(fix(), None))