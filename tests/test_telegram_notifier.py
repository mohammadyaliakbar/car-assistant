import threading
import time
from datetime import datetime, timezone
from unittest import mock

import httpx

from app.detectors.event_detector import DetectionEvent, Severity
from app.notifications.telegram_notifier import TelegramNotifier

URL = "https://api.telegram.org/botTOKEN/sendMessage"
FAST = (0.01, 0.01)


def make_event(
    severity=Severity.WARNING,
    event_type="coolant_overheat",
    detector="test",
):
    return DetectionEvent(
        detector=detector,
        event_type=event_type,
        severity=severity,
        message="test message",
        vehicle_id="car_a",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def make_notifier(attempt, **kwargs):
    """attempt(text) باید "ok" / "retry" / "fail" برگرداند."""
    kwargs.setdefault("retry_delays", FAST)
    n = TelegramNotifier("TOKEN", "123", **kwargs)
    n._attempt = attempt  # جایگزینی ارسال واقعی
    return n


def recorder(outcomes):
    """تابع تلاش که نتیجه‌ها را به ترتیب می‌دهد و تعداد فراخوانی را ثبت می‌کند."""
    calls = []
    it = iter(outcomes)

    def attempt(text):
        calls.append(text)
        return next(it)

    attempt.calls = calls
    return attempt


def always(outcome):
    calls = []

    def attempt(text):
        calls.append(text)
        return outcome

    attempt.calls = calls
    return attempt


# ----------------------------------------------------------- صف و cooldown


def test_info_events_are_ignored():
    a = always("ok")
    n = make_notifier(a)
    assert n.notify_event(make_event(Severity.INFO)) is False
    assert n.flush(1)
    assert a.calls == []


def test_warning_is_sent_in_background():
    a = always("ok")
    n = make_notifier(a)
    assert n.notify_event(make_event()) is True
    assert n.flush(2)
    assert len(a.calls) == 1
    assert "coolant_overheat" in a.calls[0]
    n.close()


def test_notify_event_does_not_block_on_slow_network():
    release = threading.Event()

    def slow(text):
        release.wait(5)
        return "ok"

    n = make_notifier(slow)
    start = time.monotonic()
    assert n.notify_event(make_event()) is True
    assert time.monotonic() - start < 0.5
    release.set()
    assert n.flush(2)
    n.close()


def test_cooldown_blocks_duplicate():
    a = always("ok")
    n = make_notifier(a, cooldown_seconds=300)
    assert n.notify_event(make_event()) is True
    assert n.notify_event(make_event()) is False
    assert n.flush(2)
    assert len(a.calls) == 1
    n.close()


def test_different_event_types_are_independent():
    a = always("ok")
    n = make_notifier(a)
    assert n.notify_event(make_event(event_type="a")) is True
    assert n.notify_event(make_event(event_type="b")) is True
    assert n.flush(2)
    assert len(a.calls) == 2
    n.close()


def test_failed_send_releases_cooldown():
    a = recorder(["retry", "ok"])
    n = make_notifier(a, cooldown_seconds=300)
    # WARNING معمولی: فقط یک تلاش
    assert n.notify_event(make_event()) is True
    assert n.flush(2)
    # ارسال اول شکست خورد، پس رویداد بعدی نباید در cooldown گیر کند
    assert n.notify_event(make_event()) is True
    assert n.flush(2)
    assert len(a.calls) == 2
    n.close()


def test_exception_in_send_does_not_kill_worker():
    calls = []

    def boom_then_ok(text):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return "ok"

    n = make_notifier(boom_then_ok)
    assert n.notify_event(make_event(event_type="a")) is True
    assert n.flush(2)
    assert n.notify_event(make_event(event_type="b")) is True
    assert n.flush(2)
    assert len(calls) == 2
    n.close()


def test_full_queue_drops_and_releases_cooldown():
    release = threading.Event()

    def blocked(text):
        release.wait(5)
        return "ok"

    n = make_notifier(blocked, max_queue_size=1)

    # اولی را thread برمی‌دارد و در ارسال می‌ماند، دومی در صف می‌نشیند
    assert n.notify_event(make_event(event_type="a")) is True
    time.sleep(0.1)
    assert n.notify_event(make_event(event_type="b")) is True
    # سومی جا ندارد
    assert n.notify_event(make_event(event_type="c")) is False
    assert "car_a:c:WARNING" not in n.last_sent

    release.set()
    assert n.flush(3)
    n.close()


def test_close_without_worker_is_safe():
    n = make_notifier(always("ok"))
    n.close()


# ----------------------------------------------------------- تلاش مجدد


def test_critical_is_retried_until_success():
    a = recorder(["retry", "retry", "ok"])
    n = make_notifier(a)
    assert n.notify_event(make_event(Severity.CRITICAL)) is True
    assert n.flush(2)
    assert len(a.calls) == 3
    # موفق شد، پس cooldown حفظ می‌شود
    assert "car_a:coolant_overheat:CRITICAL" in n.last_sent
    n.close()


def test_plain_warning_is_not_retried():
    a = always("retry")
    n = make_notifier(a)
    assert n.notify_event(make_event(Severity.WARNING)) is True
    assert n.flush(2)
    assert len(a.calls) == 1
    n.close()


def test_roadside_emergency_warning_is_retried():
    a = always("retry")
    n = make_notifier(a)
    ev = make_event(
        Severity.WARNING,
        event_type="roadside_emergency_initial",
        detector="roadside_emergency",
    )
    assert n.notify_event(ev) is True
    assert n.flush(2)
    assert len(a.calls) == 3  # ۱ تلاش اول + ۲ تلاش مجدد
    n.close()


def test_permanent_failure_is_not_retried():
    a = always("fail")
    n = make_notifier(a)
    assert n.notify_event(make_event(Severity.CRITICAL)) is True
    assert n.flush(2)
    assert len(a.calls) == 1
    n.close()


def test_exhausted_retries_release_cooldown():
    a = always("retry")
    n = make_notifier(a)
    assert n.notify_event(make_event(Severity.CRITICAL)) is True
    assert n.flush(2)
    assert len(a.calls) == 3
    assert "car_a:coolant_overheat:CRITICAL" not in n.last_sent
    n.close()


def test_close_interrupts_retry_wait():
    a = always("retry")
    n = make_notifier(a, retry_delays=(30.0,))
    assert n.notify_event(make_event(Severity.CRITICAL)) is True
    time.sleep(0.1)  # thread وارد انتظار بین تلاش‌ها شده است
    start = time.monotonic()
    n.close(timeout=0.2)
    assert time.monotonic() - start < 3
    assert len(a.calls) == 1


# ----------------------------------------------------------- _attempt واقعی


def _resp(status, json=None, content=None):
    request = httpx.Request("POST", URL)
    if content is not None:
        return httpx.Response(status, content=content, request=request)
    return httpx.Response(status, json=json, request=request)


def _attempt_with(response=None, error=None):
    n = TelegramNotifier("TOKEN", "123")
    with mock.patch.object(httpx, "post", return_value=response, side_effect=error):
        return n._attempt("hi")


def test_attempt_ok():
    assert _attempt_with(_resp(200, {"ok": True})) == "ok"


def test_attempt_api_rejection_is_permanent():
    assert _attempt_with(_resp(200, {"ok": False})) == "fail"


def test_attempt_server_error_is_retried():
    assert _attempt_with(_resp(500, {})) == "retry"


def test_attempt_rate_limit_is_retried():
    assert _attempt_with(_resp(429, {})) == "retry"


def test_attempt_client_error_is_permanent():
    assert _attempt_with(_resp(401, {})) == "fail"
    assert _attempt_with(_resp(400, {})) == "fail"


def test_attempt_network_error_is_retried():
    assert _attempt_with(error=httpx.ConnectError("down")) == "retry"


def test_attempt_invalid_json_is_retried():
    assert _attempt_with(_resp(200, content=b"not json")) == "retry"


def test_send_message_returns_bool():
    n = TelegramNotifier("TOKEN", "123")
    with mock.patch.object(httpx, "post", return_value=_resp(200, {"ok": True})):
        assert n.send_message("hi") is True
    with mock.patch.object(httpx, "post", return_value=_resp(500, {})):
        assert n.send_message("hi") is False
