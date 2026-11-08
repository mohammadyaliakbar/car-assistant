import json
from pathlib import Path

from pydantic import BaseModel

from app.detectors.event_detector import DetectionEvent
from app.emergency.models import (
    EmergencyContact,
    EmergencyReport,
    Instruction,
    RiskAssessment,
    RiskLevel,
)
from app.location.models import GpsFix, RoadContext

DEFAULT_CONTACTS_FILE = Path(__file__).parent / "data" / "contacts.json"
FALLBACK_KEY = "DEFAULT"

# سطح‌هایی که آماده‌سازی گزارش امدادی دارند. UNKNOWN عمداً شامل است:
# نامعلوم بودن موقعیت یعنی امن بودنش ثابت نشده است.
REPORT_LEVELS = {RiskLevel.HIGH, RiskLevel.CRITICAL, RiskLevel.UNKNOWN}

EVENT_LABELS = {
    "engine_stalled_while_moving": "خاموش شدن موتور هنگام حرکت",
}
RISK_LABELS = {
    RiskLevel.LOW: "کم",
    RiskLevel.MEDIUM: "متوسط",
    RiskLevel.HIGH: "زیاد",
    RiskLevel.CRITICAL: "بحرانی",
    RiskLevel.UNKNOWN: "نامشخص",
}


class CountryContacts(BaseModel):
    country_name: str
    contacts: list[EmergencyContact]


def load_contacts(path: Path | None = None) -> dict[str, CountryContacts]:
    """فایل JSON را می‌خواند و اعتبارسنجی می‌کند. در صورت خطا استثنا می‌دهد."""
    path = path or DEFAULT_CONTACTS_FILE
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    countries: dict[str, CountryContacts] = {}
    for code, value in raw["countries"].items():
        entry = CountryContacts.model_validate(value)
        for contact in entry.contacts:
            if not contact.number.strip():
                raise ValueError(f"Empty phone number for {code}: {contact.name}")
        countries[code.upper()] = entry
    return countries


class EmergencyContacts:
    """آماده‌سازی گزارش امدادی. تماس خودکار ندارد."""

    def __init__(
        self,
        countries: dict[str, CountryContacts] | None = None,
        default_country: str = "IR",
    ):
        self.countries = countries if countries is not None else load_contacts()
        self.default_country = default_country.upper()

    def resolve_country(self, country_code: str | None) -> str | None:
        """کد کشور مؤثر را برمی‌گرداند؛ None یعنی نه کشور و نه DEFAULT موجود است."""
        for code in (country_code, self.default_country):
            if code and code.upper() in self.countries:
                return code.upper()
        return FALLBACK_KEY if FALLBACK_KEY in self.countries else None

    def get_contacts(self, country_code: str | None) -> list[EmergencyContact]:
        code = self.resolve_country(country_code)
        return list(self.countries[code].contacts) if code else []

    @staticmethod
    def should_prepare(assessment: RiskAssessment) -> bool:
        return assessment.level in REPORT_LEVELS

    def build_report(
        self,
        event: DetectionEvent,
        fix: GpsFix | None,
        road: RoadContext | None,
        assessment: RiskAssessment,
        instruction: Instruction | None,
        vehicle: str,
        country_code: str | None = None,
    ) -> EmergencyReport:
        return EmergencyReport(
            latitude=fix.latitude if fix else None,
            longitude=fix.longitude if fix else None,
            gps_accuracy_m=fix.accuracy_m if fix else None,
            road_type=road.highway_type if road else None,
            heading_deg=fix.heading_deg if fix else None,
            event_type=event.event_type,
            vehicle=vehicle,
            stopped_at=event.timestamp,
            risk_level=assessment.level,
            risk_reasons=list(assessment.reasons),
            instruction_id=instruction.id if instruction else None,
            contacts=self.get_contacts(country_code),
            country_code=self.resolve_country(country_code),
            gps_timestamp=fix.timestamp if fix else None,
        )


def format_message(report: EmergencyReport) -> str:
    """متن آماده‌ی ارسال به امدادگر. مقدار نامعلوم همیشه «نامشخص» نوشته می‌شود."""
    lines = ["درخواست امداد خودرو"]
    lines.append(
        "رویداد: " + EVENT_LABELS.get(report.event_type, report.event_type)
    )
    lines.append(f"خودرو: {report.vehicle}")
    lines.append(f"زمان توقف: {report.stopped_at.isoformat()}")

    if report.latitude is not None and report.longitude is not None:
        position = f"مختصات: {report.latitude:.6f}, {report.longitude:.6f}"
        if report.gps_accuracy_m is not None:
            position += f" (دقت حدود {report.gps_accuracy_m:.0f} متر)"
        else:
            position += " (دقت نامشخص)"
        lines.append(position)
        if report.gps_timestamp is not None:
            lines.append(f"زمان ثبت مختصات: {report.gps_timestamp.isoformat()}")
    else:
        lines.append("مختصات: نامشخص")

    lines.append(f"نوع جاده (از نقشه): {report.road_type or 'نامشخص'}")
    if report.heading_deg is not None:
        lines.append(f"جهت حرکت: {report.heading_deg:.0f} درجه")
    else:
        lines.append("جهت حرکت: نامشخص")
    lines.append(f"سطح خطر: {RISK_LABELS[report.risk_level]}")

    if report.contacts and not all(c.verified for c in report.contacts):
        lines.append("توجه: شماره‌های امدادی ثبت‌شده آزمایشی و تأییدنشده‌اند.")
    return "\n".join(lines)