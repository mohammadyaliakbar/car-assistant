# app/motion/source.py
"""
منبع داده‌ی حرکتی: برنامه فقط به واسط `MotionSource` وابسته است و فرقی
نمی‌کند داده از گوشی بیاید یا از شبیه‌ساز.

پیاده‌سازی‌ها:
- `QueueMotionSource`: دریافت فشاری (push). برای گوشی (اندروید/Chaquopy) و
  برای هر شبیه‌ساز بیرونی؛ هر کدام پیام‌هایی با «قالب پیام» پایین می‌فرستند.
- `SimulatedMotionSource`: شبیه‌ساز درون‌برنامه‌ای (زمان واقعی).

قالب پیام (JSON یا dict) — قرارداد مشترک گوشی و شبیه‌ساز:

    حسگر حرکتی:  {"t": 1767268800123,
                  "accel": [ax, ay, az],        # m/s²، دستگاه مختصات گوشی، با گرانش
                  "gyro":  [gx, gy, gz]}         # rad/s؛ اختیاری (null یا حذف)
    سرعت/موتور:  {"t": 1767268800500, "speed_kmh": 62.0, "rpm": 2300}
                  # هر دو اختیاری ولی دست‌کم یکی؛ سرعت می‌تواند از GPS گوشی یا OBD باشد

- `t`: زمان UTC به میلی‌ثانیه‌ی epoch (نه ثانیه، نه nanosecond و نه زمان
  بوت). در اندروید `event.timestamp` از نوع elapsedRealtimeNanos است؛ طرف
  Java باید قبل از ارسال آن را به epoch میلی‌ثانیه تبدیل کند، مثلاً
  `System.currentTimeMillis() + (event.timestamp - SystemClock.elapsedRealtimeNanos()) / 1_000_000`.
- مقدار نامعلوم را حذف کنید یا null بفرستید؛ هرگز 0 نفرستید (0 یعنی «ایستاده»
  یا «چرخش صفر»، نه «نامعلوم»).
"""

import json
import logging
import math
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.motion.models import MotionSample
from app.motion.simulator import MotionSimulator

logger = logging.getLogger(__name__)

# بازه‌ی معقول epoch میلی‌ثانیه (حدود ۱۹۷۳ تا ۵۱۳۸)؛ ثانیه یا نانوثانیه رد می‌شود
_MIN_EPOCH_MS = 1e11
_MAX_EPOCH_MS = 1e14


@dataclass(frozen=True)
class VehicleSignal:
    """سرعت و/یا دور موتور در یک لحظه. مقدار نامعلوم None است."""

    timestamp: datetime
    speed_kmh: float | None = None
    rpm: float | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp باید timezone-aware باشد.")
        if self.speed_kmh is None and self.rpm is None:
            raise ValueError("دست‌کم یکی از speed_kmh و rpm لازم است.")
        for name in ("speed_kmh", "rpm"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} باید عدد متناهی و نامنفی باشد.")


class MotionSource(Protocol):
    def read_samples(self) -> list[MotionSample]:
        """نمونه‌های حسگر رسیده از آخرین فراخوانی (بدون انتظار)."""

    def read_vehicle_signals(self) -> list[VehicleSignal]:
        """سرعت/دور موتور رسیده از آخرین فراخوانی (بدون انتظار)."""

    def close(self) -> None:
        """آزادسازی منابع."""


# ---------------------------------------------------------------- تبدیل پیام

def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} باید عدد باشد.")
    return float(value)


def _optional_number(value: Any, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _vector(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} باید فهرستی سه‌عضوی باشد.")
    return tuple(_number(v, name) for v in value)  # type: ignore[return-value]


def _timestamp(value: Any) -> datetime:
    t = _number(value, "t")
    if not math.isfinite(t) or not _MIN_EPOCH_MS <= t <= _MAX_EPOCH_MS:
        raise ValueError(
            "t باید epoch به «میلی‌ثانیه» باشد (مقدار شبیه ثانیه، نانوثانیه "
            "یا زمان بوت است)."
        )
    return datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)


def parse_message(message: dict | str | bytes) -> MotionSample | VehicleSignal:
    """پیام قرارداد را به MotionSample یا VehicleSignal تبدیل می‌کند؛ ValueError در خطا."""
    if isinstance(message, (str, bytes)):
        try:
            message = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON نامعتبر.") from exc
    if not isinstance(message, dict):
        raise ValueError("پیام باید یک شیء JSON باشد.")

    ts = _timestamp(message.get("t"))

    if "accel" in message:
        ax, ay, az = _vector(message["accel"], "accel")
        gyro = message.get("gyro")
        gx, gy, gz = (None, None, None) if gyro is None else _vector(gyro, "gyro")
        return MotionSample(ts, ax, ay, az, gx, gy, gz)

    if "speed_kmh" in message or "rpm" in message:
        return VehicleSignal(
            ts,
            speed_kmh=_optional_number(message.get("speed_kmh"), "speed_kmh"),
            rpm=_optional_number(message.get("rpm"), "rpm"),
        )

    raise ValueError("پیام نه حسگر حرکتی است و نه سرعت/موتور.")


# ---------------------------------------------------------------- منبع فشاری

