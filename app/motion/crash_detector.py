# app/motion/crash_detector.py
"""
تشخیص «احتمالی» تصادف از شتاب‌سنج/ژیروسکوپ گوشی و سرعت خودرو.

اصول:
- خروجی فقط «مشکوک به تصادف» است (`crash_suspected`)، نه قطعی.
- یک ضربه‌ی تنها کافی نیست (افتادن گوشی، دست‌انداز، بستن در). برای
  رویداد باید قرینه‌ی دیگری هم باشد، به‌ویژه افت ناگهانی سرعت.
- نبود داده نامعلوم است (`None`)، نه «تأییدنشده» و نه «تأییدشده». اگر
  سرعت معلوم باشد و افت نکرده باشد، ضربه رد می‌شود. اگر سرعت نامعلوم
  باشد، فقط با قرینه‌های قوی‌تر و با اطمینان «low» رویداد می‌دهد.
- منابع قرینه: سرعت خودرو، حسگرهای گوشی (چرخش، سکون) و خاموش‌شدن موتور.
  خاموش‌شدن موتور فقط قرینه‌ی تأییدکننده است: همه‌ی تصادف‌ها موتور را
  خاموش نمی‌کنند، پس «موتور هنوز روشن است» ضربه را رد نمی‌کند. اگر موتور
  پیش از ضربه هم خاموش یا نامعلوم باشد (مثلاً خودروی برقی/هیبریدی)،
  این قرینه نامعلوم است.
- مستقل از Roadside Emergency است؛ اتصال به `crash_suspected` بعداً انجام
  می‌شود.

هشدار: همه‌ی آستانه‌های `CrashConfig` حدسی‌اند و با داده‌ی واقعی
(آزمایش کنترل‌شده یا ثبت داده) تنظیم نشده‌اند.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from app.detectors.event_detector import DetectionEvent, Severity
from app.motion.models import G, MotionSample


@dataclass(frozen=True)
class CrashConfig:
    impact_g: float = 4.0            # آستانه‌ی ضربه (بزرگی شتاب کل)
    severe_impact_g: float = 8.0     # ضربه‌ی شدید
    confirm_window_s: float = 8.0    # مهلت جمع‌آوری قرینه بعد از ضربه
    min_speed_kmh: float = 20.0      # حداقل سرعت پیش از ضربه
    speed_max_age_s: float = 6.0     # سرعت پیش از ضربه حداکثر چقدر قدیمی باشد
    engine_on_rpm: float = 500.0     # هم‌خوان با StoppageDetector
    engine_max_age_s: float = 6.0    # rpm پیش از ضربه حداکثر چقدر قدیمی باشد
    stop_speed_kmh: float = 5.0      # زیر این مقدار «ایستاده»
    drop_ratio: float = 0.6          # افت حداقل ۶۰٪ سرعت قبلی
    gyro_spin_rad_s: float = 4.0     # چرخش شدید (حدود ۲۳۰ درجه بر ثانیه)
    settle_s: float = 1.0            # صبر بعد از آخرین ضربه، پیش از سنجش سکون
    still_duration_s: float = 3.0    # مدت سکون لازم
    still_tol_ms2: float = 1.5       # نوسان مجاز شتاب در سکون
    still_gyro_rad_s: float = 0.5    # نوسان مجاز ژیروسکوپ در سکون
    cooldown_s: float = 60.0         # بعد از یک رویداد، رویداد تکراری نده
    includes_gravity: bool = True    # شتاب‌سنج شامل گرانش است؟


@dataclass
class _Pending:
    impact_ts: datetime
    deadline: datetime
    peak_g: float
    last_high_ts: datetime
    peak_gyro: float | None
    gyro_seen: bool
    still_since: datetime | None = None
    still: bool = False
    still_samples: int = 0


class CrashDetector:
    name = "crash"
    EVENT_TYPE = "crash_suspected"

    def __init__(self, vehicle_id: str, config: CrashConfig | None = None):
        self.vehicle_id = vehicle_id
        self.config = config or CrashConfig()
        self.reset()

    def reset(self) -> None:
        self._speeds: deque[tuple[datetime, float]] = deque(maxlen=64)
        self._rpms: deque[tuple[datetime, float]] = deque(maxlen=64)
        self._recent_gyro: deque[tuple[datetime, float]] = deque()
        self._last_ts: datetime | None = None
        self._last_sample_ts: datetime | None = None
        self._hold_until: datetime | None = None
        self._pending: _Pending | None = None

    # ------------------------------------------------------------ ورودی‌ها

    def update_speed(self, speed_kmh: float | None, timestamp: datetime) -> None:
        """سرعت خودرو (از OBD یا GPS). مقدار نامعلوم نادیده گرفته می‌شود."""
        if speed_kmh is None or not math.isfinite(speed_kmh) or speed_kmh < 0:
            return
        if self._speeds and timestamp < self._speeds[-1][0]:
            return
        self._speeds.append((timestamp, speed_kmh))

    def update_engine(self, rpm: float | None, timestamp: datetime) -> None:
        """دور موتور (از OBD). مقدار نامعلوم نادیده گرفته می‌شود."""
        if rpm is None or not math.isfinite(rpm) or rpm < 0:
            return
        if self._rpms and timestamp < self._rpms[-1][0]:
            return
        self._rpms.append((timestamp, rpm))

    def update_vehicle(
        self,
        timestamp: datetime,
        speed_kmh: float | None = None,
        rpm: float | None = None,
    ) -> None:
        """میان‌بر برای داده‌ی یک رکورد OBD (سرعت و دور موتور)."""
        self.update_speed(speed_kmh, timestamp)
        self.update_engine(rpm, timestamp)

    def process(self, sample: MotionSample) -> list[DetectionEvent]:
        """یک نمونه‌ی حسگر را پردازش می‌کند و رویدادهای تازه را برمی‌گرداند."""
        ts = sample.timestamp
        if self._last_sample_ts is not None and ts < self._last_sample_ts:
            return []  # نمونه‌ی قدیمی‌تر از قبلی؛ نادیده گرفته می‌شود
        gap = (
            (ts - self._last_sample_ts).total_seconds()
            if self._last_sample_ts is not None
            else 0.0
        )
        self._last_sample_ts = ts

        events = self._expire(ts)

        g = sample.accel_magnitude_g
        gyro = sample.gyro_magnitude_rad_s

        if self._pending is None:
            held = self._hold_until is not None and ts < self._hold_until
            if g >= self.config.impact_g and not held:
                self._start(sample, g, gyro)
        else:
            self._update(sample, g, gyro, gap)

        if gyro is not None:
            self._recent_gyro.append((ts, gyro))
            while self._recent_gyro and (ts - self._recent_gyro[0][0]).total_seconds() > 0.5:
                self._recent_gyro.popleft()
        return events

    def poll(self, now: datetime) -> list[DetectionEvent]:
        """
        برای وقتی که نمونه‌ی تازه‌ای نمی‌رسد (مثلاً گوشی بعد از ضربه از کار
        افتاده): مهلت قرینه‌ها را با زمان واقعی می‌بندد.
        """
        return self._expire(now)

    # ------------------------------------------------------------ وضعیت

    def _start(self, sample: MotionSample, g: float, gyro: float | None) -> None:
        ts = sample.timestamp
        recent = [v for _, v in self._recent_gyro]
        if gyro is not None:
            recent.append(gyro)
        self._pending = _Pending(
            impact_ts=ts,
            deadline=ts + timedelta(seconds=self.config.confirm_window_s),
            peak_g=g,
            last_high_ts=ts,
            peak_gyro=max(recent) if recent else None,
            gyro_seen=bool(recent),
        )

    def _update(self, sample: MotionSample, g: float, gyro: float | None, gap: float) -> None:
        cfg = self.config
        p = self._pending
        ts = sample.timestamp

        if g >= cfg.impact_g:
            p.peak_g = max(p.peak_g, g)
            p.last_high_ts = ts
            p.still_since = None
        if gyro is not None:
            p.gyro_seen = True
            p.peak_gyro = gyro if p.peak_gyro is None else max(p.peak_gyro, gyro)

        # سکون: فقط بعد از فروکش‌کردن ضربه
        if (ts - p.last_high_ts).total_seconds() < cfg.settle_s:
            return
        p.still_samples += 1
        if gap > 1.0:  # شکاف زیاد در نمونه‌ها: سکونِ قبلی معتبر نیست
            p.still_since = None

        reference = G if cfg.includes_gravity else 0.0
        quiet = abs(sample.accel_magnitude_ms2 - reference) <= cfg.still_tol_ms2
        if gyro is not None and gyro > cfg.still_gyro_rad_s:
            quiet = False
        if not quiet:
            p.still_since = None
            return
        if p.still_since is None:
            p.still_since = ts
        if (ts - p.still_since).total_seconds() >= cfg.still_duration_s:
            p.still = True

    def _expire(self, now: datetime) -> list[DetectionEvent]:
        if self._pending is not None and now >= self._pending.deadline:
            return self._finalize(now)
        return []

    # ------------------------------------------------------------ تصمیم

    def _speed_before(self, impact_ts: datetime) -> float | None:
        for ts, speed in reversed(self._speeds):
            if ts <= impact_ts:
                age = (impact_ts - ts).total_seconds()
                return speed if age <= self.config.speed_max_age_s else None
        return None

    def _speed_after(self, p: _Pending) -> float | None:
        last = None
        for ts, speed in self._speeds:
            if p.impact_ts < ts <= p.deadline:
                last = speed
        return last

    def _engine_off(self, p: _Pending) -> tuple[bool | None, float | None, float | None]:
        """
        (engine_off, rpm_before, rpm_after).
        True فقط وقتی موتور پیش از ضربه روشن بوده و بعد از آن خاموش شده است.
        """
        cfg = self.config
        before = None
        for ts, rpm in reversed(self._rpms):
            if ts <= p.impact_ts:
                age = (p.impact_ts - ts).total_seconds()
                before = rpm if age <= cfg.engine_max_age_s else None
                break
        after = None
        for ts, rpm in self._rpms:
            if p.impact_ts < ts <= p.deadline:
                after = rpm

        if before is None or before < cfg.engine_on_rpm or after is None:
            return None, before, after  # نامعلوم یا مرتبط نیست
        return after < cfg.engine_on_rpm, before, after

    def _finalize(self, now: datetime) -> list[DetectionEvent]:
        cfg = self.config
        p = self._pending
        self._pending = None

        before = self._speed_before(p.impact_ts)
        after = self._speed_after(p)

        # هر قرینه: True / False / None (نامعلوم)
        if before is None:
            drop = None
        elif before < cfg.min_speed_kmh:
            drop = False  # ضربه در حال ایستادن یا سرعت کم (مثل پارک)
        elif after is None:
            drop = None
        else:
            drop = after <= max(cfg.stop_speed_kmh, before * (1 - cfg.drop_ratio))
        spin = None if not p.gyro_seen else p.peak_gyro >= cfg.gyro_spin_rad_s
        still = None if p.still_samples == 0 else p.still

        engine_off, rpm_before, rpm_after = self._engine_off(p)

        severe = p.peak_g >= cfg.severe_impact_g
        phone_signals = (spin is True) + (still is True)
        verdict = None
        if drop is False:
            # سرعت معلوم و افت نکرده؛ فقط ضربه‌ی شدید همراه خاموشی موتور
            # (قرینه‌ی مستقل از گوشی) هنوز مشکوک است، آن هم با اطمینان کم.
            if engine_off is True and severe:
                verdict = (Severity.WARNING, "low")
        elif drop is True:
            corroborated = phone_signals >= 1 or engine_off is True or severe
            verdict = (
                (Severity.CRITICAL, "high") if corroborated
                else (Severity.WARNING, "medium")
            )
        else:  # افت سرعت نامعلوم
            if engine_off is True and phone_signals >= 1 and severe:
                verdict = (Severity.WARNING, "medium")
            elif phone_signals == 2 and severe:
                verdict = (Severity.WARNING, "low")  # فقط حسگر گوشی

        if verdict is None:
            self._hold_until = now + timedelta(seconds=1.0)
            return []

        severity, confidence = verdict
        self._hold_until = now + timedelta(seconds=cfg.cooldown_s)
        return [
            DetectionEvent(
                detector=self.name,
                event_type=self.EVENT_TYPE,
                severity=severity,
                message=(
                    f"Possible crash detected: {p.peak_g:.1f} g impact "
                    f"(confidence: {confidence})."
                ),
                vehicle_id=self.vehicle_id,
                timestamp=p.impact_ts,
                details={
                    "peak_g": round(p.peak_g, 1),
                    "speed_before_kmh": before,
                    # کلیدی که RiskAnalyzer از رویدادهای دیگر می‌شناسد
                    "previous_speed_kmh": before,
                    "speed_after_kmh": after,
                    "speed_drop": drop,
                    "spin": spin,
                    "still": still,
                    "engine_off": engine_off,
                    "rpm_before": rpm_before,
                    "rpm_after": rpm_after,
                    "confidence": confidence,
                },
            )
        ]
