"""تست‌های سناریوی نهم: تشخیص تصادف (حسگر گوشی + OBD + تأیید راننده)."""

import threading
import time
from datetime import datetime, timezone

import pytest

from app.motion import CrashConfig, CrashDetector
from app.simulation.lab_engine import SimulationEngine
from app.simulation.lab_history import HistoryStore
from app.simulation.lab_server import LabApp, make_server
from app.simulation.scenarios import LAB_SCENARIOS

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run(**kw):
    e = SimulationEngine("crash_detection", start_time=T0, **kw)
    e.run_to_end()
    return e


def crash_event(e):
    return next(x for x in e.events if x["detector"] == "crash")


# ------------------------------------------------------------ تشخیص

def test_crash_is_detected_by_real_detectors_with_full_evidence():
    e = run()
    s = e.summary()
    assert s["verdict"] == "detected_by_detector" and s["false_alarms"] == 0
    ev = crash_event(e)
    assert ev["event_type"] == "crash_suspected" and ev["severity"] == "critical"
    d = ev["details"]
    assert d["confidence"] == "high" and d["peak_g"] >= 8.0
    assert d["speed_drop"] is True and d["spin"] is True and d["still"] is True
    assert d["engine_off"] is True and d["speed_before_kmh"] > 80 and d["speed_after_kmh"] == 0
    # OBD هم هم‌زمان خاموشی در حال حرکت را دید (قرینه‌ی مستقل)
    assert [x["event_type"] for x in e.events] == [
        "engine_stalled_while_moving", "crash_suspected"]


def test_detection_latency_reflects_the_detectors_confirm_window():
    e = run()
    lat = {r["event_type"]: r["detection_latency_s"] for r in e.summary()["expected"]}
    assert lat["engine_stalled_while_moving"] == 1.0
    assert lat["crash_suspected"] == CrashConfig().confirm_window_s    # ۸ ثانیه، ذاتی Detector
    assert not any(f["events"] for f in e.frames[: int(e.event_time_s)])   # پیش از ضربه هیچ رویدادی نیست


def test_detector_is_real_and_thresholds_untouched(monkeypatch):
    calls = []
    original = CrashDetector.process

    def spy(self, sample):
        calls.append(sample)
        return original(self, sample)

    monkeypatch.setattr(CrashDetector, "process", spy)
    e = run()
    assert len(calls) == 50 * len(e.frames)                 # ۵۰ نمونه‌ی حسگر در هر ثانیه
    assert e.crash_detector.config == CrashConfig()          # آستانه‌های پیش‌فرض پروژه
    assert (CrashConfig().impact_g, CrashConfig().confirm_window_s) == (4.0, 8.0)


def test_obd_side_follows_crash_physics_and_gps_freezes():
    e = run()
    t0 = int(e.event_time_s)
    assert e.frames[t0]["obd"]["speed"] > 80 and e.frames[t0]["obd"]["rpm"] > 2000   # هنوز پیش از ضربه
    assert e.frames[t0 + 1]["obd"]["rpm"] == 0 and 0 < e.frames[t0 + 1]["obd"]["speed"] < 40
    assert e.frames[t0 + 2]["obd"]["speed"] == 0
    tail = {(f["gps"]["latitude"], f["gps"]["longitude"]) for f in e.frames[t0 + 2:]}
    assert len(tail) == 1
    peaks = [f["motion"]["peak_g"] for f in e.frames]
    assert max(peaks) >= 9 and peaks[t0 - 1] < 4          # ضربه فقط در ثانیه‌ی رویداد


def test_emergency_assistant_runs_for_crash_event():
    e = run()
    crash = [r for r in e.emergency_log if r["event_type"] == "crash_suspected"]
    assert [r["phase"] for r in crash] == ["initial", "final"]
    assert crash[1]["assessment"]["level"] == "critical"
    assert crash[0]["generation_ms"] >= 0


