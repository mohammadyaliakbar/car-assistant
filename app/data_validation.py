from dataclasses import dataclass
from typing import Any
import math

from pydantic import ValidationError

from app.data_models import VehicleData
from app.vehicle_profiles import VehicleProfile


@dataclass
class ValidationReport:
    data: VehicleData | None
    errors: list[str]
    warnings: list[str]


class DataValidator:
    def __init__(self, profile: VehicleProfile):
        self.profile = profile

    def validate(self, raw_data: dict[str, Any]) -> ValidationReport:
        errors = []
        warnings = []

        try:
            data = VehicleData.model_validate(raw_data)
        except ValidationError as exc:
            return ValidationReport(
                data=None,
                errors=[str(error) for error in exc.errors()],
                warnings=[],
            )

        # بررسی دور موتور
        if data.rpm is not None:
            if not math.isfinite(data.rpm) or data.rpm < 0:
                errors.append("RPM must be a finite non-negative number.")
            elif data.rpm > self.profile.max_rpm:
                warnings.append(
                    f"RPM exceeds configured maximum "
                    f"({self.profile.max_rpm})."
                )

        # بررسی سرعت
        if data.speed is not None:
            if not math.isfinite(data.speed) or data.speed < 0:
                errors.append("Speed must be a finite non-negative number.")
            elif data.speed > self.profile.max_speed_kmh:
                warnings.append(
                    f"Speed exceeds configured maximum "
                    f"({self.profile.max_speed_kmh} km/h)."
                )

        # بررسی دمای مایع خنک‌کننده
        if data.coolant_temperature_c is not None:
            temp = data.coolant_temperature_c

            if not math.isfinite(temp):
                errors.append("Coolant temperature must be finite.")
            elif not (
                self.profile.min_coolant_temp_c
                <= temp
                <= self.profile.max_coolant_temp_c
            ):
                warnings.append(
                    "Coolant temperature is outside the configured range."
                )

        # بررسی بار موتور
        if data.engine_load_percent is not None:
            load = data.engine_load_percent

            if not math.isfinite(load) or not (
                self.profile.min_engine_load_percent
                <= load
                <= self.profile.max_engine_load_percent
            ):
                warnings.append(
                    "Engine load is outside the configured range."
                )

        # بررسی موقعیت دریچه گاز
        if data.throttle_position_percent is not None:
            throttle = data.throttle_position_percent

            if not math.isfinite(throttle) or not (
                self.profile.min_throttle_position_percent
                <= throttle
                <= self.profile.max_throttle_position_percent
            ):
                warnings.append(
                    "Throttle position is outside the configured range."
                )

        return ValidationReport(
            data=data,
            errors=errors,
            warnings=warnings,
        )