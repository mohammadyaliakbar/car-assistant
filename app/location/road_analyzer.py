"""
تحلیل جاده‌ی اطراف خودرو با OpenStreetMap (Overpass API).

اصول:
- اطلاعات OSM ممکن است ناقص باشند. نبودِ تگ به معنی نبودِ محدودیت یا
  خطر نیست؛ پس فیلدها فقط True یا None می‌شوند، هیچ‌وقت False.
- هر خطای شبکه، timeout یا پاسخ ناقص یعنی RoadContext.unavailable
  (استثنا بیرون نمی‌آید).
- نزدیک‌ترین راه همیشه راهِ درست نیست. اگر راهی پرسرعت در محدوده‌ی
  خطای GPS باشد، محافظه‌کارانه همان انتخاب می‌شود.
- این ماژول لاین یا شانه را تشخیص نمی‌دهد و ادعا هم نمی‌کند.
"""

import logging
import math
import re
import time
from collections import OrderedDict
from functools import partial
from typing import Any, Callable

from app.location.models import GpsFix, RoadContext

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "SmartCarAssistant/0.1 (development)"

EARTH_RADIUS_M = 6_371_000.0

# راه‌های قابل‌عبور برای خودرو. footway, path, cycleway و ... عمداً نیستند.
ROAD_TYPES = frozenset({
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service", "track", "road",
})
FAST_TYPES = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})
NON_WINDING_JUNCTIONS = frozenset({"roundabout", "circular"})

FetchFunction = Callable[[str, float], dict]


# ---------------------------------------------------------------- شبکه

def overpass_fetch(query: str, timeout_s: float, url: str = OVERPASS_URL) -> dict:
    """درخواست واقعی به Overpass. در تست‌ها استفاده نمی‌شود."""
    import requests  # import تنبل: تست‌ها به نصب requests نیاز ندارند

    response = requests.post(
        url,
        data={"data": query},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout_s,
    )
    response.raise_for_status()
    return response.json()


def build_query(
    lat: float,
    lon: float,
    radius_m: float,
    bay_radius_m: float,
    server_timeout_s: int = 10,
) -> str:
    road_regex = "|".join(sorted(ROAD_TYPES))
    point = f"{lat:.6f},{lon:.6f}"
    return (
        f"[out:json][timeout:{server_timeout_s}];\n"
        f"is_in({point})->.areas;\n"
        "(\n"
        f'  way(around:{radius_m:.0f},{point})[highway~"^({road_regex})$"];\n'
        f"  node(around:{bay_radius_m:.0f},{point})[highway=emergency_bay];\n"
        "  way(pivot.areas)[amenity=parking];\n"
        f"  way(around:{bay_radius_m:.0f},{point})[highway=emergency_bay];\n"
        ");\n"
        "out geom;"
    )


# ---------------------------------------------------------------- هندسه

Point = tuple[float, float]