def test_other_scenarios_have_no_phone_sensors_and_no_crash():
    for name, spec in LAB_SCENARIOS.items():
        if name == "crash_detection":
            continue
        assert not spec.phone_sensors
        e = SimulationEngine(name, start_time=T0)
        e.run_to_end()
        assert e.frames[5]["motion"] is None and e.frames[5]["crash_gate"] is None
        assert all(x["detector"] != "crash" for x in e.events)


def test_crash_run_is_repeatable():
    a, b = run(), run()
    assert [f["obd"] for f in a.frames] == [f["obd"] for f in b.frames]
    assert [f["motion"] for f in a.frames] == [f["motion"] for f in b.frames]
    assert a.events == b.events


# ------------------------------------------------------------ دروازه‌ی تأیید راننده

def test_telegram_message_is_held_until_driver_confirms():
    sent = []
    e = SimulationEngine("crash_detection", start_time=T0, crash_alert_sink=sent.append)
    while e.t < 60:                                   # بعد از رویداد (۵۳) و پیش از هر پاسخی
        e.step()
    gate = e.gate_snapshot()
    assert gate["state"] == "waiting" and gate["seconds_left"] > 0 and sent == []
    assert e.answer_crash(True) is True
    assert e.gate_snapshot()["state"] == "confirmed"
    types = [x.event_type for x in sent]
    assert types[0] == "crash_suspected" and {"roadside_crash_initial", "roadside_crash_final"} <= set(types)
    assert e.answer_crash(True) is False              # درخواست دومی در انتظار نیست


def test_driver_decline_drops_everything():
    sent = []
    e = run(driver_response="decline", driver_delay_s=3, crash_alert_sink=sent.append)
    assert e.gate_snapshot()["state"] == "declined" and sent == [] and e.alerts_released == []
    assert e.gate_snapshot()["answered_by"] == "simulated_driver"


def test_simulated_driver_confirm_with_delay():
    sent = []
    e = run(driver_response="confirm", driver_delay_s=5, crash_alert_sink=sent.append)
    g = e.gate_snapshot()
    assert g["state"] == "confirmed" and g["answered_t"] - g["created_t"] == 5
    assert len(sent) == 3 and all(a["t"] == g["answered_t"] for a in e.alerts_released)


def test_no_answer_timeout_policy_send_vs_skip():
    sent = []
    e = run(confirm_timeout_s=20, on_timeout="send", crash_alert_sink=sent.append)
    g = e.gate_snapshot()
    assert g["state"] == "timeout_sent" and len(sent) == 3
    assert {a["t"] for a in e.alerts_released} == {g["created_t"] + 20}     # دقیقاً در لحظه‌ی تایم‌اوت

    sent2 = []
    e2 = run(confirm_timeout_s=20, on_timeout="skip", crash_alert_sink=sent2.append)
    assert e2.gate_snapshot()["state"] == "timeout_skipped" and sent2 == []


def test_late_confirmation_after_skip_still_releases():
    sent = []
    e = SimulationEngine("crash_detection", start_time=T0, confirm_timeout_s=10,
                         on_timeout="skip", crash_alert_sink=sent.append)
    e.run_to_end()
    assert e.gate_snapshot()["state"] == "timeout_skipped" and sent == []
    assert e.answer_crash(True) is True               # در پنجره‌ی تأیید دیرهنگام
    assert e.gate_snapshot()["state"] == "late_confirmed" and len(sent) == 3


def test_failing_alert_sink_does_not_break_the_run():
    e = run(driver_response="confirm", crash_alert_sink=lambda ev: 1 / 0)
    assert e.summary()["verdict"] == "detected_by_detector" and len(e.alerts_released) == 3


@pytest.mark.parametrize("kw", [
    {"driver_response": "maybe"}, {"driver_delay_s": -1}, {"confirm_timeout_s": 0},
    {"on_timeout": "later"},
])
def test_invalid_crash_options_rejected(kw):
    with pytest.raises(ValueError):
        SimulationEngine("crash_detection", **kw)


# ------------------------------------------------------------ سرور، تلگرام، تاریخچه

class FakeNotifier:
    def __init__(self):
        self.events = []

    def notify_event(self, event):
        self.events.append(event)
        return True


