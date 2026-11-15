# app/simulation/lab_server.py
"""
سرور محلی داشبورد شبیه‌ساز (فقط کتابخانه‌ی استاندارد پایتون).

داشبورد با polling از `/api/state` می‌خواند؛ موتور در ریسمان خودش اجرا می‌شود و
هرگز منتظر مرورگر نمی‌ماند. ارسال واقعی تلگرام پیش‌فرض خاموش است و فقط با
کلید داشبورد (و در صورت تنظیم‌بودن توکن) فعال می‌شود.
"""

import json
import logging
import mimetypes
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app.detectors import DetectionEvent
from app.location import RoadAnalyzer
from app.location.offline_provider import OfflineRoadProvider
from app.simulation.lab_controller import SPEEDS, SimulationController
from app.simulation.lab_engine import SimulationEngine, build_detector_engine
from app.simulation.lab_history import HISTORY_DIR, HistoryStore, ReplayEngine
from app.simulation.lab_report import build_report, runs_csv, scenarios_csv
from app.simulation.lab_routing import RoadRouter, RoutingError
from app.simulation.options import DRIVER_ACTIONS
from app.motion import CrashConfig
from app.motion.crash_response import DEFAULT_CONFIRM_TIMEOUT_S, DEFAULT_ON_TIMEOUT, TIMEOUT_POLICIES
from app.simulation.routes import ROUTES, ROAD_TYPES, Route
from app.simulation.scenarios import LAB_SCENARIOS

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
ROADS_DB = Path(__file__).resolve().parent.parent.parent / "data" / "roads_tehran.sqlite"
MAX_BODY = 64 * 1024


