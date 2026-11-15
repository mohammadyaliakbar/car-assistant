# app/simulation/lab_engine.py
"""
موتور آزمایشگاه شبیه‌سازی (ساعت مجازی، قابل‌تکرار).

مسیر داده دقیقاً مسیر واقعی پروژه است:
    داده‌ی خام OBD ← DataValidator ← VehicleRecord(source="SIMULATOR")
    ← DetectorEngine (Detectorهای واقعی) ← RoadsideEmergencyService

هیچ رویدادی مستقیم ساخته نمی‌شود. تنها استثنا وضعیت GPS است که Detector ندارد و
با منبع `simulator_status` و بدون ادعای تشخیص Detector علامت‌گذاری می‌شود.
"""

import dataclasses
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from itertools import islice

from app.data_models import VehicleRecord
from app.data_validation import DataValidator
from app.detectors import (
    CoolantOverheatDetector,
    DetectionEvent,
    DetectorEngine,
    Severity,
    StoppageDetector,
    TroubleCodeDetector,
    VoltageDetector,
)
from app.emergency import (
    EmergencyContacts,
    RecommendationEngine,
    RiskAnalyzer,
    RoadsideEmergencyService,
)
from app.motion import CrashDetector, CrashMonitor, CrashResponder, MotionSimulator
from app.motion.crash_response import DEFAULT_CONFIRM_TIMEOUT_S, DEFAULT_ON_TIMEOUT, TIMEOUT_POLICIES
from app.simulation.options import DRIVER_ACTIONS
from app.location.gps_client import GpsSource
from app.location.models import GpsFix, RoadContext
from app.simulation.routes import Route, get_route
from app.simulation.scenarios import (
    DTC_DESCRIPTIONS,
    ENGINE_ON_RPM,
    DrivingModel,
    ScenarioSpec,
    get_scenario,
    obd_frame,
)
from app.vehicle_profiles import get_vehicle_profile

logger = logging.getLogger(__name__)

SOURCE_NAME = "SIMULATOR"
MOTION_RATE_HZ = 50                # نرخ حسگر گوشی؛ هر ثانیه‌ی مجازی ۵۰ نمونه
DT_S = 1.0                      # هر گام = یک ثانیه‌ی مجازی (مثل POLL_INTERVAL_S پروژه)
GPS_ACCURACY_M = 5.0
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def build_detector_engine() -> DetectorEngine:
    """همان فهرست و پارامترهای پیش‌فرض main.build_detector_engine (آستانه‌ها دست‌نخورده)."""
    return DetectorEngine([
        CoolantOverheatDetector(),
        TroubleCodeDetector(),
        VoltageDetector(),
        StoppageDetector(),
    ])


class SimulatedRoadAnalyzer:
    """
    اطلاعات جاده‌ی ساختگی متناسب با نوع قطعه‌ی مسیر، برای نمایش قطعی و آفلاین.
    جایگزین RoadAnalyzer واقعی (دیتابیس آفلاین/Overpass) است و با همان واسط
    `analyze(fix)` کار می‌کند. خروجی با note صریح «داده‌ی ساختگی» علامت می‌خورد.
    """

    CONTEXTS = {
        "urban": dict(highway_type="secondary", is_limited_access=False, maxspeed_kmh=50.0),
        "highway": dict(highway_type="motorway", is_limited_access=True, maxspeed_kmh=110.0),
        "rural": dict(highway_type="primary", is_limited_access=False, maxspeed_kmh=80.0,
                      is_winding=True),
    }

    def __init__(self, road_type_getter):
        self._road_type = road_type_getter

    def analyze(self, fix: GpsFix | None) -> RoadContext:
        if fix is None:
            return RoadContext.unavailable("no GPS fix")
        return RoadContext(
            **self.CONTEXTS[self._road_type()],
            data_quality="ok",
            notes=["SIMULATED road data (not from a real map)"],
        )


class LabMotionSource:
    """منبع حرکتی فشاری (push)؛ واسط MotionSource پروژه."""

    def __init__(self):
        self._samples: list = []

    def push(self, samples) -> None:
        self._samples.extend(samples)

    def read_samples(self) -> list:
        out, self._samples = self._samples, []
        return out

    def read_vehicle_signals(self) -> list:
        return []

    def close(self) -> None:
        pass


