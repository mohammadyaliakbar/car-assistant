import argparse
from datetime import datetime, timezone

from app.config import settings
from app.detectors import DetectionEvent, Severity
from app.emergency import (
    EmergencyContacts,
    RecommendationEngine,
    RiskAnalyzer,
    RoadsideEmergencyService,
)
from app.location import RoadAnalyzer, SimulatedGpsSource
from app.vehicle_profiles import get_vehicle_profile

from app.notifications.telegram_notifier import TelegramNotifier
from app.detectors import DetectionEvent, Severity
from app.emergency import RiskLevel


def make_notifier():
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        return TelegramNotifier(token=token, chat_id=chat_id, cooldown_seconds=0)
    print("(تلگرام غیرفعال: توکن یا chat id نیست)")
    return None


NOTIFIER = make_notifier()


def to_event(response, vehicle_id):
    ins = response.instruction
    lines = [f"🚨 {ins.title}", f"سطح خطر: {response.assessment.level.value}",
             ins.headline, ""]
    lines += [f"{i}. {s}" for i, s in enumerate(ins.steps, 1)]
    lines += [f"⚠️ {w}" for w in ins.warnings]
    if ins.status == "draft":
        lines.append("(راهنمای پیش‌نویس و تأییدنشده)")
    if response.message:
        lines += ["", response.message]
    level = response.assessment.level
    sev = Severity.CRITICAL if level in (RiskLevel.HIGH, RiskLevel.CRITICAL) else Severity.WARNING
    return DetectionEvent(
        detector="roadside_emergency",
        event_type=f"roadside_emergency_{response.phase}",
        severity=sev, message="\n".join(lines), vehicle_id=vehicle_id,
        timestamp=response.event_timestamp, details={},
    )


def show(response):
    print(f"\n=== فاز: {response.phase} | خطر: {response.assessment.level.value} "
          f"| اطمینان: {response.assessment.confidence} ===")
    print("دلایل:", *response.assessment.reasons, sep="\n  - ")
    print(f"راهنما: {response.instruction.id} (وضعیت: {response.instruction.status})")
    print("خلاصه:", response.instruction.headline)
    for i, step in enumerate(response.instruction.steps, 1):
        print(f"  {i}. {step}")
    for w in response.instruction.warnings:
        print("  هشدار:", w)
    if response.message:
        print("\n" + response.message)
    if NOTIFIER is not None:
        try:
            NOTIFIER.notify_event(to_event(response, "demo"))
            print("→ به تلگرام ارسال شد.")
        except Exception as exc:
            print("→ ارسال تلگرام ناموفق:", exc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--lat", type=float, default=35.6892)
    p.add_argument("--lon", type=float, default=51.3890)
    p.add_argument("--gps-scenario", default="good", choices=SimulatedGpsSource.SCENARIOS)
    p.add_argument("--prev-speed", type=float, default=100.0)
    p.add_argument("--no-map", action="store_true")
    args = p.parse_args()

    profile = get_vehicle_profile(settings.VEHICLE_ID)

    def no_map(q, t):
        raise ConnectionError("map disabled")

    service = RoadsideEmergencyService(
        gps=SimulatedGpsSource(args.gps_scenario, args.lat, args.lon),
        road_analyzer=RoadAnalyzer(fetch=no_map) if args.no_map else RoadAnalyzer(),
        risk_analyzer=RiskAnalyzer(),
        recommender=RecommendationEngine(),
        contacts=EmergencyContacts(),
        vehicle=f"{profile.manufacturer} {profile.model}",
        publish=show,
        submit=lambda task: task(),     # همگام، برای دیدن هر دو فاز پشت‌سرهم
    )

    service.handle(DetectionEvent(
        detector="stoppage",
        event_type="engine_stalled_while_moving",
        severity=Severity.WARNING,
        message="demo",
        vehicle_id=profile.vehicle_id,
        timestamp=datetime.now(timezone.utc),
        details={"previous_speed_kmh": args.prev_speed, "rpm": 0.0},
    ))


if __name__ == "__main__":
    main()