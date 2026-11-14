# app/motion/simulator.py
"""
شبیه‌ساز حسگر حرکتی گوشی و داده‌ی OBD (سرعت و دور موتور) برای آزمایش
بدون خودرو.

همه‌ی سناریوها قطعی‌اند (seed ثابت). مقادیر شتاب، چرخش و دور موتور
«تقریبی و ساختگی»اند و جای داده‌ی واقعی را نمی‌گیرند.
"""

import math
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.motion.models import G, MotionSample

SCENARIOS = (
    "normal_driving",
    "hard_brake",
    "pothole",
    "phone_drop",
    "low_speed_bump",
    "crash",
    "crash_no_speed",
    "crash_engine_off",
    "crash_engine_running",
    "engine_stall",
)

_GRAVITY_VECTOR = (2.0, 3.0, math.sqrt(G**2 - 2.0**2 - 3.0**2))  # گوشی کج، بزرگی = G
_SHOCK_DIRECTION = (0.8, 0.5, math.sqrt(1 - 0.8**2 - 0.5**2))

Profile = Callable[[float], float]


@dataclass(frozen=True)
class SimStep:
    sample: MotionSample
    speed_kmh: float | None  # فقط در لحظه‌ی خواندن OBD (مثلاً هر ۲ ثانیه)
    rpm: float | None = None  # هم‌زمان با سرعت، در صورت در دسترس بودن


@dataclass(frozen=True)
class _Spec:
    speed: Profile
    rpm: Profile
    shocks: list = ()           # (شروع، مدت، بزرگی بر حسب g)
    bursts: list = ()           # (شروع، مدت، rad/s)
    quiet_after: float | None = None  # گوشی بعد از این لحظه تقریباً بی‌حرکت است
    braking: tuple | None = None      # (شروع، مدت) ترمز ملایم
    speed_available: bool = True
    rpm_available: bool = True


def _rpm_from_speed(speed: Profile) -> Profile:
    return lambda t: 800.0 + 25.0 * speed(t)  # دور آرام + متناسب با سرعت