class RouteGpsSource(GpsSource):
    """GPS مسیرمحور. هنگام قطع، آخرین نمونه‌ی معتبر را با زمان قدیمی برمی‌گرداند."""

    def __init__(self):
        self.last_fix: GpsFix | None = None

    def get_fix(self) -> GpsFix | None:
        return self.last_fix


class SimulationEngine:
    def __init__(
        self,
        scenario: str,
        vehicle_id: str = "car_a",
        event_time_s: float | None = None,
        duration_s: float | None = None,
        route: Route | None = None,
        seed: int = 1,
        start_time: datetime | None = None,
        road_analyzer=None,
        driver_response: str = "none",
        driver_delay_s: float = 5.0,
        confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
        on_timeout: str = DEFAULT_ON_TIMEOUT,
        crash_alert_sink=None,
    ):
        self.spec: ScenarioSpec = get_scenario(scenario)
        self.route = route or get_route(self.spec.route)
        self.event_time_s = (
            self.spec.event_time_s if event_time_s is None else float(event_time_s)
        )
        self.duration_s = float(duration_s or self.spec.duration_s)
        if self.duration_s <= 0 or self.event_time_s < 0:
            raise ValueError("زمان سناریو نامعتبر است.")
        self.seed = seed
        if driver_response not in DRIVER_ACTIONS:
            raise ValueError(f"driver_response باید یکی از {DRIVER_ACTIONS} باشد.")
        if driver_delay_s < 0:
            raise ValueError("driver_delay_s نباید منفی باشد.")
        if not confirm_timeout_s > 0:
            raise ValueError("confirm_timeout_s باید مثبت باشد.")
        if on_timeout not in TIMEOUT_POLICIES:
            raise ValueError(f"on_timeout باید یکی از {TIMEOUT_POLICIES} باشد.")
        self.driver_response = driver_response
        self.driver_delay_s = float(driver_delay_s)
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.on_timeout = on_timeout
        self._alert_sink = crash_alert_sink
        self.profile = get_vehicle_profile(vehicle_id)
        self.start_time = start_time or datetime.now(timezone.utc)

        config, self.expected = self.spec.build(self.event_time_s)
        if route is not None:       # مسیر دلخواه: سرعت از نوع هر قطعه‌ی جاده می‌آید
            config = dataclasses.replace(config, road_dependent_speed=True)
        self.model = DrivingModel(config, seed)
        self.validator = DataValidator(self.profile)
        self.detectors = build_detector_engine()
        self.gps = RouteGpsSource()
        self._current_road = self.route.road_types[0]
        self.roads = road_analyzer or SimulatedRoadAnalyzer(lambda: self._current_road)

        self._responses: list = []
        self._new_responses: list[dict] = []
        self.emergency = RoadsideEmergencyService(
            gps=self.gps,
            road_analyzer=self.roads,
            risk_analyzer=RiskAnalyzer(),
            recommender=RecommendationEngine(),
            contacts=EmergencyContacts(default_country="IR"),
            vehicle=f"{self.profile.manufacturer} {self.profile.model}",
            country_code="IR",
            publish=self._responses.append,
            submit=lambda fn: fn(),         # همزمان؛ نتیجه‌ی تکرارپذیر
            clock=self.now,
        )

        # حسگر گوشی و تشخیص تصادف (فقط سناریوهای دارای motion_scenario)
        self.responder: CrashResponder | None = None
        self.crash_monitor: CrashMonitor | None = None
        self.alerts_released: list[dict] = []
        self._gate_lock = threading.Lock()
        self._gate: dict = {"state": "idle"}
        self._driver_answer_t: float | None = None
        self._mail_import_failed = False
        if self.spec.phone_sensors:
            self.motion_source = LabMotionSource()
            self._motion_iter = MotionSimulator(
                rate_hz=MOTION_RATE_HZ,
                duration_s=self.duration_s + 1,
                event_time_s=self.event_time_s,
                seed=seed,
                start=self.start_time,
            ).scenario(self.spec.motion_scenario)
            self.crash_detector = CrashDetector(self.profile.vehicle_id)
            self.responder = CrashResponder(
                emergency_handler=lambda event: self._run_emergency(event, crash=True),
                send_alert=self._release_alert,
                prompt=self._on_prompt,
                timeout_s=self.confirm_timeout_s,
                on_timeout=self.on_timeout,
                clock=self.now,
            )
            self.crash_monitor = CrashMonitor(
                self.motion_source, self.crash_detector, self.responder, clock=self.now
            )

        self.t = 0.0
        self.distance_m = 0.0
        self.frames: list[dict] = []
        self.events: list[dict] = []
        self.emergency_log: list[dict] = []
        self.alerts: dict[str, dict] = {}
        self.validation_errors = 0
        self._detected: dict[tuple, float] = {}
        self._unexpected: list[dict] = []
        self._gps_lost_since: float | None = None

    # ------------------------------------------------------------ زمان

    def now(self) -> datetime:
        return self.start_time + timedelta(seconds=self.t)

    @property
    def finished(self) -> bool:
        return self.t >= self.duration_s

    # ------------------------------------------------------------ گام

    def step(self) -> dict | None:
        if self.finished:
            return None
        t = self.t
        ts = self.now()

        road = self.route.position_at(self.distance_m).road_type
        self._current_road = road
        state = self.model.advance(t, road)
        self.distance_m += state.speed / 3.6 * DT_S
        pos = self.route.position_at(self.distance_m)
        self._current_road = pos.road_type

        gps_info = self._update_gps(state, pos, ts)
        raw = obd_frame(state)
        report = self.validator.validate(raw)

        new_events: list[DetectionEvent] = []
        if report.errors or report.data is None:
            self.validation_errors += 1
        else:
            record = VehicleRecord(
                vehicle_id=self.profile.vehicle_id,
                source=SOURCE_NAME,
                timestamp=ts,
                data=report.data,
            )
            new_events = self.detectors.process(record)
            for event in new_events:
                self._register_event(event, t)
                self._run_emergency(event)
            if self.crash_monitor is not None:
                self.crash_monitor.update_vehicle(ts, raw["speed"], raw["rpm"])

        motion = None
        if self.crash_monitor is not None:
            motion = self._feed_motion()
            for event in self.crash_monitor.step():       # CrashDetector واقعی
                self._register_event(event, t)
                new_events.append(event)
            self._advance_gate(t)

        new_responses, self._new_responses = self._new_responses, []
        self.emergency_log.extend(new_responses)

        status_alert = self._gps_status_alert(gps_info, t, ts)

        frame = {
            "t": t,
            "timestamp": ts.isoformat(),
            "source": SOURCE_NAME,
            "simulated": True,
            "scenario": self.spec.name,
            "obd": raw,
            "engine_on": state.rpm >= ENGINE_ON_RPM,
            "road_type": pos.road_type,
            "distance_m": round(self.distance_m, 1),
            "arrived": pos.arrived,
            "gps": gps_info,
            "validation": {"errors": report.errors, "warnings": report.warnings},
            "events": [e.model_dump(mode="json") for e in new_events],
            "status_alerts": [status_alert] if status_alert else [],
            "emergency": new_responses,
            "alerts": self.active_alerts(),
            "danger_level": self.danger_level(),
            "dtc_descriptions": {c: DTC_DESCRIPTIONS.get(c, "Unknown code") for c in state.dtc},
            "motion": motion,
            "crash_gate": self.gate_snapshot(t) if self.responder is not None else None,
        }
        self.frames.append(frame)
        self.t += DT_S
        return frame

    def run_to_end(self) -> list[dict]:
        while not self.finished:
            self.step()
        return self.frames

    # ------------------------------------------------------------ اضطراری و تصادف

    def _run_emergency(self, event: DetectionEvent, crash: bool = False) -> None:
        started = time.perf_counter()
        before = len(self._responses)
        self.emergency.handle(event, crash_suspected=crash)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        for resp in self._responses[before:]:
            d = resp.model_dump(mode="json")
            d["generation_ms"] = elapsed_ms
            d["sim_t"] = self.t
            self._new_responses.append(d)
            if self.responder is not None and resp.event_type == CrashDetector.EVENT_TYPE:
                self._hold_crash_message(resp)

    def _hold_crash_message(self, resp) -> None:
        """پاسخ اضطراریِ تصادف هم مثل main.py پشت دروازه‌ی تأیید راننده نگه داشته می‌شود."""
        try:
            from main import emergency_to_event      # همان تبدیل برنامه‌ی اصلی
            self.responder.submit_alert(emergency_to_event(resp, self.profile.vehicle_id))
        except Exception:
            if not self._mail_import_failed:
                self._mail_import_failed = True
                logger.exception("Could not queue the crash emergency message.")

    def _feed_motion(self) -> dict | None:
        samples = [step.sample for step in islice(self._motion_iter, MOTION_RATE_HZ)]
        if not samples:
            return None
        self.motion_source.push(samples)
        gyros = [x.gyro_magnitude_rad_s for x in samples if x.gyro_magnitude_rad_s is not None]
        g = [x.accel_magnitude_g for x in samples]
        return {
            "samples": len(samples),
            "peak_g": round(max(g), 2),
            "mean_g": round(sum(g) / len(g), 2),
            "peak_gyro_rad_s": round(max(gyros), 2) if gyros else None,
        }

    def _on_prompt(self, request) -> None:
        with self._gate_lock:
            self._gate = {
                "state": "waiting", "request_id": request.id,
                "created_t": self.t, "expires_t": self.t + self.confirm_timeout_s,
                "answered_t": None, "answered_by": None,
            }
        if self.driver_response != "none":
            self._driver_answer_t = self.t + self.driver_delay_s

    def answer_crash(self, confirmed: bool, by: str = "user") -> bool:
        """پاسخ راننده؛ از هر ریسمانی امن است. False یعنی درخواستی نبود."""
        if self.responder is None or not self.responder.answer(confirmed):
            return False
        with self._gate_lock:
            late = self._gate.get("state", "").startswith("timeout")
            self._gate.update(
                state=("late_" if late else "") + ("confirmed" if confirmed else "declined"),
                answered_t=self.t, answered_by=by,
            )
        return True

    def _release_alert(self, event: DetectionEvent) -> None:
        """پیام پس از تأیید راننده (یا تایم‌اوت با سیاست send) آزاد می‌شود."""
        self.alerts_released.append({
            "t": self.t, "event_type": event.event_type,
            "severity": event.severity.value, "message": event.message,
        })
        if self._alert_sink is not None:
            try:
                self._alert_sink(event)
            except Exception:
                logger.exception("Crash alert sink failed.")

    def _advance_gate(self, t: float) -> None:
        if self._driver_answer_t is not None and t >= self._driver_answer_t:
            self._driver_answer_t = None
            self.answer_crash(self.driver_response == "confirm", by="simulated_driver")
        with self._gate_lock:
            waiting = self._gate.get("state") == "waiting"
        if waiting and not self.responder.pending_requests:     # تایم‌اوت شد
            with self._gate_lock:
                if self._gate.get("state") == "waiting":
                    self._gate["state"] = (
                        "timeout_sent" if self.on_timeout == "send" else "timeout_skipped"
                    )

    def gate_snapshot(self, t: float | None = None) -> dict:
        now_t = self.t if t is None else t
        with self._gate_lock:
            g = dict(self._gate)
        g["seconds_left"] = (
            max(0.0, round(g["expires_t"] - now_t, 1)) if g["state"] == "waiting" else None
        )
        g["alerts_released"] = len(self.alerts_released)
        g["on_timeout"] = self.on_timeout
        g["timeout_s"] = self.confirm_timeout_s
        g["driver_response"] = self.driver_response
        return g

    # ------------------------------------------------------------ GPS

    def _update_gps(self, state, pos, ts) -> dict:
        if state.gps_ok:
            self.gps.last_fix = GpsFix(
                latitude=pos.latitude,
                longitude=pos.longitude,
                accuracy_m=GPS_ACCURACY_M,
                speed_kmh=state.speed,
                heading_deg=pos.heading_deg % 360.0,
                timestamp=ts,
                source="SIMULATED",
            )
            status = "connected"
        else:
            status = "disconnected" if self.gps.last_fix else "unavailable"

        fix = self.gps.last_fix
        if fix is None:
            return {"status": status, "live": False, "latitude": None, "longitude": None,
                    "speed_kmh": None, "heading_deg": None, "accuracy_m": None, "age_s": None}
        return {
            "status": status,
            "live": status == "connected",    # موقعیت قدیمی هرگز «زنده» نیست
            "latitude": fix.latitude,
            "longitude": fix.longitude,
            "speed_kmh": fix.speed_kmh if status == "connected" else None,
            "heading_deg": fix.heading_deg,
            "accuracy_m": fix.accuracy_m,
            "age_s": round(fix.age_s(ts), 1),
        }

    def _gps_status_alert(self, gps_info: dict, t: float, ts) -> dict | None:
        """وضعیت GPS: نه خروجی Detector؛ با source=simulator_status مشخص می‌شود."""
        key = "gps:signal"
        if gps_info["status"] == "connected":
            if self._gps_lost_since is not None:
                self._gps_lost_since = None
                self.alerts.pop(key, None)
            return None
        if self._gps_lost_since is not None:
            return None
        self._gps_lost_since = t
        alert = {
            "key": key, "source": "simulator_status", "detector": None,
            "event_type": "gps_signal_lost", "severity": "warning",
            "message": "GPS signal lost; showing last valid position (not live).",
            "first_t": t, "timestamp": ts.isoformat(), "count": 1,
        }
        self.alerts[key] = alert
        return alert

    # ------------------------------------------------------------ هشدارها

    def _alert_key(self, e: DetectionEvent) -> str:
        if e.detector == "trouble_codes":
            return f"trouble_codes:{e.details.get('code')}"
        if e.detector == "stoppage":
            return f"stoppage:{e.event_type}"
        return e.detector       # coolant/voltage: یک هشدار فعال برای هر Detector

    def _register_event(self, e: DetectionEvent, t: float) -> None:
        d = e.model_dump(mode="json")
        d["sim_t"] = t
        self.events.append(d)

        key = (e.detector, e.event_type)
        match = next((x for x in self.expected if x.key == key), None)
        if match is not None:
            self._detected.setdefault(key, t)
        else:
            self._unexpected.append(d)

        akey = self._alert_key(e)
        cur = self.alerts.get(akey)
        if cur is None:
            self.alerts[akey] = {
                "key": akey, "source": "detector", "detector": e.detector,
                "event_type": e.event_type, "severity": e.severity.value,
                "message": e.message, "first_t": t,
                "timestamp": e.timestamp.isoformat(), "count": 1,
            }
            return
        cur["count"] += 1
        # فقط تشدید جایگزین می‌شود؛ تنزل شدت هشدار متناقض نشان نمی‌دهد
        if SEVERITY_RANK[e.severity.value] >= SEVERITY_RANK[cur["severity"]]:
            cur.update(severity=e.severity.value, event_type=e.event_type,
                       message=e.message, timestamp=e.timestamp.isoformat())

    def active_alerts(self) -> list[dict]:
        return sorted(
            (dict(a) for a in self.alerts.values()),
            key=lambda a: (-SEVERITY_RANK[a["severity"]], a["first_t"]),
        )

    def danger_level(self) -> str:
        top = max((SEVERITY_RANK[a["severity"]] for a in self.alerts.values()), default=-1)
        return {-1: "normal", 0: "info", 1: "warning", 2: "critical"}[top]

    # ------------------------------------------------------------ خلاصه

    def summary(self) -> dict:
        results = []
        for x in self.expected:
            at = self._detected.get(x.key)
            results.append({
                "detector": x.detector,
                "event_type": x.event_type,
                "fault_time_s": x.fault_time_s,
                "detected": at is not None,
                "detected_at_s": at,
                "detection_latency_s": None if at is None else round(at - x.fault_time_s, 1),
            })
        responses = [r for r in self.emergency_log if r["phase"] == "initial"]
        if self.expected:
            verdict = (
                "detected_by_detector"
                if all(r["detected"] for r in results)
                else "missed_by_detector"
            )
        elif self.spec.coverage == "status_only":
            verdict = "status_only_no_detector"
        else:
            verdict = "false_alarm" if self._unexpected else "no_events_as_expected"
        crash = None
        if self.responder is not None:
            gate = self.gate_snapshot()
            crash = {
                "gate_state": gate["state"], "alerts_released": gate["alerts_released"],
                "driver_response": self.driver_response, "on_timeout": self.on_timeout,
            }
        return {
            "crash": crash,
            "verdict": verdict,
            "scenario": self.spec.name,
            "coverage": self.spec.coverage,
            "coverage_note": self.spec.coverage_note,
            "frames": len(self.frames),
            "simulated_seconds": self.t,
            "events": len(self.events),
            "expected": results,
            "verified_by_detector": bool(self.expected) and all(r["detected"] for r in results),
            "false_alarms": len(self._unexpected),
            "unexpected_events": self._unexpected,
            "validation_errors": self.validation_errors,
            "emergency_responses": len(self.emergency_log),
            "first_emergency_generation_ms": (
                responses[0]["generation_ms"] if responses else None
            ),
        }
