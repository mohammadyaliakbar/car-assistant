# app/simulation/scenarios.py
"""
هشت سناریوی آزمایشگاه شبیه‌سازی.

هر سناریو فقط «داده‌ی خام OBD» تولید می‌کند؛ هیچ رویدادی مستقیم ساخته نمی‌شود.
تشخیص را Detectorهای واقعی پروژه انجام می‌دهند. `expected` می‌گوید کدام
تشخیص از Detector انتظار می‌رود تا گزارش بتواند «تشخیص واقعی» را از «فقط تولید
داده» جدا کند.
"""

import math
import random
from dataclasses import dataclass, field

ENGINE_ON_RPM = 500.0  # هم‌خوان با آستانه‌ی Detectorهای پروژه (فقط برای نمایش)

DTC_DESCRIPTIONS = {
    "P0300": "Random/multiple cylinder misfire detected",
    "P0301": "Cylinder 1 misfire detected",
    "P0171": "System too lean (bank 1)",
    "P0420": "Catalyst system efficiency below threshold (bank 1)",
}

TARGET_SPEED = {"urban": 42.0, "highway": 98.0, "rural": 68.0}
ACCEL_KMH_PER_S = 3.0
BRAKE_KMH_PER_S = 5.0
COAST_DECEL_KMH_PER_S = 1.6   # خودرو بعد از خاموشی موتور


@dataclass(frozen=True)
class ExpectedDetection:
    detector: str
    event_type: str
    fault_time_s: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.detector, self.event_type)


@dataclass(frozen=True)
class ModelConfig:
    stall_at: float | None = None
    overheat_at: float | None = None
    voltage_drop_at: float | None = None
    dtc_events: tuple[tuple[float, str], ...] = ()
    gps_loss: tuple[float, float] | None = None   # (شروع، پایان)
    road_dependent_speed: bool = False


@dataclass(frozen=True)
class VState:
    speed: float
    rpm: float
    coolant: float
    voltage: float
    dtc: tuple[str, ...]
    gps_ok: bool

    @property
    def engine_on(self) -> bool:
        return self.rpm >= ENGINE_ON_RPM


class DrivingModel:
    """مدل پویای خودرو. با seed ثابت، کاملاً تکرارپذیر است."""

    def __init__(self, config: ModelConfig, seed: int = 1):
        self.cfg = config
        self.seed = seed
        self.speed = 0.0
        self._stall_rpm: float | None = None
        self._coolant_at_heat: float | None = None

    def _noise(self, channel: str, t: float, amp: float) -> float:
        return random.Random(f"{self.seed}:{channel}:{int(t)}").uniform(-amp, amp)

    def _base_coolant(self, t: float) -> float:
        return 85.0 + 5.0 * (1.0 - math.exp(-t / 30.0))

    def advance(self, t: float, road_type: str) -> VState:
        c = self.cfg
        stalled = c.stall_at is not None and t >= c.stall_at

        if stalled:
            self.speed = max(0.0, self.speed - COAST_DECEL_KMH_PER_S)
            if self._stall_rpm is None:
                self._stall_rpm = 2600.0
            rpm = self._stall_rpm * math.exp(-(t - c.stall_at) / 1.5)
            rpm = 0.0 if rpm < 100 else rpm
        else:
            target = TARGET_SPEED[road_type if c.road_dependent_speed else "highway"]
            target += 5.0 * math.sin(t / 17.0) + self._noise("spd", t, 1.5)
            delta = target - self.speed
            self.speed += max(-BRAKE_KMH_PER_S, min(ACCEL_KMH_PER_S, delta))
            self.speed = max(0.0, self.speed)
            rpm = 800.0 + 22.0 * self.speed + self._noise("rpm", t, 40.0)

        engine_on = rpm >= ENGINE_ON_RPM

        coolant = self._base_coolant(t) + self._noise("cool", t, 0.4)
        if c.overheat_at is not None and t >= c.overheat_at:
            if self._coolant_at_heat is None:
                self._coolant_at_heat = self._base_coolant(t)
            coolant = min(122.0, self._coolant_at_heat + 0.35 * (t - c.overheat_at))
            coolant += self._noise("cool", t, 0.2)

        if not engine_on:
            voltage = 12.4
        elif c.voltage_drop_at is not None and t >= c.voltage_drop_at:
            voltage = max(11.0, 14.1 - 0.05 * (t - c.voltage_drop_at))
        else:
            voltage = 14.1 + self._noise("volt", t, 0.05)

        dtc = tuple(code for at, code in c.dtc_events if t >= at)

        gps_ok = True
        if c.gps_loss is not None and c.gps_loss[0] <= t < c.gps_loss[1]:
            gps_ok = False

        return VState(self.speed, rpm, coolant, voltage, dtc, gps_ok)


def obd_frame(s: VState) -> dict:
    """هم‌قالب `OBDClient.read_vehicle_data()` (همه‌ی ۱۰ کلید)."""
    on = s.engine_on
    return {
        "rpm": float(s.rpm),
        "speed": float(s.speed),
        "coolant_temperature_c": float(s.coolant),
        "engine_load_percent": 15.0 + 0.4 * s.speed if on else 0.0,
        "throttle_position_percent": 10.0 + 0.3 * s.speed if on else 0.0,
        "intake_temperature_c": 35.0,
        "intake_pressure_kpa": 30.0 + 0.5 * s.speed if on else 100.0,
        "trouble_codes": list(s.dtc),
        "diagnostic_status": {"mil_on": bool(s.dtc), "dtc_count": len(s.dtc)},
        "control_module_voltage_v": float(s.voltage),
    }


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    title_fa: str
    title_en: str
    description: str
    route: str
    duration_s: float
    event_time_s: float
    coverage: str            # detector | negative | status_only
    coverage_note: str
    _build: object = field(repr=False, default=None)

    def build(self, event_time_s: float | None = None):
        t0 = self.event_time_s if event_time_s is None else event_time_s
        return self._build(t0)


