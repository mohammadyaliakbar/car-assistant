# app/simulation/routes.py
"""
مسیرهای آزمایشی GPS.

مختصات، نقاط تقریبیِ آزمایشی در اطراف تهران و کرج‌اند، نه هندسه‌ی دقیق جاده.
هدف فقط حرکت قابل‌مشاهده‌ی خودرو روی نقشه است. نوع جاده (urban/highway/rural)
برای هر قطعه جداگانه تعیین می‌شود تا سناریوی «تغییر شرایط مسیر» ممکن شود.
"""

import bisect
import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0
ROAD_TYPES = ("urban", "highway", "rural")


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360.0


@dataclass(frozen=True)
class RoutePosition:
    latitude: float
    longitude: float
    heading_deg: float
    road_type: str
    distance_m: float
    arrived: bool


class Route:
    def __init__(
        self,
        name: str,
        title: str,
        points: list[tuple[float, float]],
        road_types: list[str] | str = "highway",
    ):
        for lat, lon in points:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"مختصات نامعتبر: {(lat, lon)}")
        # نقاط تکراری (قطعه‌ی با طول صفر) حذف می‌شوند
        pts = [points[0]]
        for p in points[1:]:
            if haversine_m(pts[-1], p) > 0.5:
                pts.append(p)
        if len(pts) < 2:
            raise ValueError("مسیر دست‌کم به دو نقطه‌ی متمایز نیاز دارد.")

        segments = len(pts) - 1
        if isinstance(road_types, str):
            road_types = [road_types] * segments
        if len(road_types) != segments:
            raise ValueError("تعداد نوع جاده‌ها باید با تعداد قطعه‌ها برابر باشد.")
        for rt in road_types:
            if rt not in ROAD_TYPES:
                raise ValueError(f"نوع جاده‌ی ناشناخته: {rt!r}")

        self.name = name
        self.title = title
        self.points = pts
        self.road_types = list(road_types)
        self._cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            self._cum.append(self._cum[-1] + haversine_m(a, b))
        self.length_m = self._cum[-1]

    @classmethod
    def from_points(
        cls,
        start: tuple[float, float],
        destination: tuple[float, float],
        road_type: str = "highway",
        name: str = "custom",
    ) -> "Route":
        """مسیر مستقیم بین مبدأ و مقصد (انتخاب دستی از روی نقشه)."""
        return cls(name, "مسیر انتخاب‌شده توسط کاربر", [start, destination], road_type)

    def position_at(self, distance_m: float) -> RoutePosition:
        d = max(0.0, min(distance_m, self.length_m))
        i = min(bisect.bisect_right(self._cum, d) - 1, len(self.points) - 2)
        a, b = self.points[i], self.points[i + 1]
        seg_len = self._cum[i + 1] - self._cum[i]
        f = (d - self._cum[i]) / seg_len if seg_len else 0.0
        return RoutePosition(
            latitude=a[0] + (b[0] - a[0]) * f,
            longitude=a[1] + (b[1] - a[1]) * f,
            heading_deg=bearing_deg(a, b),
            road_type=self.road_types[i],
            distance_m=d,
            arrived=distance_m >= self.length_m,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "length_m": round(self.length_m, 1),
            "points": [list(p) for p in self.points],
            "road_types": self.road_types,
        }


ROUTES: dict[str, Route] = {
    r.name: r
    for r in (
        Route(
            "tehran_karaj_highway",
            "تهران ← کرج (بزرگراه، مختصات تقریبی)",
            [
                (35.6997, 51.3380), (35.7050, 51.2800), (35.7130, 51.2200),
                (35.7250, 51.1500), (35.7380, 51.0800), (35.7600, 51.0300),
                (35.8000, 50.9950),
            ],
            "highway",
        ),
        Route(
            "mixed_urban_highway_rural",
            "شهری ← بزرگراه ← جاده‌ی خارج شهر (مختصات تقریبی)",
            [
                (35.7150, 51.4090), (35.7120, 51.4000), (35.7060, 51.3930),
                (35.7020, 51.3650), (35.7030, 51.3300),
                (35.7080, 51.3050), (35.7130, 51.2800),
            ],
            ["urban", "urban", "highway", "highway", "rural", "rural"],
        ),
    )
}


def get_route(name: str) -> Route:
    try:
        return ROUTES[name]
    except KeyError:
        raise ValueError(f"مسیر ناشناخته: {name!r}. گزینه‌ها: {', '.join(ROUTES)}") from None