class LabApp:
    def __init__(self, controller: SimulationController | None = None,
                 notifier_factory=None, roads_db: Path = ROADS_DB,
                 history_dir: Path | str = HISTORY_DIR):
        self.store = HistoryStore(history_dir)
        self.router = RoadRouter(roads_db)
        self._route_cache: OrderedDict = OrderedDict()
        self.controller = controller or SimulationController(self._make_engine)
        self.controller.subscribe_end(self._on_run_end)
        self.roads_db = roads_db
        self._notifier_factory = notifier_factory
        self._notifier = None
        self.telegram_enabled = False
        self.controller.subscribe(self._on_frame)

    # ---- ساخت موتور از پارامترهای JSON
    def _make_engine(self, scenario, **p):
        if scenario.startswith("replay:"):
            return ReplayEngine(scenario, self.store)
        return SimulationEngine(scenario, crash_alert_sink=self._send_crash_alert, **p)

    def _send_crash_alert(self, event) -> None:
        """پیام‌های تصادف فقط بعد از تأیید راننده به اینجا می‌رسند (CrashResponder)."""
        if not self.telegram_enabled or self._notifier is None:
            return
        event = event.model_copy(update={"message": "[SIMULATION] " + event.message})
        self._notifier.notify_event(event)

    def answer_crash(self, confirmed: bool) -> dict:
        engine = self.controller.engine
        if (engine is None or getattr(engine, "is_replay", False)
                or getattr(engine, "responder", None) is None):
            raise ValueError("پرسش تأیید راننده فعال نیست.")
        if not engine.answer_crash(confirmed):
            raise ValueError("درخواست تأییدی در انتظار پاسخ نیست.")
        return {"gate": engine.gate_snapshot()}

    def _on_run_end(self, engine, status, started_at, ended_at) -> None:
        if getattr(engine, "is_replay", False):
            return                                   # بازپخش دوباره ذخیره نمی‌شود
        try:
            self.store.save_run(engine, status, started_at, ended_at)
        except Exception:
            logger.exception("Saving simulation history failed.")

    def plan_route(self, start, dest, road_type: str = "highway") -> dict:
        """
        مسیر روی جاده‌های دیتابیس آفلاین؛ اگر ممکن نبود، خط مستقیم با هشدار صریح.
        نتیجه: {"route": Route, "method": "roads"|"straight", "warning": str|None, ...}
        """
        key = (round(start[0], 6), round(start[1], 6), round(dest[0], 6), round(dest[1], 6), road_type)
        if key in self._route_cache:
            self._route_cache.move_to_end(key)
            return self._route_cache[key]
        try:
            planned = self.router.plan(start, dest)
            route = Route("custom", "مسیر روی جاده‌های دیتابیس آفلاین",
                          [tuple(p) for p in planned.points], planned.road_types)
            result = {"route": route, "method": "roads", "warning": None}
        except RoutingError as exc:
            route = Route.from_points(start, dest, road_type)
            result = {"route": route, "method": "straight",
                      "warning": f"مسیر روی جاده پیدا نشد؛ خط مستقیم استفاده شد. ({exc})"}
        result["length_m"] = round(route.length_m, 1)
        self._route_cache[key] = result
        while len(self._route_cache) > 8:
            self._route_cache.popitem(last=False)
        return result

    def route_preview(self, start, dest, road_type: str) -> dict:
        r = self.plan_route(start, dest, road_type)
        route = r["route"]
        return {"method": r["method"], "warning": r["warning"], "length_m": r["length_m"],
                "points": [list(p) for p in route.points], "road_types": route.road_types}

    def start(self, body: dict) -> dict | None:
        if body.get("replay"):
            self.store.load_run(str(body["replay"]))   # ValueError اگر نباشد
            self.controller.start("replay:" + str(body["replay"]))
            return None
        params = {}
        info = None
        if body.get("event_time_s") not in (None, ""):
            params["event_time_s"] = float(body["event_time_s"])
        if body.get("duration_s") not in (None, ""):
            params["duration_s"] = float(body["duration_s"])
        if body.get("driver_response"):
            params["driver_response"] = str(body["driver_response"])
        for key in ("driver_delay_s", "confirm_timeout_s"):
            if body.get(key) not in (None, ""):
                params[key] = float(body[key])
        if body.get("on_timeout"):
            params["on_timeout"] = str(body["on_timeout"])
        if body.get("vehicle_id"):
            params["vehicle_id"] = str(body["vehicle_id"])
        if body.get("start") and body.get("destination"):
            s, d = body["start"], body["destination"]
            planned = self.plan_route(
                (float(s[0]), float(s[1])), (float(d[0]), float(d[1])),
                str(body.get("road_type", "highway")),
            )
            params["route"] = planned["route"]
            info = {"route_method": planned["method"], "warning": planned["warning"],
                    "route_length_m": planned["length_m"]}
        if body.get("road_source") == "project":
            if not self.roads_db.is_file():
                raise ValueError("دیتابیس آفلاین جاده (data/roads_tehran.sqlite) پیدا نشد.")
            params["road_analyzer"] = RoadAnalyzer(offline=OfflineRoadProvider(self.roads_db))
        self.controller.start(str(body.get("scenario", "")), **params)
        return info

    # ---- تلگرام
    def set_telegram(self, enabled: bool) -> dict:
        if not enabled:
            self.telegram_enabled = False
            return {"enabled": False}
        if self._notifier is None:
            self._notifier = self._notifier_factory() if self._notifier_factory else None
        if self._notifier is None:
            raise ValueError("تلگرام تنظیم نشده است (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        self.telegram_enabled = True
        return {"enabled": True}

    def _on_frame(self, frame: dict) -> None:
        if not self.telegram_enabled or self._notifier is None or frame.get("replayed"):
            return                                   # بازپخش هرگز پیام واقعی نمی‌فرستد
        for raw in frame["events"]:
            if raw["detector"] == "crash":
                continue                             # فقط از دروازه‌ی تأیید راننده می‌گذرد
            if raw["severity"] in ("warning", "critical"):
                event = DetectionEvent.model_validate(raw)
                event = event.model_copy(update={"message": "[SIMULATION] " + event.message})
                self._notifier.notify_event(event)

    def close(self) -> None:
        self.controller.reset()
        if self._notifier is not None and hasattr(self._notifier, "close"):
            self._notifier.close(timeout=3.0)

    def config(self) -> dict:
        det = {type(d).__name__: d for d in build_detector_engine().detectors}
        cool, volt = det["CoolantOverheatDetector"], det["VoltageDetector"]
        return {
            "scenarios": [
                {"name": s.name, "title_fa": s.title_fa, "title_en": s.title_en,
                 "description": s.description, "route": s.route, "duration_s": s.duration_s,
                 "event_time_s": s.event_time_s, "coverage": s.coverage,
                 "phone_sensors": s.phone_sensors,
                 "coverage_note": s.coverage_note}
                for s in LAB_SCENARIOS.values()
            ],
            "routes": {k: r.to_dict() for k, r in ROUTES.items()},
            "road_types": list(ROAD_TYPES),
            "speeds": list(SPEEDS),
            "thresholds": {
                "coolant_warning_c": cool.warning_c, "coolant_critical_c": cool.critical_c,
                "voltage_charging_min_v": volt.charging_min_v,
            },
            "crash": {
                "impact_g": CrashConfig().impact_g,
                "confirm_window_s": CrashConfig().confirm_window_s,
                "default_confirm_timeout_s": DEFAULT_CONFIRM_TIMEOUT_S,
                "default_on_timeout": DEFAULT_ON_TIMEOUT,
                "on_timeout_policies": list(TIMEOUT_POLICIES),
                "driver_actions": list(DRIVER_ACTIONS),
            },
            "project_road_available": self.roads_db.is_file(),
            "roads_available": self.router.available,
            "telegram_configurable": self._notifier_factory is not None,
        }


def make_handler(app: LabApp):
    posts = {
        "/api/pause": lambda b: app.controller.pause(),
        "/api/resume": lambda b: app.controller.resume(),
        "/api/stop": lambda b: app.controller.stop(),
        "/api/reset": lambda b: app.controller.reset(),
        "/api/restart": lambda b: app.controller.restart(),
        "/api/start": app.start,
        "/api/speed": lambda b: app.controller.set_speed(float(b["multiplier"])),
        "/api/telegram": lambda b: app.set_telegram(bool(b.get("enabled"))),
        "/api/crash/answer": lambda b: app.answer_crash(bool(b["confirmed"])),
        "/api/history/delete": lambda b: app.store.delete_run(str(b.get("run_id", ""))),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, obj, status=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _text(self, text: str, filename: str):
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path == "/api/config":
                    return self._json(app.config())
                if url.path == "/api/route":
                    q = parse_qs(url.query)
                    try:
                        s = [float(x) for x in q["start"][0].split(",")]
                        d = [float(x) for x in q["dest"][0].split(",")]
                        if len(s) != 2 or len(d) != 2:
                            raise ValueError
                        return self._json(app.route_preview(tuple(s), tuple(d), q.get("road_type", ["highway"])[0]))
                    except (KeyError, ValueError, IndexError, TypeError):
                        return self._json({"error": "پارامترهای start و dest باید به‌صورت lat,lon باشند."}, 400)
                if url.path == "/api/roads":
                    q = parse_qs(url.query)
                    try:
                        box = [float(q[k][0]) for k in ("south", "west", "north", "east")]
                        zoom = int(q.get("zoom", ["14"])[0])
                        return self._json(app.router.roads_in_bbox(*box, zoom))
                    except (KeyError, ValueError, TypeError):
                        return self._json({"error": "پارامترهای south, west, north, east, zoom لازم است."}, 400)
                    except RoutingError as exc:
                        return self._json({"error": str(exc), "features": []}, 404)
                if url.path == "/api/history":
                    return self._json({"runs": app.store.list_runs()})
                if url.path.startswith("/api/history/"):
                    try:
                        return self._json(app.store.load_run(unquote(url.path.rsplit("/", 1)[1])))
                    except ValueError as exc:
                        return self._json({"error": str(exc)}, 404)
                if url.path == "/api/report":
                    return self._json(build_report(app.store.list_runs()))
                if url.path == "/api/report.csv":
                    metas = app.store.list_runs()
                    by = parse_qs(url.query).get("by", ["run"])[0]
                    text = scenarios_csv(build_report(metas)) if by == "scenario" else runs_csv(metas)
                    return self._text(text, f"lab-report-{by}.csv")
                if url.path == "/api/state":
                    start = int(parse_qs(url.query).get("from", ["0"])[0])
                    return self._json(app.controller.snapshot(max(0, start)))
                self._static(url.path)
            except Exception:
                logger.exception("GET %s failed", url.path)
                self._json({"error": "internal error"}, 500)

        def _static(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents or not target.is_file():
                return self._json({"error": "not found"}, 404)
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                             + ("; charset=utf-8" if target.suffix in (".html", ".js", ".css") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            action = posts.get(urlparse(self.path).path)
            if action is None:
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._json({"error": "body too large"}, 413)
                body = json.loads(self.rfile.read(length) or b"{}")
                result = action(body)
                self._json({"ok": True, **(result or {}), "state": app.controller.state})
            except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            except Exception:
                logger.exception("POST %s failed", self.path)
                self._json({"ok": False, "error": "internal error"}, 500)

    return Handler


def make_server(app: LabApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
