from datetime import datetime

from app.data_models import VehicleRecord
from app.detectors.event_detector import (
    BaseDetector,
    DetectionEvent,
    Severity,
)


class StoppageDetector(BaseDetector):
    name = "stoppage"

    def __init__(
        self,
        engine_on_rpm: float = 500,
        moving_speed_kmh: float = 10,
        long_idle_s: float = 300,
    ):
        self.engine_on_rpm = engine_on_rpm
        self.moving_speed_kmh = moving_speed_kmh
        self.long_idle_s = long_idle_s
        self.reset()

    def reset(self) -> None:
        self._prev_speed: float | None = None
        self._prev_rpm: float | None = None
        self._idle_since: datetime | None = None
        self._long_idle_reported = False

    def process(self, record: VehicleRecord) -> list[DetectionEvent]:
        speed = record.data.speed
        rpm = record.data.rpm
        now = record.timestamp

        if speed is None or rpm is None:
            return []           # نامعلوم؛ وضعیت قبلی را نگه می‌داریم

        prev_speed, prev_rpm = self._prev_speed, self._prev_rpm
        self._prev_speed, self._prev_rpm = speed, rpm

        events: list[DetectionEvent] = []

        # موتور در حین حرکت خاموش شد
        if (
            prev_speed is not None
            and prev_rpm is not None
            and prev_speed >= self.moving_speed_kmh
            and prev_rpm >= self.engine_on_rpm
            and rpm < self.engine_on_rpm
        ):
            events.append(
                self._event(
                    record,
                    "engine_stalled_while_moving",
                    Severity.WARNING,
                    "Engine stopped while the vehicle was moving.",
                    {"previous_speed_kmh": prev_speed, "rpm": rpm},
                )
            )

        # ایستاده با موتور روشن
        idle = speed < 1 and rpm >= self.engine_on_rpm
        if idle:
            if self._idle_since is None:
                self._idle_since = now
                self._long_idle_reported = False
            elif (
                not self._long_idle_reported
                and (now - self._idle_since).total_seconds() >= self.long_idle_s
            ):
                self._long_idle_reported = True
                events.append(
                    self._event(
                        record,
                        "long_idle",
                        Severity.INFO,
                        f"Engine has been idling for over "
                        f"{self.long_idle_s:.0f} seconds.",
                        {"idle_seconds": (now - self._idle_since).total_seconds()},
                    )
                )
        else:
            self._idle_since = None
            self._long_idle_reported = False

        return events

    def _event(self, record, event_type, severity, message, details):
        return DetectionEvent(
            detector=self.name,
            event_type=event_type,
            severity=severity,
            message=message,
            vehicle_id=record.vehicle_id,
            timestamp=record.timestamp,
            details=details,
        )