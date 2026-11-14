"""تست‌های تاریخچه، بازپخش، گزارش و CSV (مرحله ۳)."""

import csv
import io
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from app.simulation.lab_controller import SimulationController
from app.simulation.lab_engine import SimulationEngine
from app.simulation.lab_history import HistoryStore, ReplayEngine
from app.simulation.lab_report import _safe, build_report, runs_csv, scenarios_csv
from app.simulation.lab_server import LabApp, make_server

T0 = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)


def finished_engine(name, **kw):
    e = SimulationEngine(name, start_time=T0, **kw)
    e.run_to_end()
    return e


def save(store, name, status="finished", n=0):
    e = finished_engine(name)
    return store.save_run(e, status, T0 + timedelta(seconds=n), T0 + timedelta(seconds=n + 5))


# ------------------------------------------------------------ ذخیره‌سازی

def test_saved_run_contains_everything_required(tmp_path):
    store = HistoryStore(tmp_path)
    rid = save(store, "highway_stoppage")
    run = store.load_run(rid)
    meta = run["meta"]
    assert meta["scenario"] == "highway_stoppage" and meta["status"] == "finished"
    assert meta["started_at"] and meta["ended_at"]                   # زمان شروع و پایان
    assert len(run["frames"]) == 170 and "obd" in run["frames"][0]   # داده‌ی خودرو
    assert run["frames"][0]["gps"]["latitude"] is not None           # GPS
    assert [e["event_type"] for e in run["events"]] == ["engine_stalled_while_moving"]
    assert run["alerts"] and meta["metrics"]["max_danger_level"] == "warning"
    assert [r["phase"] for r in run["emergency"]] == ["initial", "final"]
    assert run["summary"]["verdict"] == "detected_by_detector"


def test_empty_run_is_not_saved_and_ids_are_unique(tmp_path):
    store = HistoryStore(tmp_path)
    assert store.save_run(SimulationEngine("normal_driving", start_time=T0), "stopped", T0, T0) is None
    a, b = save(store, "ecu_trouble_code"), save(store, "ecu_trouble_code")
    assert a != b and len(store.list_runs()) == 2


