import json
import logging
import time
from pathlib import Path

from app.config import settings
from app.data_models import VehicleRecord
from app.data_validation import DataValidator
from app.detectors import (
    CoolantOverheatDetector,
    DetectionEvent,
    DetectorEngine,
    Severity,
    StoppageDetector,
    TroubleCodeDetector,
    VoltageDetector,
)
from app.emergency import (
    EmergencyContacts,
    EmergencyResponse,
    RecommendationEngine,
    RiskAnalyzer,
    RiskLevel,
    RoadsideEmergencyService,
)
from app.location import RoadAnalyzer, SimulatedGpsSource
from app.location.offline_provider import OfflineRoadProvider
from app.logger import get_logger, setup_logging
from app.notifications.telegram_notifier import TelegramNotifier
from app.obd_client import OBDClient
from app.vehicle_profiles import get_vehicle_profile

setup_logging()
# لاگ httpx آدرس کامل درخواست (شامل توکن بات تلگرام) را چاپ می‌کند
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "vehicle_data.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"
EMERGENCY_FILE = DATA_DIR / "emergency.jsonl"
# نقشه‌ی آفلاین جاده (ساخته‌شده با tools/build_road_db.py)
ROADS_DB_FILE = DATA_DIR / "roads_tehran.sqlite"

POLL_INTERVAL_S = 1.0
MAX_CONSECUTIVE_ERRORS = 10
SOURCE_NAME = "OBD"

# تنظیمات موقت GPS شبیه‌ساز (بعداً با GPS واقعی گوشی جایگزین می‌شود)
COUNTRY_CODE = "IR"
SIM_GPS_SCENARIO = "good"
SIM_LAT = 35.6892
SIM_LON = 51.3890

# فقط رویدادهای این سطح‌ها به تلگرام می‌روند
TELEGRAM_SEVERITIES = {Severity.WARNING, Severity.CRITICAL}


def build_detector_engine() -> DetectorEngine:
    """Building the DetectorEngine and all vehicle detectors."""

    return DetectorEngine([
        CoolantOverheatDetector(),
        TroubleCodeDetector(),
        VoltageDetector(),
        StoppageDetector(),
    ])


def build_telegram_notifier():
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")

    if token and chat_id:
        logger.info("Telegram notifications enabled.")
        return TelegramNotifier(
            token=token,
            chat_id=chat_id,
            cooldown_seconds=300,
        )

    logger.warning(
        "Telegram notifications disabled: missing bot token or chat ID."
    )
    return None


def notify_telegram(notifier, event: DetectionEvent) -> None:
    """ارسال رویداد به تلگرام؛ هرگز استثنا بیرون نمی‌دهد."""
    if notifier is None or event.severity not in TELEGRAM_SEVERITIES:
        return
    try:
        notifier.notify_event(event)
    except Exception:
        logger.exception("Telegram notification failed.")


def emergency_to_event(
    response: EmergencyResponse,
    vehicle_id: str,
) -> DetectionEvent:
    """
    تبدیل پاسخ اضطراری به DetectionEvent تا با همان notify_event
    ارسال شود. نوع رویداد برای هر فاز جداست تا cooldown نوتیفایر،
    پیام فاز نهایی را بعد از پیام فاز فوری حذف نکند.
    """
    instruction = response.instruction
    lines = [
        f"🚨 {instruction.title}",
        f"سطح خطر: {response.assessment.level.value}",
        instruction.headline,
        "",
    ]
    lines += [f"{i}. {step}" for i, step in enumerate(instruction.steps, 1)]
    lines += [f"⚠️ {w}" for w in instruction.warnings]
    if instruction.status == "draft":
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
        details={
            "phase": response.phase,
            "risk_level": level.value,
            "instruction_id": instruction.id,
        },
    )


def build_road_analyzer() -> RoadAnalyzer:
    """اول نقشه‌ی آفلاین، و در صورت نبودن یا خارج‌بودن نقطه، Overpass."""
    if ROADS_DB_FILE.is_file():
        logger.info("Offline road map enabled: %s", ROADS_DB_FILE)
        return RoadAnalyzer(offline=OfflineRoadProvider(ROADS_DB_FILE))

    logger.warning(
        "Offline road map not found (%s); using Overpass only.", ROADS_DB_FILE
    )
    return RoadAnalyzer()


def build_emergency_service(profile, telegram_notifier) -> RoadsideEmergencyService:
    def publish(response: EmergencyResponse) -> None:
        instruction = response.instruction
        draft = " (پیش‌نویس تأییدنشده)" if instruction.status == "draft" else ""
        logger.warning(
            "[EMERGENCY/%s] خطر=%s | %s%s",
            response.phase,
            response.assessment.level.value,
            instruction.headline,
            draft,
        )
        for number, step in enumerate(instruction.steps, 1):
            logger.info("  %d. %s", number, step)
        for warning in instruction.warnings:
            logger.warning("  هشدار: %s", warning)
        if response.message:
            logger.info("\n%s", response.message)

        with EMERGENCY_FILE.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
                + "\n"
            )

        notify_telegram(
            telegram_notifier,
            emergency_to_event(response, profile.vehicle_id),
        )

    return RoadsideEmergencyService(
        gps=SimulatedGpsSource(
            scenario=SIM_GPS_SCENARIO,
            latitude=SIM_LAT,
            longitude=SIM_LON,
        ),
        road_analyzer=build_road_analyzer(),
        risk_analyzer=RiskAnalyzer(),
        recommender=RecommendationEngine(),
        contacts=EmergencyContacts(default_country=COUNTRY_CODE),
        vehicle=f"{profile.manufacturer} {profile.model}",
        country_code=COUNTRY_CODE,
        publish=publish,
    )