def _spec(name, fa, en, desc, route, duration, t0, coverage, note, build):
    return ScenarioSpec(name, fa, en, desc, route, duration, t0, coverage, note, build)


def _normal(t0):
    return ModelConfig(), ()


def _stoppage(t0):
    return (
        ModelConfig(stall_at=t0),
        (ExpectedDetection("stoppage", "engine_stalled_while_moving", t0),),
    )


def _overheat(t0):
    return (
        ModelConfig(overheat_at=t0),
        (ExpectedDetection("coolant_overheat", "engine_overheat", t0),),
    )


def _voltage(t0):
    return (
        ModelConfig(voltage_drop_at=t0),
        (ExpectedDetection("voltage", "low_charging_voltage", t0),),
    )


def _dtc(t0):
    return (
        ModelConfig(dtc_events=((t0, "P0301"),)),
        (ExpectedDetection("trouble_codes", "new_trouble_code", t0),),
    )


def _gps_loss(t0):
    return ModelConfig(gps_loss=(t0, t0 + 60.0)), ()


def _multi(t0):
    return (
        ModelConfig(
            overheat_at=t0,
            voltage_drop_at=t0 + 10.0,
            dtc_events=((t0 + 20.0, "P0300"),),
        ),
        (
            ExpectedDetection("coolant_overheat", "engine_overheat", t0),
            ExpectedDetection("voltage", "low_charging_voltage", t0 + 10.0),
            ExpectedDetection("trouble_codes", "new_trouble_code", t0 + 20.0),
        ),
    )


def _road(t0):
    return ModelConfig(road_dependent_speed=True), ()


LAB_SCENARIOS: dict[str, ScenarioSpec] = {
    s.name: s
    for s in (
        _spec("normal_driving", "رانندگی عادی", "Normal Driving",
              "رانندگی عادی بدون خرابی؛ انتظار می‌رود هیچ رویدادی ثبت نشود.",
              "tehran_karaj_highway", 240, 0, "negative",
              "هر رویداد تشخیص‌داده‌شده هشدار اشتباه حساب می‌شود.", _normal),
        _spec("highway_stoppage", "خاموشی موتور در بزرگراه", "Engine Stoppage on Highway",
              "موتور در سرعت بزرگراه خاموش می‌شود و خودرو با سرعت کم‌شونده می‌ایستد.",
              "tehran_karaj_highway", 170, 45, "detector",
              "StoppageDetector و سپس RoadsideEmergencyService.", _stoppage),
        _spec("coolant_overheat", "داغ‌شدن مایع خنک‌کننده", "Coolant Overheating",
              "دمای مایع خنک‌کننده تدریجی بالا می‌رود (هشدار و بحرانی).",
              "tehran_karaj_highway", 200, 45, "detector",
              "CoolantOverheatDetector با آستانه‌های پیش‌فرض پروژه.", _overheat),
        _spec("battery_drop", "افت ولتاژ باتری", "Battery Voltage Drop",
              "ولتاژ شارژ با موتور روشن تدریجی افت می‌کند.",
              "tehran_karaj_highway", 160, 45, "detector",
              "VoltageDetector (low_charging_voltage).", _voltage),
        _spec("ecu_trouble_code", "کد خطای ECU", "ECU Trouble Code",
              "کد خطای P0301 در میانه‌ی رانندگی ظاهر می‌شود.",
              "tehran_karaj_highway", 120, 45, "detector",
              "TroubleCodeDetector.", _dtc),
        _spec("gps_signal_loss", "قطع سیگنال GPS", "GPS Signal Loss",
              "سیگنال GPS برای ۶۰ ثانیه قطع و سپس برقرار می‌شود.",
              "tehran_karaj_highway", 180, 45, "status_only",
              "پروژه Detector مخصوص GPS ندارد؛ فقط وضعیت داشبورد است، نه تشخیص Detector.",
              _gps_loss),
        _spec("multiple_failures", "چند خرابی هم‌زمان", "Multiple Simultaneous Failures",
              "داغ‌شدن موتور، افت ولتاژ و کد خطای ECU پشت‌سرهم رخ می‌دهند.",
              "tehran_karaj_highway", 220, 45, "detector",
              "سه Detector هم‌زمان؛ هشدارها بدون تناقض ادغام می‌شوند.", _multi),
        _spec("road_changes", "تغییر شرایط مسیر", "Road Condition Changes",
              "شهری ← بزرگراه ← جاده‌ی خارج شهر با سرعت متناسب با هر نوع جاده.",
              "mixed_urban_highway_rural", 480, 0, "negative",
              "بدون خرابی؛ فقط نوع جاده و سرعت تغییر می‌کند.", _road),
    )
}


def get_scenario(name: str) -> ScenarioSpec:
    try:
        return LAB_SCENARIOS[name]
    except KeyError:
        raise ValueError(
            f"سناریوی ناشناخته: {name!r}. گزینه‌ها: {', '.join(LAB_SCENARIOS)}"
        ) from None
