"""تست‌های سرور و داشبورد شبیه‌ساز (مرحله ۲)."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from app.simulation.lab_server import LabApp, make_server


class FakeNotifier:
    def __init__(self):
        self.events = []

    def notify_event(self, event):
        self.events.append(event)
        return True


@pytest.fixture
def server(tmp_path):
    notifier = FakeNotifier()
    app = LabApp(notifier_factory=lambda: notifier, history_dir=tmp_path)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, app, notifier
    app.close()
    srv.shutdown()
    srv.server_close()


def call(base, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jcall(base, path, body=None, raw=None):
    status, payload = call(base, path, body, raw)
    return status, json.loads(payload)


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_config_lists_scenarios_and_real_thresholds(server):
    base, *_ = server
    status, cfg = jcall(base, "/api/config")
    assert status == 200 and len(cfg["scenarios"]) == 9
    assert cfg["thresholds"] == {"coolant_warning_c": 105, "coolant_critical_c": 115,
                                 "voltage_charging_min_v": 13.0}
    assert cfg["speeds"] == [1, 2, 5, 10]


def test_full_control_cycle_over_http(server):
    base, *_ = server
    assert jcall(base, "/api/speed", {"multiplier": 50})[1]["ok"]
    assert jcall(base, "/api/start", {"scenario": "ecu_trouble_code"})[1]["state"] == "running"
    assert wait_for(lambda: jcall(base, "/api/state")[1]["frame_count"] >= 5)
    assert jcall(base, "/api/pause", {})[1]["state"] == "paused"
    n = jcall(base, "/api/state")[1]["frame_count"]
    time.sleep(0.2)
    assert jcall(base, "/api/state")[1]["frame_count"] == n
    jcall(base, "/api/resume", {})
    assert wait_for(lambda: jcall(base, "/api/state")[1]["state"] == "finished")
    snap = jcall(base, "/api/state")[1]
    assert snap["summary"]["verdict"] == "detected_by_detector"
    # دریافت افزایشی: فقط فریم‌های جدید
    assert len(jcall(base, f"/api/state?from={snap['frame_count'] - 3}")[1]["frames"]) == 3
    assert jcall(base, "/api/restart", {})[1]["ok"]
    assert jcall(base, "/api/reset", {})[1]["state"] == "idle"
    assert jcall(base, "/api/state")[1]["frame_count"] == 0


def test_custom_route_start_destination(server):
    base, *_ = server
    jcall(base, "/api/speed", {"multiplier": 50})
    body = {"scenario": "normal_driving", "start": [35.70, 51.40], "destination": [35.75, 51.30],
            "road_type": "urban", "duration_s": 20}
    assert jcall(base, "/api/start", body)[1]["ok"]
    assert wait_for(lambda: jcall(base, "/api/state")[1]["state"] == "finished")
    f = jcall(base, "/api/state")[1]["frames"]
    assert f[0]["gps"]["latitude"] == pytest.approx(35.70, abs=1e-3)
    assert {x["road_type"] for x in f} == {"urban"}


@pytest.mark.parametrize("path,body", [
    ("/api/start", {"scenario": "nope"}),
    ("/api/start", {"scenario": "normal_driving", "start": [999, 0], "destination": [0, 0]}),
    ("/api/start", {"scenario": "normal_driving", "road_source": "project"}),   # دیتابیس آفلاین نیست
    ("/api/speed", {"multiplier": 0}),
    ("/api/speed", {}),
    ("/api/restart", {}),
])
def test_bad_requests_get_400_not_500(server, path, body):
    base, app, _ = server
    app.roads_db = app.roads_db.parent / "missing.sqlite"
    status, data = jcall(base, path, body)
    assert status == 400 and data["ok"] is False and data["error"]


def test_malformed_json_unknown_route_and_path_traversal(server):
    base, *_ = server
    assert call(base, "/api/start", raw=b"{not json")[0] == 400
    assert call(base, "/api/nothing", {})[0] == 404
    assert call(base, "/../../config.py")[0] == 404
    assert call(base, "/vendor/../../../../main.py")[0] == 404
    assert call(base, "/")[0] == 200 and b"SIMULATOR" in call(base, "/")[1]


def test_telegram_is_off_by_default_and_marks_simulation(server):
    base, app, notifier = server
    jcall(base, "/api/speed", {"multiplier": 50})
    jcall(base, "/api/start", {"scenario": "ecu_trouble_code"})
    assert wait_for(lambda: jcall(base, "/api/state")[1]["state"] == "finished")
    assert notifier.events == []                         # خاموش: هیچ پیامی نرفت

    assert jcall(base, "/api/telegram", {"enabled": True})[1]["enabled"] is True
    jcall(base, "/api/start", {"scenario": "ecu_trouble_code"})
    assert wait_for(lambda: jcall(base, "/api/state")[1]["state"] == "finished")
    assert len(notifier.events) == 1
    assert notifier.events[0].message.startswith("[SIMULATION]")
    assert jcall(base, "/api/telegram", {"enabled": False})[1]["enabled"] is False


def test_telegram_cannot_be_enabled_without_credentials(tmp_path):
    app = LabApp(notifier_factory=lambda: None, history_dir=tmp_path)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, data = jcall(f"http://127.0.0.1:{srv.server_address[1]}", "/api/telegram",
                             {"enabled": True})
        assert status == 400 and not app.telegram_enabled
    finally:
        app.close()
        srv.shutdown()
        srv.server_close()


def test_slow_client_does_not_block_engine(server):
    """موتور مستقل از خواندن داشبورد جلو می‌رود."""
    base, app, _ = server
    jcall(base, "/api/speed", {"multiplier": 50})
    jcall(base, "/api/start", {"scenario": "normal_driving", "duration_s": 60})
    assert wait_for(lambda: app.controller.state == "finished")   # بدون هیچ GET /api/state
    assert app.controller.snapshot()["frame_count"] == 60
