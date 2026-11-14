# app/simulation/lab_engine.py
"""
موتور آزمایشگاه شبیه‌سازی (ساعت مجازی، قابل‌تکرار).

مسیر داده دقیقاً مسیر واقعی پروژه است:
    داده‌ی خام OBD ← DataValidator ← VehicleRecord(source="SIMULATOR")
    ← DetectorEngine (Detectorهای واقعی) ← RoadsideEmergencyService

هیچ رویدادی مستقیم ساخته نمی‌شود. تنها استثنا وضعیت GPS است که Detector ندارد و
با منبع `simulator_status` و بدون ادعای تشخیص Detector علامت‌گذاری می‌شود.
"""

import time
from datetime import datetime, timedelta, timezone

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

SOURCE_NAME = "SIMULATOR"
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
        self.profile = get_vehicle_profile(vehicle_id)
        self.start_time = start_time or datetime.now(timezone.utc)

        config, self.expected = self.spec.build(self.event_time_s)
        self.model = DrivingModel(config, seed)
        self.validator = DataValidator(self.profile)
        self.detectors = build_detector_engine()
        self.gps = RouteGpsSource()
        self._current_road = self.route.road_types[0]
        self.roads = road_analyzer or SimulatedRoadAnalyzer(lambda: self._current_road)

        self._responses: list = []
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
        new_responses: list[dict] = []
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
                started = time.perf_counter()
                before = len(self._responses)
                self.emergency.handle(event)
                elapsed_ms = (time.perf_counter() - started) * 1000
                for resp in self._responses[before:]:
                    d = resp.model_dump(mode="json")
                    d["generation_ms"] = round(elapsed_ms, 3)
                    d["sim_t"] = t
                    new_responses.append(d)
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
        }
        self.frames.append(frame)
        self.t += DT_S
        return frame

    def run_to_end(self) -> list[dict]:
        while not self.finished:
            self.step()
        return self.frames

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
        return {
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
