from datetime import datetime,timezone

from pydantic import BaseModel, Field

class vehicleData(BaseModel):
    rpm: float | None = None
    speed: float | None = None
    coolant_temperature_c: float | None = None

class vehicleRecord(BaseModel):
    vehicle_id: str
    source: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    data: vehicleData
    