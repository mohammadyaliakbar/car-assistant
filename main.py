import argparse
import json
import logging
import sys
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
from app.motion import CrashDetector, CrashMonitor, CrashResponder
from app.motion.console_confirmation import ConsoleAnswerListener, make_console_prompt
from app.motion.crash_response import TIMEOUT_POLICIES
from app.notifications.telegram_notifier import TelegramNotifier
from app.obd_client import OBDClient
from app.simulation import (
    DRIVER_ACTIONS,
    SCENARIOS,
    DriverBehavior,
    SimulatedDriver,
    SimulatedOBDClient,
    SimulatedWorld,
    SimulationOptions,
    WorldGpsSource,
    resolve_crash_policy,
)
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
SIM_SOURCE_NAME = "SIMULATOR"

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
    # پاسخ‌های ناشی از تصادف نوع جدا دارند، وگرنه cooldown تلگرام آن‌ها را بعد از
    # پاسخ‌های ناشی از خاموشی موتور (همان حادثه) تکراری می‌پندارد و حذف می‌کند.
    prefix = (
        "roadside_crash"
        if response.event_type == CrashDetector.EVENT_TYPE
        else "roadside_emergency"
    )
    return DetectionEvent(
        detector="roadside_emergency",
        event_type=f"{prefix}_{response.phase}",
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


def build_emergency_service(
    profile,
    telegram_notifier,
    gps=None,
    crash_alert=None,
) -> RoadsideEmergencyService:
    """
    gps: منبع GPS (پیش‌فرض: شبیه‌ساز ساده تا GPS واقعی گوشی وصل شود).
    crash_alert: تابعی که پیام‌های مرتبط با تصادف را می‌گیرد. این پیام‌ها فقط
        بعد از تأیید راننده به تلگرام می‌روند (نگاه کنید به CrashResponder).
    """
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

        alert = emergency_to_event(response, profile.vehicle_id)
        if response.event_type == CrashDetector.EVENT_TYPE and crash_alert is not None:
            crash_alert(alert)
        else:
            notify_telegram(telegram_notifier, alert)

    return RoadsideEmergencyService(
        gps=gps or SimulatedGpsSource(
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
    crash_monitor=None,
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

    # سرعت و دور موتور برای تشخیص تصادف (حلقه‌ی تشخیص تصادف جداست)
    if crash_monitor is not None:
        crash_monitor.update_vehicle(
            record.timestamp, record.data.speed, record.data.rpm
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


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="دستیار هوشمند خودرو")

    source = parser.add_argument_group("منبع داده (پیش‌فرض: OBD واقعی)")
    source.add_argument("--simulate", metavar="SCENARIO", choices=list(SCENARIOS),
                        help="شبیه‌ساز درون‌برنامه‌ای به‌جای OBD واقعی")
    source.add_argument("--port", help="پورت OBD (پیش‌فرض: settings.OBD_PORT)")
    source.add_argument("--list-scenarios", action="store_true",
                        help="فهرست سناریوهای شبیه‌ساز")

    sim = parser.add_argument_group("گزینه‌های شبیه‌ساز (فقط با --simulate)")
    sim.add_argument("--event-time", type=float, default=15.0,
                     help="رویداد سناریو چند ثانیه بعد از شروع رخ دهد (پیش‌فرض ۱۵)")
    sim.add_argument("--lat", type=float, default=SIM_LAT)
    sim.add_argument("--lon", type=float, default=SIM_LON)
    sim.add_argument("--gps", choices=SimulatedGpsSource.SCENARIOS, default=SIM_GPS_SCENARIO,
                     help="وضعیت GPS شبیه‌سازی‌شده")
    sim.add_argument("--driver", choices=DRIVER_ACTIONS, default="none",
                     help="رفتار راننده‌ی شبیه‌سازی‌شده هنگام پرسش تصادف")
    sim.add_argument("--driver-delay", type=float, default=10.0,
                     help="چند ثانیه بعد از پرسش، راننده‌ی شبیه‌سازی‌شده پاسخ دهد")
    sim.add_argument("--duration", type=float,
                     help="بعد از این‌همه ثانیه برنامه خودکار پایان یابد")

    crash = parser.add_argument_group("تأیید راننده در تصادف")
    crash.add_argument("--confirm-timeout", type=float,
                       help="مهلت پاسخ راننده به ثانیه (پیش‌فرض ۶۰)")
    crash.add_argument("--on-timeout", choices=TIMEOUT_POLICIES,
                       help="اگر راننده پاسخ نداد: send (پیش‌فرض) یا skip")

    parser.add_argument("--no-telegram", action="store_true",
                        help="تلگرام غیرفعال شود (مفید برای آزمایش)")

    args = parser.parse_args(argv)
    if args.simulate and args.port:
        parser.error("--simulate و --port را با هم نمی‌توان داد.")
    return args


def simulation_from_args(args: argparse.Namespace) -> SimulationOptions | None:
    if not args.simulate:
        return None
    return SimulationOptions(
        scenario=args.simulate,
        event_time_s=args.event_time,
        latitude=args.lat,
        longitude=args.lon,
        gps_mode=args.gps,
        driver=DriverBehavior(args.driver, args.driver_delay),
    )


def main(argv=None, simulation: SimulationOptions | None = None) -> int:
    """
    simulation: اگر داده شود (مثلاً از یک اسکریپت پایتونی شبیه‌ساز در همین
    پردازش)، بر گزینه‌های --simulate اولویت دارد.
    """
    global DATA_FILE, EVENTS_FILE, EMERGENCY_FILE

    args = parse_args(argv)

    if args.list_scenarios:
        for name, definition in SCENARIOS.items():
            print(f"{name:22} {definition.description}")
        return 0

    try:
        simulation = simulation or simulation_from_args(args)
        crash_timeout_s, crash_on_timeout = resolve_crash_policy(
            cli_timeout_s=args.confirm_timeout,
            cli_on_timeout=args.on_timeout,
            simulation=simulation,
            settings_timeout_s=getattr(settings, "CRASH_CONFIRM_TIMEOUT_S", None),
            settings_on_timeout=getattr(settings, "CRASH_ON_TIMEOUT", None),
        )
    except ValueError as error:
        print(f"گزینه‌ی نامعتبر: {error}", file=sys.stderr)
        return 2

    client = None
    emergency = None
    telegram_notifier = None
    crash_monitor = None
    simulated_driver = None
    responder = None
    exit_code = 0

    try:
        # 1. دریافت پروفایل خودرو
        profile = get_vehicle_profile(settings.VEHICLE_ID)

        # 2. ساخت Validator
        validator = DataValidator(profile)

        # 3. ساخت DetectorEngine (فقط یک بار تا وضعیت Detectorها حفظ شود)
        detector_engine = build_detector_engine()

        # 4. تلگرام
        telegram_notifier = None if args.no_telegram else build_telegram_notifier()

        # 5. حالت اجرا: شبیه‌ساز درون‌برنامه‌ای یا OBD واقعی
        world = None
        gps = None
        source_name = SOURCE_NAME
        if simulation is not None:
            world = SimulatedWorld(
                simulation.scenario,
                event_time_s=simulation.event_time_s,
                latitude=simulation.latitude,
                longitude=simulation.longitude,
                gps_mode=simulation.gps_mode,
            )
            gps = WorldGpsSource(world)
            source_name = SIM_SOURCE_NAME
            # داده‌ی شبیه‌سازی با داده‌ی واقعی مخلوط نشود
            sim_dir = DATA_DIR / "simulation"
            DATA_FILE = sim_dir / "vehicle_data.json"
            EVENTS_FILE = sim_dir / "events.jsonl"
            EMERGENCY_FILE = sim_dir / "emergency.jsonl"
            sim_dir.mkdir(parents=True, exist_ok=True)
            logger.warning(
                "SIMULATION MODE: scenario=%s | event at +%.0fs | %s",
                simulation.scenario,
                simulation.event_time_s,
                SCENARIOS[simulation.scenario].description,
            )

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 6. سرویس اضطراری؛ پیام‌های تصادف از دروازه‌ی تأیید راننده می‌گذرند
        def submit_crash_alert(event: DetectionEvent) -> None:
            if responder is not None:
                responder.submit_alert(event)
            else:
                notify_telegram(telegram_notifier, event)

        emergency = build_emergency_service(
            profile, telegram_notifier, gps=gps, crash_alert=submit_crash_alert
        )

        # 7. تشخیص تصادف از حسگرهای گوشی (فعلاً فقط از شبیه‌ساز)
        if world is not None:
            if simulation.driver.action != "none":
                simulated_driver = SimulatedDriver(simulation.driver)
            prompt_handlers = [make_console_prompt(crash_timeout_s, crash_on_timeout)]
            if simulated_driver is not None:
                prompt_handlers.append(simulated_driver.on_prompt)

            def prompt(request) -> None:
                for handler in prompt_handlers:
                    try:
                        handler(request)
                    except Exception:
                        logger.exception("Crash prompt handler failed.")

            responder = CrashResponder(
                emergency_handler=lambda event: emergency.handle(
                    event, crash_suspected=True
                ),
                send_alert=lambda event: notify_telegram(telegram_notifier, event),
                prompt=prompt,
                timeout_s=crash_timeout_s,
                on_timeout=crash_on_timeout,
            )
            if simulated_driver is not None:
                simulated_driver.attach(responder)

            crash_monitor = CrashMonitor(
                world.motion_source(), CrashDetector(profile.vehicle_id), responder
            )
            crash_monitor.start()
            if sys.stdin is not None and sys.stdin.isatty():
                ConsoleAnswerListener(responder).start()
            logger.info(
                "Crash detection enabled (confirm timeout=%.0fs, on timeout=%s).",
                crash_timeout_s,
                crash_on_timeout,
            )
        else:
            logger.warning(
                "Crash detection disabled: no phone motion source is connected yet."
            )

        # 8. اتصال به منبع داده
        if world is not None:
            client = SimulatedOBDClient(world)
            port_label = "simulator"
        else:
            port_label = args.port or settings.OBD_PORT
            client = OBDClient(port_label)

        logger.info(
            "Monitoring started (port=%s, source=%s).",
            port_label,
            source_name,
        )

        consecutive_errors = 0
        deadline = time.monotonic() + args.duration if args.duration else None

        # 9. شروع مانیتورینگ پیوسته
        while True:

            try:
                ok = run_cycle(
                    client,
                    validator,
                    detector_engine,
                    emergency,
                    profile,
                    source_name,
                    telegram_notifier,
                    crash_monitor,
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
                    exit_code = 1
                    break

            if getattr(client, "finished", False):
                logger.info("Simulation finished.")
                break
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Requested duration reached.")
                break

            time.sleep(POLL_INTERVAL_S)

    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user.")

    except (ConnectionError, OSError, ValueError):
        logger.exception("Failed to start monitoring.")
        exit_code = 1

    except Exception:
        logger.exception("Unexpected error in main process.")
        exit_code = 1

    finally:
        if crash_monitor is not None:
            crash_monitor.stop()
        if simulated_driver is not None:
            simulated_driver.close()
        if emergency is not None:
            emergency.close()
        if telegram_notifier is not None:
            # پیام‌های باقی‌مانده در صف (تا چند ثانیه) فرستاده می‌شوند
            telegram_notifier.close(timeout=5.0)
        if client is not None:
            client.close()
            logger.info("OBD connection closed.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
