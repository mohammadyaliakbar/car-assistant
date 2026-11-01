from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GpsFix(BaseModel):
    """یک نمونه موقعیت. فیلدهای None یعنی نامعلوم (نه صفر)."""

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    speed_kmh: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    timestamp: datetime
    source: str  # مثلاً "PHONE_GPS" یا "SIMULATED"

    @field_validator("timestamp")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware.")
        return value

    def age_s(self, now: datetime | None = None) -> float:
        """سن نمونه به ثانیه."""
        now = now or datetime.now(timezone.utc)
        return (now - self.timestamp).total_seconds()


class RoadContext(BaseModel):
    """
    اطلاعات جاده از نقشه.

    قاعده: None یعنی «نامعلوم». نبودن تگ در OpenStreetMap هرگز
    به معنی نبود محدودیت یا نبود خطر نیست.
    """

    model_config = ConfigDict(frozen=True)

    highway_type: str | None = None            # motorway, trunk, primary, ...
    is_limited_access: bool | None = None      # آزادراه/بزرگراه با دسترسی محدود
    maxspeed_kmh: float | None = None
    has_emergency_bay_nearby: bool | None = None
    emergency_bay_distance_m: float | None = None
    no_stopping_tagged: bool | None = None
    lane_count: int | None = None
    is_winding: bool | None = None             # تخمین هندسی پیچ‌دار بودن
    data_quality: Literal["ok", "partial", "unavailable"] = "unavailable"
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def unavailable(cls, reason: str) -> "RoadContext":
        return cls(data_quality="unavailable", notes=[reason])