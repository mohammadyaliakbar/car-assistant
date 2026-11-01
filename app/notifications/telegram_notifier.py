# app/notifications/telegram_notifier.py

import logging
import time
from html import escape

import httpx

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        cooldown_seconds: int = 300,
    ):
        self.token = token
        self.chat_id = chat_id
        self.cooldown_seconds = cooldown_seconds

        self.api_url = f"https://api.telegram.org/bot{token}/sendMessage"

        # زمان آخرین ارسال موفق هر نوع رویداد
        self.last_sent: dict[str, float] = {}

    def send_message(self, text: str) -> bool:
        """ارسال یک پیام متنی به تلگرام."""

        try:
            response = httpx.post(
                self.api_url,
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )

            response.raise_for_status()

            result = response.json()
            if not result.get("ok"):
                logger.error("Telegram API rejected the message: %s", result)
                return False

            return True

        except httpx.HTTPError:
            logger.exception("Failed to send Telegram message.")
            return False

    def notify_event(self, event) -> bool:
        """ارسال رویدادهای WARNING و CRITICAL به تلگرام."""

        severity = event.severity.value.upper()

        if severity not in {"WARNING", "CRITICAL"}:
            return False

        # کلید جلوگیری از ارسال تکراری یک هشدار
        event_key = (
            f"{event.vehicle_id}:"
            f"{event.event_type}:"
            f"{severity}"
        )

        now = time.monotonic()
        last_sent = self.last_sent.get(event_key)

        if (
            last_sent is not None
            and now - last_sent < self.cooldown_seconds
        ):
            logger.debug("Telegram notification skipped (cooldown): %s", event_key)
            return False

        emoji = "🚨" if severity == "CRITICAL" else "⚠️"

        message = (
            f"{emoji} <b>هشدار دستیار هوشمند خودرو</b>\n\n"
            f"<b>سطح:</b> {escape(severity)}\n"
            f"<b>خودرو:</b> {escape(str(event.vehicle_id))}\n"
            f"<b>نوع رویداد:</b> {escape(str(event.event_type))}\n"
            f"<b>پیام:</b> {escape(str(event.message))}\n"
            f"<b>زمان:</b> {escape(event.timestamp.isoformat())}"
        )

        if self.send_message(message):
            self.last_sent[event_key] = now
            logger.info("Telegram notification sent: %s", event_key)
            return True

        return False