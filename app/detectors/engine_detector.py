from app.data_models import VehicleRecord
from app.detectors.event_detector import (
    BaseDetector,
    DetectionEvent,
    Severity,
)


    

class CoolantOverheatDetector(BaseDetector):
    name = "coolant_overheat"

    def __init__(
        self,
        warning_c: float = 105,
        critical_c: float = 115,
        hysteresis_c: float = 5,
        required_samples: int = 3,
    ):
        self.warning_c = warning_c
        self.critical_c = critical_c
        self.hysteresis_c = hysteresis_c
        self.required_samples = required_samples
        self.reset()

    def reset(self) -> None:
        self._level: Severity | None = None      # وضعیت اعلام‌شده فعلی
        self._candidate: Severity | None = None  # وضعیت در حال بررسی
        self._count = 0

    def _classify(self, temp: float) -> Severity | None:
        if temp >= self.critical_c:
            return Severity.CRITICAL
        if temp >= self.warning_c:
            return Severity.WARNING
        # برای خروج از هشدار، دما باید کمی زیر آستانه بیاید
        if self._level is not None and temp > self.warning_c - self.hysteresis_c:
            return self._level
        return None

    def process(self, record: VehicleRecord) -> list[DetectionEvent]:
        temp = record.data.coolant_temperature_c
        if temp is None:        # نامعلوم؛ وضعیت را تغییر نده
            return []

        level = self._classify(temp)

        if level == self._candidate:
            self._count += 1
        else:
            self._candidate = level
            self._count = 1

        if self._count < self.required_samples or level == self._level:
            return []

        self._level = level
        if level is None:
            return []           # برگشت به حالت عادی

        return [
            DetectionEvent(
                detector=self.name,
                event_type="engine_overheat",
                severity=level,
                message=f"Coolant temperature is high: {temp:.0f} °C",
                vehicle_id=record.vehicle_id,
                timestamp=record.timestamp,
                details={"coolant_temperature_c": temp},
            )
        ]


class TroubleCodeDetector(BaseDetector):
    name = "trouble_codes"

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._known: set[str] = set()

    def process(self, record: VehicleRecord) -> list[DetectionEvent]:
        codes = record.data.trouble_codes
        if codes is None:       # None = نامعلوم، [] = بدون خطا
            return []

        current = set(codes)
        new_codes = sorted(current - self._known)
        self._known = current

        return [
            DetectionEvent(
                detector=self.name,
                event_type="new_trouble_code",
                severity=Severity.WARNING,
                message=f"New trouble code detected: {code}",
                vehicle_id=record.vehicle_id,
                timestamp=record.timestamp,
                details={"code": code},
            )
            for code in new_codes
        ]


class VoltageDetector(BaseDetector):
    """ولتاژ را بسته به روشن‌بودن موتور بررسی می‌کند."""

    name = "voltage"

    def __init__(
        self,
        engine_on_rpm: float = 500,
        charging_min_v: float = 13.0,
        charging_max_v: float = 15.2,
        battery_min_v: float = 11.8,
    ):
        self.engine_on_rpm = engine_on_rpm
        self.charging_min_v = charging_min_v
        self.charging_max_v = charging_max_v
        self.battery_min_v = battery_min_v
        self.reset()

    def reset(self) -> None:
        self._active: str | None = None

    def process(self, record: VehicleRecord) -> list[DetectionEvent]:
        d = record.data
        if d.control_module_voltage_v is None or d.rpm is None:
            return []           # بدون دانستن وضعیت موتور نمی‌شود قضاوت کرد

        v = d.control_module_voltage_v
        engine_on = d.rpm >= self.engine_on_rpm

        problem = None
        sev = Severity.WARNING
        if engine_on and v < self.charging_min_v:
            problem = "low_charging_voltage"
        elif engine_on and v > self.charging_max_v:
            problem, sev = "over_voltage", Severity.CRITICAL
        elif not engine_on and v < self.battery_min_v:
            problem = "low_battery"

        if problem == self._active:
            return []
        self._active = problem
        if problem is None:
            return []

        return [
            DetectionEvent(
                detector=self.name,
                event_type=problem,
                severity=sev,
                message=f"Abnormal voltage ({problem}): {v:.1f} V",
                vehicle_id=record.vehicle_id,
                timestamp=record.timestamp,
                details={"voltage_v": v, "rpm": d.rpm},
            )
        ]