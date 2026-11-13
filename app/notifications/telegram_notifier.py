# app/notifications/telegram_notifier.py

import logging
import queue
import threading
import time
from html import escape

import httpx

logger = logging.getLogger(__name__)

# علامت پایان برای thread پس‌زمینه
_STOP = object()

# نتیجه‌ی یک تلاش ارسال
_OK = "ok"        # پیام رسید
_RETRY = "retry"  # خطای موقتی (شبکه، timeout، 429، 5xx): ارزش تلاش مجدد دارد
_FAIL = "fail"    # خطای دائمی (مثلاً توکن اشتباه یا پیام نامعتبر): تلاش مجدد بی‌فایده است

# رویدادهای این detector همیشه اولویت‌دار هستند (حتی با severity برابر WARNING)
_PRIORITY_DETECTORS = {"roadside_emergency"}


class TelegramNotifier:
    """
    ارسال هشدارها به تلگرام از طریق یک صف و thread پس‌زمینه.

    `notify_event()` هرگز منتظر شبکه نمی‌ماند: پیام را در صف می‌گذارد و
    برمی‌گردد. مقدار True یعنی «در صف قرار گرفت»، نه «به تلگرام رسید».

    پیام‌های اولویت‌دار (CRITICAL و رویدادهای roadside_emergency) در صورت
    خطای موقتی چند بار دوباره تلاش می‌شوند؛ بقیه فقط یک بار.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        cooldown_seconds: int = 300,
        max_queue_size: int = 50,
        request_timeout: float = 10,
        retry_delays: tuple[float, ...] = (2.0, 5.0),
    ):
        self.token = token
        self.chat_id = chat_id
        self.cooldown_seconds = cooldown_seconds
        self.request_timeout = request_timeout
        # فاصله‌ی بین تلاش‌ها؛ تعداد کل تلاش‌ها = len(retry_delays) + 1
        self.retry_delays = retry_delays

        self.api_url = f"https://api.telegram.org/bot{token}/sendMessage"

        # زمان آخرین ارسال (یا رزرو در صف) هر نوع رویداد
        self.last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._worker: threading.Thread | None = None
        self._closing = threading.Event()

    # ------------------------------------------------------------ ارسال

    def _attempt(self, text: str) -> str:
        """
        یک تلاش ارسال. یکی از _OK / _RETRY / _FAIL را برمی‌گرداند.

        عمداً متن استثنا لاگ نمی‌شود، چون پیام خطای httpx شامل URL
        (و در نتیجه توکن بات) است.
        """

        try:
            response = httpx.post(
                self.api_url,
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=self.request_timeout,
            )
            response.raise_for_status()

            result = response.json()
            if not result.get("ok"):
                logger.error("Telegram API rejected the message: %s", result)
                return _FAIL

            return _OK

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Telegram HTTP error: status %s", status)
            if status == 429 or status >= 500:
                return _RETRY
            return _FAIL

        except httpx.HTTPError as exc:
            # timeout، قطع اتصال و ...
            logger.warning("Telegram network error: %s", type(exc).__name__)
            return _RETRY

        except ValueError:
            # پاسخ JSON معتبر نبود
            logger.warning("Telegram returned an invalid JSON response.")
            return _RETRY

    def send_message(self, text: str) -> bool:
        """ارسال همگام یک پیام، بدون تلاش مجدد. True فقط در صورت موفقیت."""

        return self._attempt(text) == _OK

    def _deliver(self, event_key: str, text: str, max_attempts: int) -> bool:
        """ارسال با تلاش مجدد (در thread پس‌زمینه)."""

        for attempt in range(1, max_attempts + 1):
            try:
                outcome = self._attempt(text)
            except Exception:
                # thread هرگز نباید بمیرد
                logger.exception("Unexpected error while sending Telegram message.")
                outcome = _RETRY

            if outcome == _OK:
                return True
            if outcome == _FAIL:
                return False

            if attempt < max_attempts:
                delays = self.retry_delays
                delay = delays[min(attempt - 1, len(delays) - 1)] if delays else 0
                logger.warning(
                    "Telegram send failed (%s), retry %d/%d in %.1fs",
                    event_key,
                    attempt,
                    max_attempts - 1,
                    delay,
                )
                # اگر برنامه در حال بسته‌شدن است، انتظار را قطع می‌کنیم
                if self._closing.wait(delay):
                    return False

        return False

    # ------------------------------------------------------------ thread

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run,
                name="telegram-notifier",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return

                event_key, text, max_attempts = item
                ok = self._deliver(event_key, text, max_attempts)

                if ok:
                    logger.info("Telegram notification sent: %s", event_key)
                else:
                    # همه‌ی تلاش‌ها شکست خورد: رزرو cooldown را برمی‌داریم
                    # تا رویداد بعدیِ همین نوع بتواند دوباره تلاش کند.
                    with self._lock:
                        self.last_sent.pop(event_key, None)
                    logger.error("Telegram notification failed: %s", event_key)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 15.0) -> bool:
        """
        منتظر خالی‌شدن صف می‌ماند. True یعنی همه‌ی پیام‌ها پردازش شدند.
        برای تست و بستن برنامه.
        """

        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout: float = 15.0) -> None:
        """پیام‌های باقی‌مانده را (تا timeout) می‌فرستد و thread را می‌بندد."""

        worker = self._worker
        if worker is None or not worker.is_alive():
            return

        self.flush(timeout)
        # اگر هنوز چیزی مانده، انتظارهای بین تلاش‌ها را قطع می‌کنیم
        self._closing.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            return
        worker.join(timeout=1.0)

    # ------------------------------------------------------------ رویداد

    @staticmethod
    def _is_priority(event, severity: str) -> bool:
        return (
            severity == "CRITICAL"
            or getattr(event, "detector", None) in _PRIORITY_DETECTORS
        )

    def notify_event(self, event) -> bool:
        """
        رویدادهای WARNING و CRITICAL را در صف ارسال تلگرام می‌گذارد.
        True یعنی در صف قرار گرفت.
        """

        severity = event.severity.value.upper()

        if severity not in {"WARNING", "CRITICAL"}:
            return False

        # کلید جلوگیری از ارسال تکراری یک هشدار
        event_key = (
            f"{event.vehicle_id}:"
            f"{event.event_type}:"
            f"{severity}"
        )

        emoji = "🚨" if severity == "CRITICAL" else "⚠️"

        message = (
            f"{emoji} <b>هشدار دستیار هوشمند خودرو</b>\n\n"
            f"<b>سطح:</b> {escape(severity)}\n"
            f"<b>خودرو:</b> {escape(str(event.vehicle_id))}\n"
            f"<b>نوع رویداد:</b> {escape(str(event.event_type))}\n"
            f"<b>پیام:</b> {escape(str(event.message))}\n"
            f"<b>زمان:</b> {escape(event.timestamp.isoformat())}"
        )

        max_attempts = (
            len(self.retry_delays) + 1
            if self._is_priority(event, severity)
            else 1
        )

        now = time.monotonic()

        with self._lock:
            last_sent = self.last_sent.get(event_key)

            if (
                last_sent is not None
                and now - last_sent < self.cooldown_seconds
            ):
                logger.debug("Telegram notification skipped (cooldown): %s", event_key)
                return False

            # رزرو cooldown هنگام ورود به صف، تا تا لحظه‌ی ارسال
            # نسخه‌ی تکراری وارد صف نشود.
            self.last_sent[event_key] = now

        try:
            self._queue.put_nowait((event_key, message, max_attempts))
        except queue.Full:
            with self._lock:
                self.last_sent.pop(event_key, None)
            logger.warning("Telegram queue is full; dropped: %s", event_key)
            return False

        self._ensure_worker()
        return True
