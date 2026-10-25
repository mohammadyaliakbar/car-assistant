from datetime import datetime, timezone

from pydantic import BaseModel, Field


class VehicleData(BaseModel):
    # اطلاعات اصلی خودرو
    rpm: float | None = None
    speed: float | None = None
    coolant_temperature_c: float | None = None

    # وضعیت کارکرد موتور
    engine_load_percent: float | None = None
    throttle_position_percent: float | None = None
    intake_temperature_c: float | None = None
    intake_pressure_kpa: float | None = None

    # اطلاعات عیب‌یابی
    trouble_codes: list[str] | None = None
    diagnostic_status: dict | None = None
    control_module_voltage_v: float | None = None


class VehicleRecord(BaseModel):
    vehicle_id: str
    source: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    data: VehicleData