from app.logger import get_logger
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.data_models import VehicleRecord

logger = get_logger(__name__)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DetectionEvent(BaseModel):
    detector: str
    event_type: str
    severity: Severity
    message: str
    vehicle_id: str
    timestamp: datetime
    details: dict = Field(default_factory=dict)


class BaseDetector(ABC):
    name: str = "base"

    @abstractmethod
    def process(self, record: VehicleRecord) -> list[DetectionEvent]:
        """یک رکورد خودرو را بررسی کرده و رویدادهای تشخیص‌داده‌شده را برمی‌گرداند."""

    def reset(self) -> None:
        """وضعیت داخلی Detector را در صورت نیاز بازنشانی می‌کند."""


class DetectorEngine:
    def __init__(self, detectors: list[BaseDetector]):
        self.detectors = detectors

    def process(self, record: VehicleRecord) -> list[DetectionEvent]:
        events: list[DetectionEvent] = []

        for detector in self.detectors:
            try:
                detected_events = detector.process(record)
                events.extend(detected_events)

            except Exception:
                # خطای یک Detector نباید مانع اجرای بقیه شود.
                logger.exception("Detector %s failed.", detector.name)

        return events

    def reset(self) -> None:
        for detector in self.detectors:
            detector.reset()