@pytest.fixture
def lab(tmp_path):
    notifier = FakeNotifier()
    app = LabApp(notifier_factory=lambda: notifier, history_dir=tmp_path)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, notifier
    app.close()
    srv.shutdown()
    srv.server_close()


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_telegram_gate_via_api_and_manual_answer(lab):
    app, notifier = lab
    app.set_telegram(True)
    app.controller.set_speed(20)
    app.start({"scenario": "crash_detection", "confirm_timeout_s": 90})
    assert wait_for(lambda: (app.controller.engine.gate_snapshot()["state"] == "waiting"))
    app.controller.pause()
    time.sleep(0.4)                                    # اعلان فریم جاری تمام شود (بدون race)
    # رویداد خاموشی (OBD) فوری رفته؛ تصادف نه
    assert [e.event_type for e in notifier.events] == ["engine_stalled_while_moving"]
    assert notifier.events[0].message.startswith("[SIMULATION]")
    assert app.answer_crash(True)["gate"]["state"] == "confirmed"
    sent = [e.event_type for e in notifier.events]
    assert sent[0] == "engine_stalled_while_moving" and "crash_suspected" in sent
    assert all(e.message.startswith("[SIMULATION]") for e in notifier.events)
    with pytest.raises(ValueError):
        app.answer_crash(True)                         # دیگر در انتظار نیست


def test_declined_crash_never_reaches_telegram(lab):
    app, notifier = lab
    app.set_telegram(True)
    app.controller.set_speed(50)
    app.start({"scenario": "crash_detection", "driver_response": "decline", "driver_delay_s": 2})
    assert app.controller.wait(30)
    time.sleep(0.2)
    # فقط رویداد OBD (خاموشی) رفته؛ هیچ پیامی از تصادف، حتی پاسخ اضطراری آن
    assert [e.event_type for e in notifier.events] == ["engine_stalled_while_moving"]
    assert app.controller.engine.gate_snapshot()["alerts_released"] == 0


def test_telegram_off_sends_nothing_even_after_confirm(lab):
    app, notifier = lab
    app.controller.set_speed(50)
    app.start({"scenario": "crash_detection", "driver_response": "confirm"})
    assert app.controller.wait(30)
    assert notifier.events == []
    assert app.controller.engine.gate_snapshot()["alerts_released"] == 3   # دروازه آزاد شد، تلگرام خاموش بود


def test_answer_without_active_prompt_or_scenario_is_rejected(lab):
    app, _ = lab
    with pytest.raises(ValueError):
        app.answer_crash(True)                         # اجرایی نیست
    app.controller.set_speed(50)
    app.start({"scenario": "normal_driving"})
    with pytest.raises(ValueError):
        app.answer_crash(True)                         # سناریو بدون حسگر گوشی
    app.controller.wait(30)


def test_crash_run_history_replay_and_report(lab, tmp_path):
    app, notifier = lab
    app.set_telegram(True)
    app.controller.set_speed(50)
    app.start({"scenario": "crash_detection", "driver_response": "confirm"})
    assert app.controller.wait(30)
    time.sleep(0.2)
    metas = app.store.list_runs()
    assert len(metas) == 1 and metas[0]["metrics"]["detected"] == 2 and metas[0]["metrics"]["expected"] == 2
    sent_before = len(notifier.events)

    app.start({"replay": metas[0]["run_id"]})
    assert app.controller.wait(30)
    frames = app.controller.snapshot()["frames"]
    assert any(f["crash_gate"] and f["crash_gate"]["state"] == "confirmed" for f in frames)
    assert any(f["motion"] and f["motion"]["peak_g"] >= 9 for f in frames)
    assert len(notifier.events) == sent_before         # بازپخش هیچ پیامی نمی‌فرستد
    with pytest.raises(ValueError):
        app.answer_crash(True)                         # در بازپخش پاسخ‌دهی ممنوع
    assert len(app.store.list_runs()) == 1
    assert HistoryStore(tmp_path).load_run(metas[0]["run_id"])["summary"]["crash"]["gate_state"] == "confirmed"
