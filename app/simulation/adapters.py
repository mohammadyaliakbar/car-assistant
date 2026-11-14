# app/simulation/adapters.py
"""
آداپتورهایی که دنیای شبیه‌سازی را با واسط‌های واقعی پروژه یکی می‌کنند، تا بقیه‌ی
برنامه فرق شبیه‌ساز و دستگاه واقعی را نبیند:

- SimulatedOBDClient: همان واسط `OBDClient` (read_vehicle_data / close)
- WorldGpsSource:     همان واسط `GpsSource` (get_fix / close)
"""

from app.location.gps_client import GpsSource, SimulatedGpsSource
from app.location.models import GpsFix
from app.simulation.world import SimulatedWorld


class SimulatedOBDClient:
    def __init__(self, world: SimulatedWorld):
        self.world = world

    def read_vehicle_data(self) -> dict:
        return self.world.obd_snapshot()

    @property
    def finished(self) -> bool:
        return self.world.finished

    def close(self) -> None:
        pass


class WorldGpsSource(GpsSource):
    """
    موقعیت ثابت با سرعت متناسب با سناریو. حالت‌های دقت/تازگی/نبودن موقعیت از
    `SimulatedGpsSource` می‌آیند (good, poor_accuracy, stale, no_fix).
    """

    def __init__(self, world: SimulatedWorld):
        self.world = world
        self._base = SimulatedGpsSource(
            scenario=world.gps_mode,
            latitude=world.latitude,
            longitude=world.longitude,
        )

    def get_fix(self) -> GpsFix | None:
        fix = self._base.get_fix()
        if fix is None:
            return None
        return fix.model_copy(update={"speed_kmh": self.world.speed_kmh()})