def save_record(record: VehicleRecord) -> None:
    """ذخیره آخرین اطلاعات خودرو در فایل JSON."""

    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            record.model_dump(mode="json"),
            file,
            ensure_ascii=False,
            indent=4,
        )


def save_events(events) -> None:
    """ذخیره رویدادهای تشخیص داده شده در فایل JSONL."""

    with EVENTS_FILE.open("a", encoding="utf-8") as file:
        for event in events:
            file.write(
                json.dumps(
                    event.model_dump(mode="json"),
                    ensure_ascii=False,
                ) + "\n"
            )


def run_cycle(
    client,
    validator,
    detector_engine,
    emergency,
    profile,
    source_name: str,
    telegram_notifier=None,
) -> bool:
    """اجرای یک چرخه دریافت، اعتبارسنجی و تشخیص اطلاعات خودرو."""

    # 1. دریافت اطلاعات خام از OBD
    raw_data = client.read_vehicle_data()

    # 2. اعتبارسنجی اطلاعات
    report = validator.validate(raw_data)

    if report.errors or report.data is None:
        for error in report.errors:
            logger.error("Validation error: %s", error)

        if report.data is None:
            logger.error("No valid vehicle data available.")

        return False

    for warning in report.warnings:
        logger.warning("Validation warning: %s", warning)

    # 3. ساخت رکورد خودرو
    record = VehicleRecord(
        vehicle_id=profile.vehicle_id,
        source=source_name,
        data=report.data,
    )

    # 4. ذخیره آخرین اطلاعات خودرو
    save_record(record)

    logger.info(
        "Vehicle data received:\n%s",
        json.dumps(
            report.data.model_dump(mode="json"),
            ensure_ascii=False,
            indent=4,
        ),
    )

    # 5. اجرای Detectorها
    events = detector_engine.process(record)

    # 6. ثبت رویدادها و ارسال به تلگرام
    for event in events:
        level = (
            logging.CRITICAL
            if event.severity == Severity.CRITICAL
            else logging.WARNING
        )
        logger.log(
            level,
            "[%s] %s | Vehicle=%s | Type=%s",
            event.severity.value,
            event.message,
            event.vehicle_id,
            event.event_type,
        )
        # رویدادهایی که سرویس اضطراری پوشش می‌دهد، با پیام‌های خودش
        # (فوری و نهایی) به تلگرام می‌روند؛ ارسال نسخه‌ی خام تکراری است.
        if event.event_type not in RoadsideEmergencyService.SUPPORTED_EVENTS:
            notify_telegram(telegram_notifier, event)

    if events:
        save_events(events)

    # 7. Roadside Emergency Assistant (هرگز استثنا نمی‌دهد و مسدود نمی‌کند)
    for event in events:
        emergency.handle(event)

    return True


def main():

    client = None
    emergency = None
    telegram_notifier = None

    try:
        # 1. دریافت پروفایل خودرو
        profile = get_vehicle_profile(settings.VEHICLE_ID)

        # 2. ساخت Validator
        validator = DataValidator(profile)

        # 3. ساخت DetectorEngine (فقط یک بار تا وضعیت Detectorها حفظ شود)
        detector_engine = build_detector_engine()

        # 4. تلگرام
        telegram_notifier = build_telegram_notifier()

        # 5. سرویس اضطراری
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        emergency = build_emergency_service(profile, telegram_notifier)

        # 6. اتصال به OBD
        client = OBDClient(settings.OBD_PORT)

        logger.info(
            "Monitoring started (port=%s, source=%s).",
            settings.OBD_PORT,
            SOURCE_NAME,
        )

        consecutive_errors = 0

        # 7. شروع مانیتورینگ پیوسته
        while True:

            try:
                ok = run_cycle(
                    client,
                    validator,
                    detector_engine,
                    emergency,
                    profile,
                    SOURCE_NAME,
                    telegram_notifier,
                )

            except Exception:
                logger.exception("Error in monitoring cycle.")
                ok = False

            if ok:
                consecutive_errors = 0

            else:
                consecutive_errors += 1

                logger.warning(
                    "Consecutive errors: %d/%d",
                    consecutive_errors,
                    MAX_CONSECUTIVE_ERRORS,
                )

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error(
                        "Stopping after %d consecutive failures.",
                        consecutive_errors,
                    )
                    break

            time.sleep(POLL_INTERVAL_S)

    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user.")

    except (ConnectionError, OSError, ValueError):
        logger.exception("Failed to start monitoring.")

    except Exception:
        logger.exception("Unexpected error in main process.")

    finally:
        if emergency is not None:
            emergency.close()
        if telegram_notifier is not None:
            # پیام‌های باقی‌مانده در صف (تا چند ثانیه) فرستاده می‌شوند
            telegram_notifier.close(timeout=5.0)
        if client is not None:
            client.close()
            logger.info("OBD connection closed.")


if __name__ == "__main__":
    main()
