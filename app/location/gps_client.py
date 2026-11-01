from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from app.location.models import GpsFix


class GpsSource(ABC):
    @abstractmethod
    def get_fix(self) -> GpsFix | None:
        """آخرین موقعیت را برمی‌گرداند؛ None یعنی موقعیتی در دسترس نیست."""

    def close(self) -> None:
        pass


class SimulatedGpsSource(GpsSource):
    """
    منبع GPS شبیه‌سازی‌شده برای تست.

    سناریوها:
      good           دقت خوب و نمونه تازه
      poor_accuracy  دقت ضعیف (۱۵۰ متر)
      stale          نمونه قدیمی (۲ دقیقه پیش)
      no_fix         موقعیتی در دسترس نیست

    مختصات پیش‌فرض فقط یک نقطه‌ی نمونه است و به جاده‌ی خاصی ربط ندارد.
    """

    SCENARIOS = ("good", "poor_accuracy", "stale", "no_fix")

    def __init__(
        self,
        scenario: str = "good",
        latitude: float = 35.6892,
        longitude: float = 51.3890,
        speed_kmh: float | None = 0.0,
        heading_deg: float | None = 90.0,
    ):
        if scenario not in self.SCENARIOS:
            raise ValueError(f"Unknown GPS scenario: {scenario}")
        self.scenario = scenario
        self.latitude = latitude
        self.longitude = longitude
        self.speed_kmh = speed_kmh
        self.heading_deg = heading_deg

    def get_fix(self) -> GpsFix | None:
        if self.scenario == "no_fix":
            return None

        now = datetime.now(timezone.utc)
        accuracy = 5.0
        timestamp = now

        if self.scenario == "poor_accuracy":
            accuracy = 150.0
        elif self.scenario == "stale":
            timestamp = now - timedelta(seconds=120)

        return GpsFix(
            latitude=self.latitude,
            longitude=self.longitude,
            accuracy_m=accuracy,
            speed_kmh=self.speed_kmh,
            heading_deg=self.heading_deg,
            timestamp=timestamp,
            source="SIMULATED",
        )