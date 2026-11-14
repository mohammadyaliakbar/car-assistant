import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.detectors.event_detector import DetectionEvent
from app.emergency.emergency_contacts import EmergencyContacts, format_message
from app.emergency.models import (
    EmergencyReport,
    EmergencyResponse,
    Instruction,
    RiskAssessment,
    RiskLevel,
)
from app.emergency.recommendation_engine import LAST_RESORT, RecommendationEngine
from app.emergency.risk_analyzer import RiskAnalyzer
from app.location.gps_client import GpsSource
from app.location.models import GpsFix, RoadContext
from app.location.road_analyzer import RoadAnalyzer

logger = logging.getLogger(__name__)

PublishFunction = Callable[[EmergencyResponse], None]
SubmitFunction = Callable[[Callable[[], None]], Any]


class RoadsideEmergencyService:
    """
    هماهنگ‌کننده‌ی Roadside Emergency Assistant.

    رویداد Detector را می‌گیرد، راهنمای فوری را منتشر می‌کند، و بعد از
    رسیدن اطلاعات نقشه راهنمای دقیق‌تر را منتشر می‌کند. هیچ تماس
    خودکاری برقرار نمی‌شود.
    """

    CRASH_EVENT = "crash_suspected"
    SUPPORTED_EVENTS = frozenset({"engine_stalled_while_moving", CRASH_EVENT})

    def __init__(
        self,
        gps: GpsSource,
        road_analyzer: RoadAnalyzer,
        risk_analyzer: RiskAnalyzer,
        recommender: RecommendationEngine,
        contacts: EmergencyContacts,
        vehicle: str,
        country_code: str | None = None,
        cooldown_s: float = 60.0,
        publish: PublishFunction | None = None,
        submit: SubmitFunction | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.gps = gps
        self.roads = road_analyzer
        self.risk = risk_analyzer
        self.recommender = recommender
        self.contacts = contacts
        self.vehicle = vehicle
        self.country_code = country_code
        self.cooldown_s = cooldown_s
        self._publish_fn = publish
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._executor: ThreadPoolExecutor | None = None
        if submit is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="roadside"
            )
            submit = self._executor.submit
        self._submit = submit

        self._lock = threading.Lock()
        self._generation = 0
        # تکراری‌بودن فقط در همان نوع رویداد سنجیده می‌شود (خاموشی موتور
        # که نتیجه‌ی تصادف است نباید رویداد تصادف را حذف کند)
        self._last_event_times: dict[str, datetime] = {}

    # ------------------------------------------------------------ API

    def handle(
        self,
        event: DetectionEvent,
        crash_suspected: bool = False,
    ) -> EmergencyResponse | None:
        """
        رویداد را پردازش می‌کند و پاسخ فوری را برمی‌گرداند.
        برای رویداد پشتیبانی‌نشده یا تکراری None برمی‌گرداند. استثنا نمی‌دهد.
        """
        if event.event_type not in self.SUPPORTED_EVENTS:
            return None
        if event.event_type == self.CRASH_EVENT:
            crash_suspected = True   # خودِ رویداد یعنی احتمال تصادف

        with self._lock:
            if self._is_duplicate(event):
                logger.info("Duplicate emergency event ignored: %s", event.event_type)
                return None
            self._last_event_times[event.event_type] = event.timestamp
            self._generation += 1
            generation = self._generation

        fix: GpsFix | None = None
        try:
            now = self._clock()
            fix = self._get_fix()
            assessment = self._initial_assessment(event, fix, crash_suspected, now)
            initial = self._build("initial", event, fix, None, assessment)
        except Exception:
            logger.exception("Initial emergency response failed; using last resort.")
            initial = self._fallback(event)

        self._publish(initial)

        try:
            self._submit(
                lambda: self._finalize(event, fix, crash_suspected, generation)
            )
        except Exception:
            logger.exception("Could not schedule the map lookup.")

        return initial

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ فاز دوم

    def _finalize(
        self,
        event: DetectionEvent,
        fix: GpsFix | None,
        crash_suspected: bool,
        generation: int,
    ) -> None:
        try:
            now = self._clock()
            if fix is None:
                fix = self._get_fix()   # شاید حالا موقعیت رسیده باشد
            road = self.roads.analyze(fix)
            assessment = self.risk.assess(
                event, fix, road, crash_suspected=crash_suspected, now=now
            )
            final = self._build("final", event, fix, road, assessment)
        except Exception:
            logger.exception("Final emergency assessment failed; keeping initial guidance.")
            return

        with self._lock:
            if generation != self._generation:
                logger.info("Final result discarded: a newer event arrived.")
                return
        self._publish(final)

    # ------------------------------------------------------------ کمکی

    def _is_duplicate(self, event: DetectionEvent) -> bool:
        last = self._last_event_times.get(event.event_type)
        if last is None:
            return False
        return (event.timestamp - last).total_seconds() < self.cooldown_s

    def _get_fix(self) -> GpsFix | None:
        try:
            return self.gps.get_fix()
        except Exception:
            logger.exception("GPS read failed.")
            return None

    def _initial_assessment(
        self,
        event: DetectionEvent,
        fix: GpsFix | None,
        crash_suspected: bool,
        now: datetime,
    ) -> RiskAssessment:
        """بدون اطلاعات نقشه همیشه UNKNOWN است، با دلیل مناسب."""
        base = self.risk.assess(
            event, fix, None, crash_suspected=crash_suspected, now=now
        )
        fix_ok = fix is not None and fix.age_s(now) <= self.risk.config.max_fix_age_s
        if fix_ok:
            return RiskAssessment(
                level=RiskLevel.UNKNOWN,
                reasons=["موقعیت GPS دریافت شد؛ اطلاعات نقشه هنوز در راه است."],
                confidence="low",
            )
        return base   # دلیل GPS (نبود یا قدیمی بودن) را نگه می‌دارد

    def _build(
        self,
        phase: str,
        event: DetectionEvent,
        fix: GpsFix | None,
        road: RoadContext | None,
        assessment: RiskAssessment,
    ) -> EmergencyResponse:
        instruction = self.recommender.select(event, assessment, road)
        report: EmergencyReport | None = None
        message: str | None = None
        if self.contacts.should_prepare(assessment):
            report = self.contacts.build_report(
                event, fix, road, assessment, instruction,
                self.vehicle, self.country_code,
            )
            message = format_message(report)
        return EmergencyResponse(
            phase=phase,
            event_type=event.event_type,
            event_timestamp=event.timestamp,
            assessment=assessment,
            instruction=instruction,
            report=report,
            message=message,
        )

    def _fallback(self, event: DetectionEvent) -> EmergencyResponse:
        return EmergencyResponse(
            phase="initial",
            event_type=event.event_type,
            event_timestamp=event.timestamp,
            assessment=RiskAssessment(
                level=RiskLevel.UNKNOWN,
                reasons=["خطای داخلی؛ راهنمای عمومی ایمنی نمایش داده شد."],
                confidence="low",
            ),
            instruction=LAST_RESORT,
        )

    def _publish(self, response: EmergencyResponse) -> None:
        if self._publish_fn is None:
            return
        try:
            self._publish_fn(response)
        except Exception:
            logger.exception("Publishing the emergency response failed.")