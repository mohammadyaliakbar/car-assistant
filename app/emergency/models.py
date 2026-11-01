from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        """ترتیب شدت. UNKNOWN عمداً -1 است و با سطح‌های دیگر مقایسه نمی‌شود."""
        return {"low": 0, "medium": 1, "high": 2, "critical": 3, "unknown": -1}[
            self.value
        ]


class RiskAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)  # دلایل، برای شفافیت
    confidence: Literal["low", "medium", "high"] = "low"


class TriggerConditions(BaseModel):
    event_type: str
    road_types: list[str] | None = None   # None = هر نوع جاده
    min_risk: RiskLevel | None = None


class Instruction(BaseModel):
    id: str
    title: str
    headline: str                 # یک خط کوتاه، مناسب اعلان/TTS
    trigger_conditions: TriggerConditions
    risk_level: RiskLevel
    priority: int = 0
    steps: list[str]
    warnings: list[str] = Field(default_factory=list)
    status: Literal["draft", "reviewed"] = "draft"


class EmergencyContact(BaseModel):
    name: str
    number: str


class EmergencyReport(BaseModel):
    """اطلاعات آماده‌ی ارسال به امداد (بدون تماس خودکار)."""

    latitude: float | None
    longitude: float | None
    gps_accuracy_m: float | None
    road_type: str | None
    heading_deg: float | None
    event_type: str
    vehicle: str                  # مثلاً "Saipa Pride"
    stopped_at: datetime
    risk_level: RiskLevel
    risk_reasons: list[str]
    instruction_id: str | None
    contacts: list[EmergencyContact]