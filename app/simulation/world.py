# app/simulation/world.py
"""
دنیای شبیه‌سازی‌شده: یک خط زمان که سه نما می‌سازد و با هم هماهنگ‌اند:
- داده‌ی OBD (همان قالب `OBDClient.read_vehicle_data()`)
- موقعیت GPS
- حسگرهای گوشی (شتاب‌سنج/ژیروسکوپ)

همه چیز در همین پردازش تولید می‌شود و از مسیر واقعی پروژه (DataValidator،
DetectorEngine، ...) عبور می‌کند. مقادیر ساختگی‌اند و جای داده‌ی واقعی را
نمی‌گیرند. موقعیت GPS ثابت است؛ فقط سرعت و جهت از سناریو می‌آید.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.motion.simulator import MotionSimulator
from app.motion.source import SimulatedMotionSource

OBD_FIELDS = (
    "rpm", "speed", "coolant_temperature_c", "engine_load_percent",
    "throttle_position_percent", "intake_temperature_c", "intake_pressure_kpa",
    "trouble_codes", "diagnostic_status", "control_module_voltage_v",
)

ENGINE_ON_RPM = 500.0  # هم‌خوان با Detectorها


@dataclass(frozen=True)
class ScenarioDef:
    description: str
    motion: str                       # سناریوی متناظر در MotionSimulator
    coolant: str = "normal"           # normal | overheat
    voltage: str = "normal"           # normal | low_charging
    dtc: str = "none"                 # none | p0300
    obd_available: bool = True        # False: همه‌ی فیلدهای OBD نامعلوم (None)


SCENARIOS: dict[str, ScenarioDef] = {
    "normal": ScenarioDef("رانندگی عادی، بدون رویداد", "normal_driving"),
    "overheat": ScenarioDef("داغ‌شدن مایع خنک‌کننده (هشدار و بحرانی)", "normal_driving", coolant="overheat"),
    "engine_stall": ScenarioDef("خاموش‌شدن موتور هنگام حرکت (بدون تصادف)", "engine_stall"),
    "trouble_code": ScenarioDef("ظهور کد خطای جدید P0300", "normal_driving", dtc="p0300"),
    "low_voltage": ScenarioDef("ولتاژ شارژ پایین", "normal_driving", voltage="low_charging"),
    "hard_brake": ScenarioDef("ترمز شدید، بدون تصادف", "hard_brake"),
    "pothole": ScenarioDef("دست‌انداز، بدون تصادف", "pothole"),
    "phone_drop": ScenarioDef("افتادن گوشی (نباید تصادف تشخیص داده شود)", "phone_drop"),
    "crash": ScenarioDef("تصادف: ضربه، افت سرعت، خاموشی موتور", "crash"),
    "crash_engine_running": ScenarioDef("تصادف و موتور روشن می‌ماند", "crash_engine_running"),
    "crash_phone_only": ScenarioDef("تصادف وقتی OBD در دسترس نیست (فقط حسگر گوشی)", "crash", obd_available=False),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SimulatedWorld:
    def __init__(
        self,
        scenario: str,
        clock: Callable[[], datetime] = _utc_now,
        event_time_s: float = 15.0,
        latitude: float = 35.6892,
        longitude: float = 51.3890,
        gps_mode: str = "good",
        duration_s: float = 3600.0,
        seed: int = 1,
    ):
        if scenario not in SCENARIOS:
            raise ValueError(
                f"سناریوی ناشناخته: {scenario!r}. گزینه‌ها: {', '.join(SCENARIOS)}"
            )
        self.scenario = scenario
        self.definition = SCENARIOS[scenario]
        self.event_time_s = event_time_s
        self.latitude = latitude
        self.longitude = longitude
        self.gps_mode = gps_mode
        self.duration_s = duration_s
        self.seed = seed
        self._clock = clock
        self.start = clock()

        sim = MotionSimulator(
            start=self.start, duration_s=duration_s, event_time_s=event_time_s, seed=seed
        )
        self._speed, self._rpm = sim.vehicle_profile(self.definition.motion)

    # ------------------------------------------------------------ زمان

    def elapsed_s(self) -> float:
        return (self._clock() - self.start).total_seconds()

    @property
    def finished(self) -> bool:
        return self.elapsed_s() >= self.duration_s

    def speed_kmh(self) -> float:
        return self._speed(self.elapsed_s())

    # ------------------------------------------------------------ OBD

    def obd_snapshot(self) -> dict:
        """هم‌قالب `OBDClient.read_vehicle_data()`؛ مقدار نامعلوم None است."""
        d = self.definition
        if not d.obd_available:
            return {field: None for field in OBD_FIELDS}

        t = self.elapsed_s()
        t0 = self.event_time_s
        speed = self._speed(t)
        rpm = self._rpm(t)
        engine_on = rpm >= ENGINE_ON_RPM

        coolant = 90.0
        if d.coolant == "overheat" and t >= t0:
            coolant = min(125.0, 90.0 + 1.2 * (t - t0))

        if not engine_on:
            voltage = 12.4
        elif d.voltage == "low_charging" and t >= t0:
            voltage = 12.2
        else:
            voltage = 14.1

        codes = ["P0300"] if d.dtc == "p0300" and t >= t0 else []

        return {
            "rpm": float(rpm),
            "speed": float(speed),
            "coolant_temperature_c": coolant,
            "engine_load_percent": 15.0 + 0.4 * speed if engine_on else 0.0,
            "throttle_position_percent": 10.0 + 0.3 * speed if engine_on else 0.0,
            "intake_temperature_c": 35.0,
            "intake_pressure_kpa": 30.0 + 0.5 * speed if engine_on else 100.0,
            "trouble_codes": codes,
            "diagnostic_status": {"mil_on": bool(codes), "dtc_count": len(codes)},
            "control_module_voltage_v": voltage,
        }

    # ------------------------------------------------------------ حسگر گوشی

    def motion_source(self) -> SimulatedMotionSource:
        """منبع حسگر گوشی؛ سرعت/دور موتور فقط از مسیر OBD می‌رسد."""
        return SimulatedMotionSource(
            self.definition.motion,
            clock=self._clock,
            start=self.start,
            emit_vehicle_signals=False,
            duration_s=self.duration_s,
            event_time_s=self.event_time_s,
            seed=self.seed,
        )
