# app/simulation/driver.py
"""راننده‌ی شبیه‌سازی‌شده: بعد از پرسش تصادف، طبق رفتار تعیین‌شده پاسخ می‌دهد."""

import logging
import threading

from app.motion.crash_response import ConfirmationRequest, CrashResponder
from app.simulation.options import DriverBehavior

logger = logging.getLogger(__name__)


class SimulatedDriver:
    def __init__(self, behavior: DriverBehavior):
        self.behavior = behavior
        self._responder: CrashResponder | None = None
        self._timers: list[threading.Timer] = []

    def attach(self, responder: CrashResponder) -> None:
        self._responder = responder

    def on_prompt(self, request: ConfirmationRequest) -> None:
        """به‌عنوان یکی از prompt‌های CrashResponder ثبت می‌شود."""
        if self.behavior.action == "none" or self._responder is None:
            logger.info("Simulated driver: no response.")
            return
        confirmed = self.behavior.action == "confirm"
        logger.info(
            "Simulated driver will %s in %.1fs.",
            "confirm" if confirmed else "decline",
            self.behavior.delay_s,
        )
        timer = threading.Timer(
            self.behavior.delay_s,
            self._answer,
            args=(confirmed, request.id),
        )
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def _answer(self, confirmed: bool, request_id: str) -> None:
        if self._responder is not None:
            self._responder.answer(confirmed, request_id=request_id)

    def close(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