class MotionSimulator:
    def __init__(
        self,
        rate_hz: float = 50.0,
        duration_s: float = 30.0,
        event_time_s: float = 15.0,
        speed_interval_s: float = 2.0,
        seed: int = 1,
        start: datetime | None = None,
    ):
        self.rate_hz = rate_hz
        self.duration_s = duration_s
        self.event_time_s = event_time_s
        self.speed_interval_s = speed_interval_s
        self.seed = seed
        self.start = start or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    # ------------------------------------------------------------ سناریوها

    def _spec(self, name: str) -> _Spec:
        t0 = self.event_time_s

        def cruise(t):
            return 60.0

        def stops_after(t_stop):  # ۶۰ km/h تا t0، سپس توقف خطی تا t0 + t_stop
            return lambda t: 60.0 if t < t0 else max(0.0, 60.0 * (1 - (t - t0) / t_stop))

        def engine_cut(at):  # موتور روشن تا لحظه‌ی at، بعد خاموش
            return lambda t: 2300.0 if t < at else 0.0

        crash_shocks = [(t0, 0.08, 10.0), (t0 + 0.15, 0.05, 6.0)]
        crash_spin = [(t0, 0.5, 5.0)]

        if name == "normal_driving":
            return _Spec(cruise, _rpm_from_speed(cruise),
                         shocks=[(8.0, 0.05, 1.8), (t0 + 6, 0.05, 2.2)])
        if name == "hard_brake":
            speed = stops_after(3.0)
            return _Spec(speed, _rpm_from_speed(speed), quiet_after=t0 + 3.5,
                         braking=(t0, 3.0))
        if name == "pothole":
            return _Spec(cruise, _rpm_from_speed(cruise), shocks=[(t0, 0.06, 3.5)])
        if name == "phone_drop":
            return _Spec(
                cruise, _rpm_from_speed(cruise),
                shocks=[(t0, 0.05, 8.0), (t0 + 0.3, 0.04, 5.0), (t0 + 0.6, 0.04, 4.2)],
                bursts=[(t0, 0.8, 6.0)],
            )
        if name == "low_speed_bump":
            def slow(t):
                return 10.0
            return _Spec(slow, _rpm_from_speed(slow), shocks=[(t0, 0.05, 5.0)])
        if name == "crash":
            return _Spec(stops_after(1.5), engine_cut(t0 + 0.4), crash_shocks,
                         crash_spin, quiet_after=t0 + 2.0)
        if name == "crash_no_speed":  # فقط حسگر گوشی (OBD در دسترس نیست)
            return _Spec(stops_after(1.5), engine_cut(t0 + 0.4), crash_shocks,
                         crash_spin, quiet_after=t0 + 2.0,
                         speed_available=False, rpm_available=False)
        if name == "crash_engine_off":  # سرعت نامعلوم، ولی دور موتور هست
            return _Spec(stops_after(1.5), engine_cut(t0 + 0.4), crash_shocks,
                         crash_spin, quiet_after=t0 + 2.0, speed_available=False)
        if name == "crash_engine_running":  # موتور بعد از تصادف روشن می‌ماند
            speed = stops_after(1.5)
            return _Spec(speed, _rpm_from_speed(speed), crash_shocks, crash_spin,
                         quiet_after=t0 + 2.0)
        if name == "engine_stall":  # خاموشی موتور بدون هیچ ضربه‌ای
            def coast(t):
                return 60.0 if t < t0 else max(0.0, 60.0 - (t - t0))
            return _Spec(coast, engine_cut(t0 + 0.2))
        raise ValueError(f"سناریوی ناشناخته: {name!r}. گزینه‌ها: {SCENARIOS}")

    def vehicle_profile(self, name: str) -> tuple[Profile, Profile]:
        """(speed_kmh(t), rpm(t)) سناریو؛ t ثانیه از شروع شبیه‌سازی."""
        spec = self._spec(name)
        return spec.speed, spec.rpm

    def scenario(self, name: str) -> Iterator[SimStep]:
        spec = self._spec(name)
        rng = random.Random(self.seed)
        total = int(self.duration_s * self.rate_hz)
        per_tick = max(1, round(self.speed_interval_s * self.rate_hz))

        for n in range(total):
            t = n / self.rate_hz
            quiet = spec.quiet_after is not None and t >= spec.quiet_after
            sigma = 0.05 if quiet else 0.3

            shock_g = max((m for s, d, m in spec.shocks if s <= t < s + d), default=None)
            if shock_g is not None:
                ax, ay, az = (c * shock_g * G for c in _SHOCK_DIRECTION)
            else:
                ax, ay, az = _GRAVITY_VECTOR
                if spec.braking and spec.braking[0] <= t < spec.braking[0] + spec.braking[1]:
                    ax += -0.8 * G  # کاهش سرعت ملایم (کمتر از ۱٫۵ g)
            ax, ay, az = (v + rng.gauss(0, sigma) for v in (ax, ay, az))

            burst = max((m for s, d, m in spec.bursts if s <= t < s + d), default=0.0)
            gx = burst * 0.6 + rng.gauss(0, 0.03)
            gy = burst * 0.8 + rng.gauss(0, 0.03)
            gz = rng.gauss(0, 0.03)

            sample = MotionSample(
                timestamp=self.start + timedelta(seconds=t),
                accel_x=ax, accel_y=ay, accel_z=az,
                gyro_x=gx, gyro_y=gy, gyro_z=gz,
            )
            tick = n % per_tick == 0
            yield SimStep(
                sample=sample,
                speed_kmh=spec.speed(t) if tick and spec.speed_available else None,
                rpm=spec.rpm(t) if tick and spec.rpm_available else None,
            )
