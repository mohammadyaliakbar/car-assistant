# app/motion/crash_response.py
"""
واکنش به `crash_suspected`:

۱. سرویس اضطراری بلافاصله فعال می‌شود (راهنمای ایمنی برای راننده).
۲. از راننده تأیید گرفته می‌شود (`prompt`).
۳. پیام تلگرام فقط بعد از تأیید راننده ارسال می‌شود. تا آن موقع، پیام‌های
   مرتبط با تصادف (خود رویداد و پاسخ‌های سرویس اضطراری) نگه داشته می‌شوند
   و بعد از تأیید یک‌جا فرستاده می‌شوند.

اگر پاسخی نیامد (`timeout_s`، پیش‌فرض ۶۰ ثانیه):
- on_timeout="send" (پیش‌فرض): ارسال می‌شود (راننده ممکن است قادر به پاسخ
  نباشد).
- on_timeout="skip": ارسال نمی‌شود. تأیید دیرهنگام راننده (تا
  `late_confirm_window_s`) همچنان معتبر است.
هر دو مقدار از برنامه (تنظیمات، خط فرمان) یا شبیه‌ساز قابل تغییرند.
"""

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.detectors.event_detector import DetectionEvent

logger = logging.getLogger(__name__)

TIMEOUT_POLICIES = ("send", "skip")
DEFAULT_CONFIRM_TIMEOUT_S = 60.0
DEFAULT_ON_TIMEOUT = "send"

_WAITING, _ALLOWED, _BLOCKED = "waiting", "allowed", "blocked"


@dataclass(frozen=True)
class ConfirmationRequest:
    id: str
    event: DetectionEvent
    created_at: datetime
    expires_at: datetime


def log_prompt(request: ConfirmationRequest) -> None:
    """پرسش پیش‌فرض: فقط لاگ. رابط کاربری واقعی (اپ) جایگزینش می‌شود."""
    logger.warning(
        "تصادف احتمالی تشخیص داده شد. برای ارسال هشدار به تلگرام، تأیید "
        "راننده لازم است (درخواست %s).",
        request.id,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CrashResponder:
    def __init__(
        self,
        emergency_handler: Callable[[DetectionEvent], object],
        send_alert: Callable[[DetectionEvent], object],
        prompt: Callable[[ConfirmationRequest], object] = log_prompt,
        timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
        on_timeout: str = DEFAULT_ON_TIMEOUT,
        late_confirm_window_s: float = 600.0,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if on_timeout not in TIMEOUT_POLICIES:
            raise ValueError(f"on_timeout باید یکی از {TIMEOUT_POLICIES} باشد.")
        if not timeout_s > 0:
            raise ValueError("timeout_s باید مثبت باشد.")
        self._emergency = emergency_handler
        self._send = send_alert
        self._prompt = prompt
        self.timeout_s = timeout_s
        self.on_timeout = on_timeout
        self.late_confirm_window_s = late_confirm_window_s
        self._clock = clock

        self._lock = threading.Lock()
        self._state = _WAITING
        self._held: dict[str, DetectionEvent] = {}
        self._pending: dict[str, ConfirmationRequest] = {}
        self._expired: dict[str, tuple[ConfirmationRequest, datetime]] = {}

    # ------------------------------------------------------------ ورودی‌ها

    def on_crash(self, event: DetectionEvent) -> ConfirmationRequest:
        """رویداد تازه‌ی تصادف: حادثه‌ی جدید شروع می‌شود."""
        now = self._clock()
        request = ConfirmationRequest(
            id=uuid.uuid4().hex[:8],
            event=event,
            created_at=now,
            expires_at=now + timedelta(seconds=self.timeout_s),
        )
        with self._lock:
            # حادثه‌ی جدید، حادثه‌ی قبلی را کنار می‌گذارد
            self._state = _WAITING
            self._held = {event.event_type: event}
            self._pending = {request.id: request}
            self._expired = {}

        self._safe(self._prompt, request, "prompt")
        self._safe(self._emergency, event, "emergency handler")
        return request

    def submit_alert(self, event: DetectionEvent) -> None:
        """
        پیام تلگرامیِ مرتبط با تصادف (مثلاً پاسخ سرویس اضطراری). بعد از تأیید
        فوراً فرستاده می‌شود؛ قبل از آن نگه داشته می‌شود؛ بعد از رد راننده
        دور ریخته می‌شود.
        """
        with self._lock:
            state = self._state
            if state == _WAITING:
                self._held[event.event_type] = event  # آخرین پیام هر نوع
        if state == _ALLOWED:
            self._safe(self._send, event, "alert sender")
        elif state == _BLOCKED:
            logger.info("Crash alert dropped (driver declined): %s", event.event_type)

    def answer(self, confirmed: bool, request_id: str | None = None) -> bool:
        """
        پاسخ راننده. True اگر به درخواستی (فعال یا تازه‌منقضی) مربوط شد.
        بدون request_id، تازه‌ترین درخواست در نظر گرفته می‌شود.
        """
        to_send: list[DetectionEvent] = []
        with self._lock:
            request = self._resolve(request_id)
            if request is None:
                return False
            self._pending.pop(request.id, None)
            self._expired.pop(request.id, None)
            if confirmed:
                self._state = _ALLOWED
                to_send = list(self._held.values())
            else:
                self._state = _BLOCKED
            self._held.clear()

        if confirmed:
            logger.warning("Driver confirmed crash alert; sending %d message(s).", len(to_send))
        else:
            logger.info("Driver declined crash alert; nothing will be sent.")
        for event in to_send:
            self._safe(self._send, event, "alert sender")
        return True

    def tick(self, now: datetime | None = None) -> None:
        """منقضی‌کردن درخواست‌های بی‌پاسخ؛ دوره‌ای صدا زده شود."""
        now = now or self._clock()
        to_send: list[DetectionEvent] = []
        with self._lock:
            timed_out = [r for r in self._pending.values() if now >= r.expires_at]
            for request in timed_out:
                del self._pending[request.id]
                self._expired[request.id] = (request, now)
            window = timedelta(seconds=self.late_confirm_window_s)
            self._expired = {
                k: v for k, v in self._expired.items() if now - v[1] <= window
            }
            if timed_out and self.on_timeout == "send" and self._state == _WAITING:
                self._state = _ALLOWED
                to_send = list(self._held.values())
                self._held.clear()

        for request in timed_out:
            if self.on_timeout == "send":
                logger.warning("No driver response (%s); sending alert per policy.", request.id)
            else:
                logger.warning("No driver response (%s); alert not sent.", request.id)
        for event in to_send:
            self._safe(self._send, event, "alert sender")

    # ------------------------------------------------------------ کمکی

    @property
    def pending_requests(self) -> list[ConfirmationRequest]:
        with self._lock:
            return list(self._pending.values())

    def _resolve(self, request_id: str | None) -> ConfirmationRequest | None:
        """باید زیر قفل صدا زده شود."""
        if request_id is not None:
            if request_id in self._pending:
                return self._pending[request_id]
            if request_id in self._expired:
                return self._expired[request_id][0]
            return None
        if self._pending:
            return list(self._pending.values())[-1]
        if self._expired:
            return list(self._expired.values())[-1][0]
        return None

    @staticmethod
    def _safe(fn: Callable, arg, what: str) -> None:
        try:
            fn(arg)
        except Exception:
            logger.exception("Crash responder: %s failed.", what)