class QueueMotionSource:
    """
    صف ایمن برای thread. گوشی یا شبیه‌ساز بیرونی داده را `push*` می‌کند و
    برنامه با `read_*` تخلیه می‌کند. هیچ‌یک از متدهای push استثنا نمی‌دهند.
    """

    def __init__(self, max_samples: int = 5000, max_signals: int = 500):
        self._lock = threading.Lock()
        self._samples: deque[MotionSample] = deque(maxlen=max_samples)
        self._signals: deque[VehicleSignal] = deque(maxlen=max_signals)
        self.dropped = 0   # قدیمی‌ترین‌هایی که با پر شدن صف دور ریخته شدند
        self.rejected = 0  # پیام‌های نامعتبر

    def push(self, item: MotionSample | VehicleSignal) -> None:
        with self._lock:
            queue = self._samples if isinstance(item, MotionSample) else self._signals
            if queue.maxlen is not None and len(queue) == queue.maxlen:
                self.dropped += 1
            queue.append(item)

    def push_message(self, message: dict | str | bytes) -> bool:
        """True اگر پذیرفته شد. پیام نامعتبر شمارش و لاگ می‌شود، استثنا نمی‌دهد."""
        try:
            item = parse_message(message)
        except ValueError as exc:
            self._reject(exc)
            return False
        self.push(item)
        return True

    def push_sensor(
        self,
        epoch_ms: float,
        ax: float, ay: float, az: float,
        gx: float | None = None, gy: float | None = None, gz: float | None = None,
    ) -> bool:
        """میان‌بر برای Java/Chaquopy که مقدارهای ساده می‌فرستد."""
        gyro = None if gx is None else [gx, gy, gz]
        return self.push_message({"t": epoch_ms, "accel": [ax, ay, az], "gyro": gyro})

    def push_vehicle(
        self,
        epoch_ms: float,
        speed_kmh: float | None = None,
        rpm: float | None = None,
    ) -> bool:
        return self.push_message(
            {"t": epoch_ms, "speed_kmh": speed_kmh, "rpm": rpm}
        )

    def _reject(self, exc: Exception) -> None:
        with self._lock:
            self.rejected += 1
            count = self.rejected
        if count == 1 or count % 100 == 0:
            logger.warning("Motion message rejected (%d so far): %s", count, exc)

    def read_samples(self) -> list[MotionSample]:
        with self._lock:
            items = list(self._samples)
            self._samples.clear()
        return items

    def read_vehicle_signals(self) -> list[VehicleSignal]:
        with self._lock:
            items = list(self._signals)
            self._signals.clear()
        return items

    def close(self) -> None:
        with self._lock:
            self._samples.clear()
            self._signals.clear()


# ---------------------------------------------------------------- شبیه‌ساز

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SimulatedMotionSource:
    """
    شبیه‌ساز درون‌برنامه‌ای در زمان واقعی: نمونه‌ها فقط وقتی تحویل می‌شوند که
    زمانشان رسیده باشد، و زمان‌نگاری‌ها به ساعت دیواری لنگر شده‌اند (مثل گوشی).
    """

    def __init__(
        self,
        scenario: str,
        clock: Callable[[], datetime] = _utc_now,
        start: datetime | None = None,
        emit_vehicle_signals: bool = True,
        **simulator_kwargs: Any,
    ):
        """
        start: لحظه‌ی صفر سناریو (پیش‌فرض: اولین خواندن). برای هماهنگی با
               OBD و GPS شبیه‌سازی‌شده همان لحظه‌ی دنیای شبیه‌سازی داده شود.
        emit_vehicle_signals: اگر False باشد سرعت/دور موتور تحویل داده نمی‌شود
               (در حالت یکپارچه، آن‌ها از مسیر OBD می‌رسند).
        """
        self.scenario = scenario
        self._clock = clock
        self._start = start
        self._emit_vehicle_signals = emit_vehicle_signals
        self._simulator_kwargs = simulator_kwargs
        self._steps = None
        self._next = None
        self._samples: list[MotionSample] = []
        self._signals: list[VehicleSignal] = []
        self.finished = False

    def _advance(self) -> None:
        now = self._clock()
        if self._steps is None:
            sim = MotionSimulator(start=self._start or now, **self._simulator_kwargs)
            self._steps = sim.scenario(self.scenario)  # سناریوی ناشناخته همین‌جا ValueError می‌دهد
            self._next = next(self._steps, None)

        while self._next is not None and self._next.sample.timestamp <= now:
            step = self._next
            self._samples.append(step.sample)
            if self._emit_vehicle_signals and (
                step.speed_kmh is not None or step.rpm is not None
            ):
                self._signals.append(
                    VehicleSignal(step.sample.timestamp, step.speed_kmh, step.rpm)
                )
            self._next = next(self._steps, None)
        self.finished = self._next is None

    def read_samples(self) -> list[MotionSample]:
        self._advance()
        items, self._samples = self._samples, []
        return items

    def read_vehicle_signals(self) -> list[VehicleSignal]:
        self._advance()
        items, self._signals = self._signals, []
        return items

    def close(self) -> None:
        self._steps = None
        self._next = None
        self._samples = []
        self._signals = []
        self.finished = True
