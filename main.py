import json
import time
from pathlib import Path
import logging

from app.notifications.telegram_notifier import TelegramNotifier
from app.config import settings
from app.data_models import VehicleRecord
from app.data_validation import DataValidator
from app.detectors import (
    CoolantOverheatDetector,
    DetectorEngine,
    StoppageDetector,
    TroubleCodeDetector,
    VoltageDetector,
)
from app.logger import get_logger, setup_logging
from app.obd_client import OBDClient
from app.vehicle_profiles import get_vehicle_profile


setup_logging()
logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "vehicle_data.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"

POLL_INTERVAL_S = 1.0
MAX_CONSECUTIVE_ERRORS = 10
SOURCE_NAME = "OBD"


def build_detector_engine() -> DetectorEngine:
    """Building the DetectorEngine and all vehicle detectors."""

    return DetectorEngine([
        CoolantOverheatDetector(),
        TroubleCodeDetector(),
        VoltageDetector(),
        StoppageDetector(),
    ])


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
    profile,
    source_name: str,
    telegram_notifier=None
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

    # logger.info(
    #     "Vehicle data received: RPM=%s | Speed=%s km/h | Coolant=%s C",
    #     report.data.rpm,
    #     report.data.speed,
    #     report.data.coolant_temperature_c,
    # )

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

    # 6. ثبت رویدادها
    for event in events:
        level = (
            logging.CRITICAL
            if event.severity.value.upper() == "CRITICAL"
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

        if telegram_notifier is not None:
            telegram_notifier.notify_event(event)

    if events:
        save_events(events)

    return True


def main():

    client = None

    try:
        # 1. دریافت پروفایل خودرو
        profile = get_vehicle_profile(settings.VEHICLE_ID)

        # 2. ساخت Validator
        validator = DataValidator(profile)

        # 3. ساخت DetectorEngine
        # فقط یک بار ساخته می‌شود تا وضعیت Detectorها حفظ شود.
        detector_engine = build_detector_engine()

                # ساخت Telegram Notifier
        telegram_token = getattr(
            settings, "TELEGRAM_BOT_TOKEN", ""
        )
        telegram_chat_id = getattr(
            settings, "TELEGRAM_CHAT_ID", ""
        )

        telegram_notifier = None

        if telegram_token and telegram_chat_id:
            telegram_notifier = TelegramNotifier(
                token=telegram_token,
                chat_id=telegram_chat_id,
                cooldown_seconds=300,
            )
            logger.info("Telegram notifications enabled.")

        else:
            logger.warning(
                "Telegram notifications disabled: "
                "missing bot token or chat ID."
            )

        # 4. اتصال به OBD با استفاده از تنظیمات پروژه
        client = OBDClient(settings.OBD_PORT)

        # 5. ایجاد پوشه ذخیره اطلاعات
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Monitoring started (port=%s, source=%s).",
            settings.OBD_PORT,
            SOURCE_NAME,
        )

        consecutive_errors = 0

        # 6. شروع مانیتورینگ پیوسته
        while True:

            try:
                ok = run_cycle(
                    client,
                    validator,
                    detector_engine,
                    profile,
                    SOURCE_NAME,
                    telegram_notifier,
                )

            except Exception:
                logger.exception("Error in monitoring cycle.")
                ok = False

            # 7. مدیریت خطاهای متوالی
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

            # 8. فاصله زمانی بین دریافت اطلاعات
            time.sleep(POLL_INTERVAL_S)

    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user.")

    except (ConnectionError, OSError, ValueError):
        logger.exception("Failed to start monitoring.")

    except Exception:
        logger.exception("Unexpected error in main process.")

    finally:
        # 9. بستن اتصال OBD
        if client is not None:
            client.close()
            logger.info("OBD connection closed.")


if __name__ == "__main__":
    main()