def test_list_skips_corrupt_and_rejects_bad_ids(tmp_path):
    store = HistoryStore(tmp_path)
    rid = save(store, "ecu_trouble_code")
    (tmp_path / "20260101-000000-broken-1.meta.json").write_text("{not json", encoding="utf-8")
    assert [m["run_id"] for m in store.list_runs()] == [rid]
    for bad in ("../x", "a/b", "", "20260101-000000-x"):
        with pytest.raises(ValueError):
            store.load_run(bad)
    with pytest.raises(ValueError):
        store.load_run("20260101-000000-missing-1")
    (tmp_path / f"{rid}.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load_run(rid)


def test_prune_keeps_newest(tmp_path):
    store = HistoryStore(tmp_path, max_runs=3)
    ids = [save(store, "ecu_trouble_code", n=i * 2) for i in range(5)]
    kept = {m["run_id"] for m in store.list_runs()}
    assert kept == set(ids[-3:])
    assert not list(tmp_path.glob(f"{ids[0]}*"))


# ------------------------------------------------------------ بازپخش

def test_replay_preserves_order_and_content(tmp_path):
    store = HistoryStore(tmp_path)
    rid = save(store, "multiple_failures")
    original = store.load_run(rid)
    replay = ReplayEngine("replay:" + rid, store)
    while not replay.finished:
        replay.step()
    assert len(replay.frames) == len(original["frames"])
    strip = lambda f: {k: v for k, v in f.items() if k not in ("replayed", "replay_of")}  # noqa: E731
    assert [strip(f) for f in replay.frames] == original["frames"]
    assert all(f["replayed"] and f["replay_of"] == rid for f in replay.frames)
    ts = [f["t"] for f in replay.frames]
    assert ts == sorted(ts)
    ev = [(f["t"], e["event_type"]) for f in replay.frames for e in f["events"]]
    assert ev == [(e["sim_t"], e["event_type"]) for e in original["events"]]


def test_replay_through_controller_supports_pause_and_is_not_saved(tmp_path):
    app = LabApp(history_dir=tmp_path)
    rid = save(app.store, "ecu_trouble_code")
    app.controller.set_speed(50)
    app.start({"replay": rid})
    deadline = time.time() + 5
    while app.controller.snapshot()["frame_count"] < 10 and time.time() < deadline:
        time.sleep(0.01)
    app.controller.pause()
    n = app.controller.snapshot()["frame_count"]
    time.sleep(0.2)
    assert app.controller.snapshot()["frame_count"] == n
    app.controller.resume()
    assert app.controller.wait(10)
    assert app.controller.snapshot()["frame_count"] == 120
    assert len(app.store.list_runs()) == 1           # بازپخش اجرای جدیدی نساخت
    with pytest.raises(ValueError):
        app.start({"replay": "20260101-000000-missing-1"})


# ------------------------------------------------------------ ذخیره‌ی خودکار توسط کنترلر

def test_autosave_on_finish_stop_and_abort_exactly_once(tmp_path):
    app = LabApp(history_dir=tmp_path)
    c = app.controller
    c.set_speed(50)
    c.start("ecu_trouble_code", start_time=T0)               # کامل
    assert c.wait(10)
    time.sleep(0.1)
    assert [m["status"] for m in app.store.list_runs()] == ["finished"]

    c.set_speed(1)
    c.start("coolant_overheat", start_time=T0)               # Stop
    time.sleep(2.2)
    c.stop()
    c.reset()                                                # بعد از Stop دوباره ذخیره نشود
    c.start("battery_drop", start_time=T0)                   # جایگزینی وسط اجرا → aborted
    time.sleep(1.5)
    c.start("normal_driving", start_time=T0)
    time.sleep(1.5)
    c.reset()
    statuses = sorted((m["scenario"], m["status"]) for m in app.store.list_runs())
    assert statuses == [("battery_drop", "aborted"), ("coolant_overheat", "stopped"),
                        ("ecu_trouble_code", "finished"), ("normal_driving", "aborted")]


def test_listener_failure_does_not_break_controller():
    c = SimulationController()
    c.subscribe_end(lambda *a: 1 / 0)
    c.set_speed(50)
    c.start("ecu_trouble_code", start_time=T0)
    assert c.wait(10) and c.snapshot()["state"] == "finished"


# ------------------------------------------------------------ گزارش

def test_report_aggregates_per_scenario(tmp_path):
    store = HistoryStore(tmp_path)
    save(store, "highway_stoppage", n=0)
    save(store, "highway_stoppage", n=10)
    save(store, "normal_driving", n=20)
    save(store, "coolant_overheat", status="stopped", n=30)
    rep = build_report(store.list_runs())
    rows = {r["scenario"]: r for r in rep["scenarios"]}
    st = rows["highway_stoppage"]
    assert (st["runs"], st["finished_runs"], st["correct_detections"], st["missed"]) == (2, 2, 2, 0)
    assert st["mean_detection_latency_s"] == 3.0 and st["emergency_responses"] == 4
    assert st["mean_emergency_generation_ms"] is not None
    assert rows["normal_driving"]["false_alarms"] == 0
    assert rows["coolant_overheat"]["incomplete_runs"] == 1 and rows["coolant_overheat"]["missed"] == 0
    t = rep["totals"]
    assert (t["runs"], t["finished_runs"], t["incomplete_runs"], t["false_alarms"]) == (4, 3, 1, 0)
    assert build_report([])["totals"]["runs"] == 0


def test_csv_output_is_parseable_and_safe(tmp_path):
    store = HistoryStore(tmp_path)
    save(store, "highway_stoppage")
    save(store, "gps_signal_loss", n=10)
    metas = store.list_runs()
    text = runs_csv(metas)
    assert text.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    by = {r["scenario"]: r for r in rows}
    assert by["highway_stoppage"]["verdict"] == "detected_by_detector"
    assert by["highway_stoppage"]["correct_detections"] == "1"
    assert by["gps_signal_loss"]["verdict"] == "status_only_no_detector"
    assert by["gps_signal_loss"]["alerts_issued"] == "1"             # هشدار وضعیت GPS هم شمرده می‌شود
    srows = list(csv.DictReader(io.StringIO(scenarios_csv(build_report(metas)).lstrip("\ufeff"))))
    assert {r["scenario"] for r in srows} == {"highway_stoppage", "gps_signal_loss"}
    assert _safe("=1+1") == "'=1+1" and _safe("-5") == "'-5" and _safe(None) == "" and _safe(3) == "3"


# ------------------------------------------------------------ API

@pytest.fixture
def server(tmp_path):
    app = LabApp(history_dir=tmp_path)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app
    app.close()
    srv.shutdown()
    srv.server_close()


def http(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_api_history_report_csv_replay_delete(server):
    base, app = server
    rid = save(app.store, "highway_stoppage")
    assert http(base, "/api/history")[2] and json.loads(http(base, "/api/history")[2])["runs"][0]["run_id"] == rid
    assert len(json.loads(http(base, f"/api/history/{rid}")[2])["frames"]) == 170
    assert http(base, "/api/history/..%2F..%2Fconfig")[0] == 404
    assert http(base, "/api/history/20260101-000000-nope-1")[0] == 404
    assert json.loads(http(base, "/api/report")[2])["totals"]["correct_detections"] == 1
    status, headers, body = http(base, "/api/report.csv")
    assert status == 200 and "attachment" in headers["Content-Disposition"] and b"highway_stoppage" in body
    assert b"scenario,runs" in http(base, "/api/report.csv?by=scenario")[2]
    assert json.loads(http(base, "/api/start", {"replay": rid})[2])["ok"]
    assert http(base, "/api/start", {"replay": "bad"})[0] == 400
    assert json.loads(http(base, "/api/history/delete", {"run_id": rid})[2])["ok"]
    assert json.loads(http(base, "/api/history")[2])["runs"] == []
    assert http(base, "/api/history/delete", {"run_id": "../x"})[0] == 400


def test_telegram_never_fires_during_replay(tmp_path):
    sent = []

    class N:
        def notify_event(self, e):
            sent.append(e)

    app = LabApp(notifier_factory=lambda: N(), history_dir=tmp_path)
    rid = save(app.store, "ecu_trouble_code")
    app.set_telegram(True)
    app.controller.set_speed(50)
    app.start({"replay": rid})
    assert app.controller.wait(10)
    assert sent == []
    app.close()
