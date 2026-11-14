# app/motion/crash_monitor.py
"""
حلقه‌ی مستقل تشخیص تصادف: منبع حرکتی → CrashDetector → CrashResponder.

عمداً جدا از حلقه‌ی OBD اجرا می‌شود: قطع‌شدن آداپتور OBD (که ممکن است خودش
نتیجه‌ی تصادف باشد) نباید تشخیص تصادف را از کار بیندازد.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.detectors.event_detector import DetectionEvent
from app.motion.crash_detector import CrashDetector
from app.motion.crash_response import CrashResponder
from app.motion.source import MotionSource

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CrashMonitor:
    def __init__(
        self,
        source: MotionSource,
        detector: CrashDetector,
        responder: CrashResponder,
        clock: Callable[[], datetime] = _utc_now,
        poll_interval_s: float = 0.05,
        idle_poll_after_s: float = 1.0,
    ):
        self.source = source
        self.detector = detector
        self.responder = responder
        self._clock = clock
        self.poll_interval_s = poll_interval_s
        self.idle_poll_after_s = idle_poll_after_s

        self._lock = threading.Lock()
        self._last_data_at: datetime | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def update_vehicle(
        self,
        timestamp: datetime,
        speed_kmh: float | None = None,
        rpm: float | None = None,
    ) -> None:
        """از حلقه‌ی OBD (thread دیگر) صدا زده می‌شود."""
        with self._lock:
            self.detector.update_vehicle(timestamp, speed_kmh, rpm)

    def step(self) -> list[DetectionEvent]:
        """یک دور پردازش (همگام و قابل‌آزمون)."""
        now = self._clock()
        events: list[DetectionEvent] = []
        with self._lock:
            for signal in self.source.read_vehicle_signals():
                self.detector.update_vehicle(
                    signal.timestamp, signal.speed_kmh, signal.rpm
                )
            samples = self.source.read_samples()
            if samples:
                self._last_data_at = now
            for sample in samples:
                events += self.detector.process(sample)

            # نمونه‌ای نمی‌رسد (مثلاً گوشی خراب شده)؛ مهلت را با ساعت واقعی ببند.
            # فقط در سکوت داده: اختلاف ساعت شبیه‌ساز نباید پنجره را زودتر ببندد.
            if self._last_data_at is not None and (
                now - self._last_data_at
            ) > timedelta(seconds=self.idle_poll_after_s):
                events += self.detector.poll(now)

        for event in events:
            try:
                self.responder.on_crash(event)
            except Exception:
                logger.exception("Crash responder failed.")
        try:
            self.responder.tick(now)
        except Exception:
            logger.exception("Crash responder tick failed.")
        return events

    # ------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="crash-monitor", daemon=True
        )
        self._thread.start()
        logger.info("Crash monitor started.")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("Crash monitor step failed.")
            self._stop.wait(self.poll_interval_s)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        try:
            self.source.close()
        except Exception:
            logger.exception("Closing motion source failed.")
