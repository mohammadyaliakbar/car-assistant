from dataclasses import dataclass
from datetime import datetime, timezone

from app.detectors.event_detector import DetectionEvent
from app.emergency.models import RiskAssessment, RiskLevel
from app.location.models import GpsFix, RoadContext

# جاده‌های پرسرعت. trunk همیشه محدودالدسترس نیست، ولی محافظه‌کارانه
# وقتی is_limited_access نامعلوم است، مثل آزادراه با آن رفتار می‌شود.
FAST_ROAD_TYPES = {"motorway", "motorway_link", "trunk", "trunk_link"}
MAIN_ROAD_TYPES = {"primary", "primary_link"}
QUIET_STREET_TYPES = {"residential", "living_street", "service"}
STREET_TYPES = QUIET_STREET_TYPES | {
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
}

_LADDER = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]


@dataclass(frozen=True)
class RiskConfig:
    max_fix_age_s: float = 30.0         # قدیمی‌تر از این، معتبر نیست
    good_accuracy_m: float = 20.0       # دقت خوب
    poor_accuracy_m: float = 50.0       # بدتر از این، ضعیف
    fast_speed_kmh: float = 80.0        # سرعت زیاد پیش از توقف
    quiet_street_speed_kmh: float = 30.0
    emergency_bay_max_distance_m: float = 150.0
    speed_mismatch_kmh: float = 30.0    # اختلاف OBD و GPS


def _shift(level: RiskLevel, steps: int) -> RiskLevel:
    index = _LADDER.index(level) + steps
    return _LADDER[max(0, min(len(_LADDER) - 1, index))]


class RiskAnalyzer:
    """
    ارزیابی خطر محل توقف.

    اصول:
    - هرگز نتیجه‌ی «لاین فعال» یا «شانه» ادعا نمی‌شود.
    - جاده‌ی پرسرعت هیچ‌وقت امن فرض نمی‌شود، حتی با دقت ضعیف GPS.
    - اطلاعات ناکافی یعنی UNKNOWN، نه LOW.
    - هیچ تگ نقشه‌ای به‌عنوان حقیقت قطعی استفاده نمی‌شود.
    """

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()

    def assess(
        self,
        event: DetectionEvent,
        fix: GpsFix | None,
        road: RoadContext | None,
        crash_suspected: bool = False,
        now: datetime | None = None,
    ) -> RiskAssessment:
        cfg = self.config
        now = now or datetime.now(timezone.utc)
        reasons: list[str] = []

        # ۱) موقعیت معتبر وجود دارد؟
        if fix is None:
            return self._unknown("موقعیت GPS در دسترس نیست.")
        age = fix.age_s(now)
        if age > cfg.max_fix_age_s:
            return self._unknown(
                f"موقعیت GPS قدیمی است ({age:.0f} ثانیه)."
            )

        # ۲) اطلاعات جاده وجود دارد؟
        if road is None or road.data_quality == "unavailable":
            return self._unknown("اطلاعات نقشه در دسترس نیست.")

        accuracy = fix.accuracy_m
        poor = accuracy is None or accuracy > cfg.poor_accuracy_m
        good = accuracy is not None and accuracy <= cfg.good_accuracy_m

        hwy = road.highway_type
        fast = road.is_limited_access is True or (
            road.is_limited_access is None and hwy in FAST_ROAD_TYPES
        )

        speed = self._previous_speed(event, fix, reasons)
        high_speed = speed is not None and speed >= cfg.fast_speed_kmh

        # ۳) سطح پایه
        if fast:
            base = RiskLevel.HIGH
            reasons.append(
                "جاده‌ی پرسرعت و یا با دسترسی محدود است. موقعیت دقیق نسبت به "
                "مسیر عبور مشخص نیست و محل توقف امن فرض نمی‌شود."
            )
        elif poor:
            return self._unknown(
                "دقت GPS ضعیف است و جاده‌ی نزدیک روی نقشه ممکن است "
                "جاده‌ی واقعی نباشد."
            )
        elif road.is_parking_area is True:
            base = RiskLevel.LOW if good else RiskLevel.MEDIUM
            reasons.append("موقعیت روی پارکینگ ثبت شده است.")
        elif hwy in MAIN_ROAD_TYPES:
            base = RiskLevel.MEDIUM
            reasons.append("جاده‌ی اصلی شهری یا بین‌شهری.")
            if high_speed or (
                road.maxspeed_kmh is not None
                and road.maxspeed_kmh >= cfg.fast_speed_kmh
            ):
                base = RiskLevel.HIGH
                reasons.append("سرعت مجاز یا سرعت پیش از توقف زیاد بوده است.")
        elif hwy in STREET_TYPES:
            base = RiskLevel.MEDIUM
            reasons.append("خیابان یا جاده‌ی فرعی.")
            if (
                hwy in QUIET_STREET_TYPES
                and speed is not None
                and speed < cfg.quiet_street_speed_kmh
            ):
                base = RiskLevel.LOW
                reasons.append("خیابان کم‌تردد و سرعت پیش از توقف کم بوده است.")
            elif high_speed:
                base = RiskLevel.HIGH
                reasons.append("سرعت پیش از توقف زیاد بوده است.")
        else:
            return self._unknown("نوع جاده نامشخص یا پشتیبانی‌نشده است.")

        # ۴) افزایش‌ها
        up = 0
        if road.no_stopping_tagged is True:
            up += 1
            reasons.append("روی نقشه محدودیت توقف ثبت شده است.")
        if road.is_winding is True:
            up += 1
            reasons.append("مسیر پیچ‌دار است و دید ممکن است کم باشد.")
        if crash_suspected:
            up += 1
            reasons.append("احتمال تصادف نیز گزارش شده است.")

        # ۵) کاهش (فقط یک سطح، فقط با دقت خوب و بدون عامل افزاینده)
        down = 0
        if (
            fast
            and up == 0
            and good
            and road.has_emergency_bay_nearby is True
            and road.emergency_bay_distance_m is not None
            and road.emergency_bay_distance_m <= cfg.emergency_bay_max_distance_m
        ):
            down = 1
            reasons.append(
                f"محل توقف اضطراری در حدود "
                f"{road.emergency_bay_distance_m:.0f} متری ثبت شده است."
            )

        level = _shift(base, up - down)

        # ۶) اطمینان
        if poor:
            confidence = "low"
            reasons.append("دقت GPS ضعیف است؛ نتیجه با اطمینان کم.")
        elif good and road.data_quality == "ok":
            confidence = "high"
        else:
            confidence = "medium"

        return RiskAssessment(level=level, reasons=reasons, confidence=confidence)

    def _previous_speed(
        self,
        event: DetectionEvent,
        fix: GpsFix,
        reasons: list[str],
    ) -> float | None:
        """بیشترین سرعت شناخته‌شده را برمی‌گرداند (محافظه‌کارانه)."""
        obd = event.details.get("previous_speed_kmh")
        values = [
            float(v)
            for v in (obd, fix.speed_kmh)
            if isinstance(v, (int, float))
        ]
        if not values:
            return None
        if len(values) == 2 and abs(values[0] - values[1]) > self.config.speed_mismatch_kmh:
            reasons.append(
                "سرعت OBD و GPS اختلاف زیادی دارند؛ مقدار بیشتر استفاده شد."
            )
        return max(values)

    @staticmethod
    def _unknown(reason: str) -> RiskAssessment:
        return RiskAssessment(
            level=RiskLevel.UNKNOWN,
            reasons=[reason],
            confidence="low",
        )