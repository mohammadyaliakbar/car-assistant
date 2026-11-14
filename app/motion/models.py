"""
مدل داده‌ی حسگرهای حرکتی گوشی (شتاب‌سنج و ژیروسکوپ).

اصل `None` یعنی نامعلوم اینجا هم برقرار است: ژیروسکوپ اختیاری است و
نبودنش به معنی «چرخش صفر» نیست.
"""

import math
from dataclasses import dataclass
from datetime import datetime

G = 9.80665  # m/s²


@dataclass(frozen=True)
class MotionSample:
    """
    یک نمونه‌ی حسگر.

    accel_*: شتاب بر حسب m/s² در دستگاه مختصات گوشی. فرض پیش‌فرض این
             است که گرانش هم در آن هست (مثل TYPE_ACCELEROMETER اندروید).
    gyro_*:  سرعت زاویه‌ای بر حسب rad/s؛ یا هر سه مقدار یا هیچ‌کدام.
    """

    timestamp: datetime
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float | None = None
    gyro_y: float | None = None
    gyro_z: float | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp باید timezone-aware باشد.")
        for name in ("accel_x", "accel_y", "accel_z"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} باید عدد متناهی باشد.")
        gyro = (self.gyro_x, self.gyro_y, self.gyro_z)
        if any(v is None for v in gyro) and any(v is not None for v in gyro):
            raise ValueError("مقادیر ژیروسکوپ باید هر سه با هم بیایند یا هیچ‌کدام.")
        if all(v is not None for v in gyro) and not all(math.isfinite(v) for v in gyro):
            raise ValueError("مقادیر ژیروسکوپ باید متناهی باشند.")

    @property
    def accel_magnitude_ms2(self) -> float:
        return math.sqrt(self.accel_x**2 + self.accel_y**2 + self.accel_z**2)

    @property
    def accel_magnitude_g(self) -> float:
        return self.accel_magnitude_ms2 / G

    @property
    def gyro_magnitude_rad_s(self) -> float | None:
        if self.gyro_x is None:
            return None
        return math.sqrt(self.gyro_x**2 + self.gyro_y**2 + self.gyro_z**2)