def _project(lat: float, lon: float, lat0: float, lon0: float) -> Point:
    """تبدیل به متر نسبت به نقطه‌ی مبدأ (تقریب محلی، مناسب چند صد متر)."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def _distance_to_origin(points: list[Point]) -> float:
    """کمترین فاصله‌ی مبدأ تا یک نقطه یا خط شکسته."""
    if len(points) == 1:
        return math.hypot(*points[0])
    best = math.inf
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 == 0:
            d = math.hypot(ax, ay)
        else:
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            d = math.hypot(ax + t * dx, ay + t * dy)
        best = min(best, d)
    return best


def _ring_contains_origin(points: list[Point]) -> bool:
    """آیا مبدأ داخل چندضلعی بسته است؟ (ray casting)"""
    if len(points) < 4 or points[0] != points[-1]:
        return False
    inside = False
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if (y1 > 0) != (y2 > 0):
            x_cross = x1 + (0 - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > 0:
                inside = not inside
    return inside


def _turn_deg(p0: Point, p1: Point, p2: Point) -> float:
    ax, ay = p1[0] - p0[0], p1[1] - p0[1]
    bx, by = p2[0] - p1[0], p2[1] - p1[1]
    if (ax == 0 and ay == 0) or (bx == 0 and by == 0):
        return 0.0
    return abs(math.degrees(math.atan2(ax * by - ay * bx, ax * bx + ay * by)))


def _is_winding(
    points: list[Point],
    threshold_deg_per_km: float,
    window_m: float = 300.0,
    min_window_m: float = 200.0,
    min_turn_deg: float = 10.0,
    min_turns: int = 3,
) -> bool:
    """
    تخمین ضعیف و فقط هندسی: مجموع چرخش مسیر در بازه‌ی حدود ±۳۰۰ متر
    از نزدیک‌ترین رأس. فقط True یا False برمی‌گرداند و نتیجه‌ی False
    به معنی «پیچ‌دار نیست» نیست (ممکن است نقشه با جزئیات کم رسم شده باشد).
    """
    if len(points) < 4:
        return False

    s = [0.0]
    for a, b in zip(points, points[1:]):
        s.append(s[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))

    nearest = min(range(len(points)), key=lambda i: math.hypot(*points[i]))
    lo = min(i for i in range(len(points)) if s[i] >= s[nearest] - window_m)
    hi = max(i for i in range(len(points)) if s[i] <= s[nearest] + window_m)

    length = s[hi] - s[lo]
    if length < min_window_m:
        return False

    turns = [
        _turn_deg(points[i - 1], points[i], points[i + 1])
        for i in range(lo + 1, hi)
    ]
    sharp = [t for t in turns if t >= min_turn_deg]
    if len(sharp) < min_turns:
        return False

    return sum(turns) / (length / 1000.0) >= threshold_deg_per_km


# ---------------------------------------------------------------- تگ‌ها

_MAXSPEED_RE = re.compile(r"(\d{1,3})(?:\s*(km/h|kmh|kph|mph))?")


def parse_maxspeed(value: Any) -> float | None:
    """'50' یا '60 mph' را به km/h تبدیل می‌کند. مقدارهای دیگر (none, signals, ...) نامعلوم‌اند."""
    if not isinstance(value, str):
        return None
    match = _MAXSPEED_RE.fullmatch(value.strip().lower())
    if not match:
        return None
    speed = float(match.group(1))
    if match.group(2) == "mph":
        speed *= 1.609344
    if not 0 < speed <= 200:
        return None
    return round(speed, 1)


def _parse_lanes(value: Any) -> int | None:
    if isinstance(value, str) and value.strip().isdigit():
        lanes = int(value.strip())
        if 1 <= lanes <= 12:
            return lanes
    return None


def _no_stopping(tags: dict) -> bool | None:
    """True فقط وقتی صریحاً ثبت شده؛ در غیر این‌صورت None (نه False)."""
    for key, value in tags.items():
        if (
            isinstance(key, str)
            and key.startswith("parking:")
            and isinstance(value, str)
            and value.lower() == "no_stopping"
        ):
            return True
    return None


def _is_fast(tags: dict) -> bool:
    return tags.get("highway") in FAST_TYPES or tags.get("motorroad") == "yes"


def _base_type(highway: str) -> str:
    return highway.removesuffix("_link")


# ---------------------------------------------------------------- تحلیل‌گر

class RoadAnalyzer:
    def __init__(
        self,
        fetch: FetchFunction | None = None,
        url: str = OVERPASS_URL,
        timeout_s: float = 8.0,
        search_radius_m: float = 60.0,
        max_search_radius_m: float = 150.0,
        bay_radius_m: float = 300.0,
        cache_ttl_s: float = 600.0,
        cache_size: int = 64,
        winding_deg_per_km: float = 250.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch or partial(overpass_fetch, url=url)
        self.timeout_s = timeout_s
        self.search_radius_m = search_radius_m
        self.max_search_radius_m = max_search_radius_m
        self.bay_radius_m = bay_radius_m
        self.cache_ttl_s = cache_ttl_s
        self.cache_size = cache_size
        self.winding_deg_per_km = winding_deg_per_km
        self._clock = clock
        self._cache: OrderedDict[tuple, tuple[float, RoadContext]] = OrderedDict()

    # --- API عمومی ---

    def analyze(self, fix: GpsFix | None) -> RoadContext:
        if fix is None:
            return RoadContext.unavailable("موقعیت GPS در دسترس نیست.")

        radius = min(
            max(self.search_radius_m, fix.accuracy_m or 0.0),
            self.max_search_radius_m,
        )
        # شبکه‌ی حدود ۱۱ متری؛ فقط برای جلوگیری از درخواست تکراری وقتی خودرو ایستاده
        key = (round(fix.latitude, 4), round(fix.longitude, 4), round(radius))

        cached = self._cache_get(key)
        if cached is not None:
            return cached

        query = build_query(
            fix.latitude, fix.longitude, radius, self.bay_radius_m,
            server_timeout_s=int(self.timeout_s) + 2,
        )
        try:
            data = self._fetch(query, self.timeout_s)
        except Exception as exc:  # شبکه، timeout، HTTP، JSON، نبودن requests
            logger.warning("Overpass request failed: %r", exc)
            return RoadContext.unavailable(
                f"دریافت اطلاعات نقشه ناموفق بود ({type(exc).__name__})."
            )

        context = self.parse_response(data, fix, radius)
        if context.data_quality != "unavailable":
            self._cache_put(key, context)  # شکست هرگز کش نمی‌شود
        return context

    def parse_response(
        self,
        data: Any,
        fix: GpsFix,
        radius_m: float | None = None,
    ) -> RoadContext:
        radius = radius_m or self.search_radius_m

        if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
            return RoadContext.unavailable("پاسخ نقشه معتبر نبود.")
        if data.get("remark"):
            # Overpass در timeout یا کمبود حافظه، پاسخ ناقص با remark می‌دهد
            return RoadContext.unavailable("سرور نقشه پاسخ ناقص یا خطا برگرداند.")

        roads: list[tuple[float, dict, list[Point]]] = []
        bay_distances: list[float] = []
        inside_parking = False

        for element in data["elements"]:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags")
            if not isinstance(tags, dict):
                tags = {}
            points = self._points(element, fix)
            if not points:
                continue
            kind = element.get("type")

            if tags.get("highway") == "emergency_bay":
                bay_distances.append(_distance_to_origin(points))
            elif kind == "way" and tags.get("highway") in ROAD_TYPES:
                roads.append((_distance_to_origin(points), tags, points))
            elif kind == "way" and tags.get("amenity") == "parking":
                if _ring_contains_origin(points):
                    inside_parking = True

        notes = [
            "اطلاعات نقشه ممکن است ناقص باشد؛ نبود تگ به معنی نبود "
            "محدودیت یا خطر نیست."
        ]
        quality = "ok"
        chosen: tuple[float, dict, list[Point]] | None = None

        roads.sort(key=lambda r: r[0])
        accuracy = fix.accuracy_m if fix.accuracy_m is not None else 50.0
        ambiguity = min(max(accuracy, 25.0), radius)

        if not roads:
            quality = "partial"
            notes.append("راه شناخته‌شده‌ای در اطراف روی نقشه پیدا نشد.")
        elif roads[0][0] > ambiguity:
            quality = "partial"
            notes.append(
                f"نزدیک‌ترین راه حدود {roads[0][0]:.0f} متر فاصله دارد و از "
                "خطای GPS دورتر است؛ نوع جاده تعیین نشد."
            )
        else:
            nearby = [r for r in roads if r[0] <= ambiguity]
            chosen = nearby[0]

            fast = [r for r in nearby if _is_fast(r[1])]
            if fast and not _is_fast(chosen[1]):
                chosen = fast[0]
                quality = "partial"
                notes.append(
                    "یک جاده‌ی پرسرعت در محدوده‌ی خطای GPS است؛ "
                    "محافظه‌کارانه همان در نظر گرفته شد."
                )

            if len({_base_type(r[1]["highway"]) for r in nearby}) > 1:
                quality = "partial"
                notes.append(
                    "چند نوع راه در محدوده‌ی خطای GPS هست؛ راه واقعی نامطمئن است."
                )

            notes.append(
                f"فاصله تا خط مرکزی راه روی نقشه حدود {chosen[0]:.0f} متر است؛ "
                "موقعیت دقیق نسبت به مسیر عبور مشخص نیست."
            )

        fields: dict[str, Any] = {}
        if chosen is not None:
            _, tags, points = chosen
            fields = {
                "highway_type": tags["highway"],
                "is_limited_access": True if _is_fast_limited(tags) else None,
                "maxspeed_kmh": parse_maxspeed(tags.get("maxspeed")),
                "lane_count": _parse_lanes(tags.get("lanes")),
                "no_stopping_tagged": _no_stopping(tags),
            }
            if tags.get("junction") not in NON_WINDING_JUNCTIONS and _is_winding(
                points, self.winding_deg_per_km
            ):
                fields["is_winding"] = True

        if bay_distances:
            fields["has_emergency_bay_nearby"] = True
            fields["emergency_bay_distance_m"] = round(min(bay_distances), 1)

        if inside_parking:
            fields["is_parking_area"] = True

        return RoadContext(data_quality=quality, notes=notes, **fields)

    # --- کمکی ---

    @staticmethod
    def _points(element: dict, fix: GpsFix) -> list[Point]:
        if element.get("type") == "node":
            raw = [element]
        else:
            raw = element.get("geometry") or []
        points: list[Point] = []
        for p in raw:
            if isinstance(p, dict) and "lat" in p and "lon" in p:
                points.append(
                    _project(p["lat"], p["lon"], fix.latitude, fix.longitude)
                )
        return points

    def _cache_get(self, key: tuple) -> RoadContext | None:
        item = self._cache.get(key)
        if item is None:
            return None
        stored_at, context = item
        if self._clock() - stored_at > self.cache_ttl_s:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return context

    def _cache_put(self, key: tuple, context: RoadContext) -> None:
        self._cache[key] = (self._clock(), context)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)


def _is_fast_limited(tags: dict) -> bool:
    """محدودالدسترس فقط وقتی مطمئنیم: motorway یا motorroad=yes. trunk به‌تنهایی نه."""
    return tags.get("highway") in {"motorway", "motorway_link"} or (
        tags.get("motorroad") == "yes"
    )