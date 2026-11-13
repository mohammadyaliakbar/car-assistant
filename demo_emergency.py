import argparse
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

from app.detectors import DetectionEvent, Severity

from app.emergency import (
    EmergencyContacts,
    RecommendationEngine,
    RiskAnalyzer,
    RiskLevel,
    RoadsideEmergencyService,
)

from app.location import RoadAnalyzer, SimulatedGpsSource
from app.location.offline_provider import OfflineRoadProvider

from app.vehicle_profiles import get_vehicle_profile
from app.notifications.telegram_notifier import TelegramNotifier


# مسیر دیتابیس آفلاین
BASE_DIR = Path(__file__).resolve().parent
ROAD_DB_PATH = BASE_DIR / "data" / "roads_tehran.sqlite"


def make_notifier():
    """ساخت ارسال‌کننده تلگرام در صورت وجود تنظیمات."""

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")

    if token and chat_id:
        return TelegramNotifier(
            token=token,
            chat_id=chat_id,
            cooldown_seconds=0,
        )

    print("(تلگرام غیرفعال: توکن یا chat id تنظیم نشده است)")
    return None


NOTIFIER = make_notifier()


def to_event(response, vehicle_id):
    """تبدیل پاسخ سیستم اضطراری به DetectionEvent."""

    ins = response.instruction

    lines = [
        f"🚨 {ins.title}",
        f"سطح خطر: {response.assessment.level.value}",
        ins.headline,
        "",
    ]

    lines += [
        f"{i}. {step}"
        for i, step in enumerate(ins.steps, 1)
    ]

    lines += [
        f"⚠️ {warning}"
        for warning in ins.warnings
    ]

    if ins.status == "draft":
        lines.append("(راهنمای پیش‌نویس و تأییدنشده)")

    if response.message:
        lines += ["", response.message]

    level = response.assessment.level

    severity = (
        Severity.CRITICAL
        if level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        else Severity.WARNING
    )

    return DetectionEvent(
        detector="roadside_emergency",
        event_type=f"roadside_emergency_{response.phase}",
        severity=severity,
        message="\n".join(lines),
        vehicle_id=vehicle_id,
        timestamp=response.event_timestamp,
        details={},
    )


def show(response):
    """نمایش نتیجه تحلیل اضطراری و ارسال آن به تلگرام."""

    print(
        f"\n=== فاز: {response.phase} "
        f"| خطر: {response.assessment.level.value} "
        f"| اطمینان: {response.assessment.confidence} ==="
    )

    print("دلایل:", *response.assessment.reasons, sep="\n  - ")

    print(
        f"راهنما: {response.instruction.id} "
        f"(وضعیت: {response.instruction.status})"
    )

    print("خلاصه:", response.instruction.headline)

    for index, step in enumerate(response.instruction.steps, 1):
        print(f"  {index}. {step}")

    for warning in response.instruction.warnings:
        print("  هشدار:", warning)

    if response.message:
        print("\n" + response.message)

    # ارسال پیام به تلگرام
    if NOTIFIER is not None:
        try:
            NOTIFIER.notify_event(
                to_event(response, "demo")
            )
            print("→ به تلگرام ارسال شد.")

        except Exception as exc:
            print("→ ارسال تلگرام ناموفق:", exc)


def create_road_analyzer(no_map: bool):
    """ساخت RoadAnalyzer متناسب با حالت اجرای برنامه."""

    if no_map:

        def no_map_fetch(query, timeout):
            raise ConnectionError("Map access is disabled.")

        print("نقشه غیرفعال است.")

        return RoadAnalyzer(fetch=no_map_fetch)

    if not ROAD_DB_PATH.exists():
        print(
            f"هشدار: دیتابیس آفلاین پیدا نشد: {ROAD_DB_PATH}"
        )
        print("RoadAnalyzer بدون منبع آفلاین ساخته می‌شود.")

        return RoadAnalyzer()

    print(f"دیتابیس آفلاین: {ROAD_DB_PATH}")

    offline_provider = OfflineRoadProvider(
        str(ROAD_DB_PATH)
    )

    return RoadAnalyzer(
        offline=offline_provider
    )


def main():
    parser = argparse.ArgumentParser(
        description="دموی سیستم مدیریت شرایط اضطراری خودرو"
    )

    parser.add_argument(
        "--lat",
        type=float,
        default=35.6892,
        help="عرض جغرافیایی",
    )

    parser.add_argument(
        "--lon",
        type=float,
        default=51.3890,
        help="طول جغرافیایی",
    )

    parser.add_argument(
        "--gps-scenario",
        default="good",
        choices=SimulatedGpsSource.SCENARIOS,
        help="سناریوی شبیه‌سازی GPS",
    )

    parser.add_argument(
        "--prev-speed",
        type=float,
        default=100.0,
        help="سرعت قبلی خودرو بر حسب کیلومتر بر ساعت",
    )

    parser.add_argument(
        "--no-map",
        action="store_true",
        help="غیرفعال کردن دسترسی به نقشه",
    )

    args = parser.parse_args()

    # اطلاعات خودرو
    profile = get_vehicle_profile(settings.VEHICLE_ID)

    # ساخت تحلیلگر نقشه
    road_analyzer = create_road_analyzer(args.no_map)

    # ساخت سرویس اضطراری
    service = RoadsideEmergencyService(
        gps=SimulatedGpsSource(
            args.gps_scenario,
            args.lat,
            args.lon,
        ),

        road_analyzer=road_analyzer,

        risk_analyzer=RiskAnalyzer(),

        recommender=RecommendationEngine(),

        contacts=EmergencyContacts(),

        vehicle=f"{profile.manufacturer} {profile.model}",

        publish=show,

        # اجرای همگام برای مشاهده هر دو فاز
        submit=lambda task: task(),
    )

    # شبیه‌سازی رویداد خاموش شدن موتور هنگام حرکت
    event = DetectionEvent(
        detector="stoppage",
        event_type="engine_stalled_while_moving",
        severity=Severity.WARNING,
        message="demo",
        vehicle_id=profile.vehicle_id,
        timestamp=datetime.now(timezone.utc),
        details={
            "previous_speed_kmh": args.prev_speed,
            "rpm": 0.0,
        },
    )

    print("\nشروع شبیه‌سازی وضعیت اضطراری خودرو...")

    try:
        service.handle(event)

    finally:
        if NOTIFIER is not None:
            print("\nدر انتظار ارسال پیام‌های تلگرام...")

            flushed = NOTIFIER.flush(timeout=20)

            print(f"وضعیت صف تلگرام: {'تکمیل شد' if flushed else 'Timeout'}")

            NOTIFIER.close(timeout=5)

if __name__ == "__main__":
    main()