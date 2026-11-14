# app/motion/console_confirmation.py
"""
تأیید راننده از طریق کنسول (برای توسعه، تا اپ موبایل ساخته شود).

`console_prompt` پرسش را چاپ می‌کند و `ConsoleAnswerListener` در thread پس‌زمینه
از ورودی می‌خواند: y/yes/بله = تأیید، n/no/خیر = رد.
"""

import logging
import sys
import threading
from typing import TextIO

from app.motion.crash_response import ConfirmationRequest, CrashResponder

logger = logging.getLogger(__name__)

YES = frozenset({"y", "yes", "بله", "آره", "ب"})
NO = frozenset({"n", "no", "نه", "خیر"})


def make_console_prompt(timeout_s: float, on_timeout: str, out: TextIO | None = None):
    def prompt(request: ConfirmationRequest) -> None:
        stream = out or sys.stderr
        outcome = (
            "پیام به‌صورت خودکار ارسال می‌شود"
            if on_timeout == "send"
            else "پیامی ارسال نمی‌شود"
        )
        print(
            "\n🚨 تصادف احتمالی تشخیص داده شد "
            f"(اطمینان: {request.event.details.get('confidence', '?')}).\n"
            f"   ارسال هشدار به تلگرام؟  [y/n]  — اگر تا {timeout_s:.0f} ثانیه "
            f"پاسخ ندهید، {outcome}.",
            file=stream,
            flush=True,
        )

    return prompt


def parse_answer(line: str) -> bool | None:
    word = line.strip().lower()
    if word in YES:
        return True
    if word in NO:
        return False
    return None


class ConsoleAnswerListener:
    def __init__(self, responder: CrashResponder, stream: TextIO | None = None):
        self.responder = responder
        self._stream = stream or sys.stdin
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="console-confirmation", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            for line in self._stream:
                answer = parse_answer(line)
                if answer is None:
                    continue
                if not self.responder.answer(answer):
                    logger.info("پاسخ راننده به درخواستی مربوط نشد (درخواست فعالی نیست).")
        except (OSError, ValueError):
            logger.debug("Console confirmation listener stopped.", exc_info=True)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
