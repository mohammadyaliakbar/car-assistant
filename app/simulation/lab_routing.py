# app/simulation/lab_routing.py
"""
مسیریابی آفلاین روی جاده‌های دیتابیس پروژه (`data/roads_tehran.sqlite`) و نمایش
جاده‌ها روی نقشه بدون نیاز به اینترنت.

دیتابیس هندسه‌ی کامل هر جاده را دارد و گره‌های مشترک OSM مختصات یکسان دارند؛ پس
گراف مسیریابی از همان نقطه‌ها ساخته می‌شود. محدودیت‌های شناخته‌شده:
- برچسب `oneway` و محدودیت‌های گردش در دیتابیس نیست؛ جهت یک‌طرفه رعایت نمی‌شود.
- `service` و `track` در مسیریابی نیستند (ورودی پارکینگ میان‌بر جعلی می‌سازد).
- هزینه‌ی هر یال فقط از نوع جاده می‌آید (نه ترافیک یا maxspeed).
"""

import heapq
import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.location.offline_provider import OfflineRoadProvider
from app.simulation.routes import haversine_m

logger = logging.getLogger(__name__)

# سرعت فرضی (km/h) برای هزینه‌ی زمانی هر نوع جاده
SPEED_KMH = {
    "motorway": 100, "motorway_link": 50, "trunk": 80, "trunk_link": 50,
    "primary": 60, "primary_link": 40, "secondary": 50, "secondary_link": 40,
    "tertiary": 40, "tertiary_link": 30, "unclassified": 35, "residential": 30,
    "living_street": 10,
}
FAST_CLASSES = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})
# لایه‌بندی برای نمایش: مهم‌ترین‌ها اول
DISPLAY_TIERS = (
    ("motorway", "motorway_link", "trunk", "trunk_link"),
    ("primary", "primary_link", "secondary", "secondary_link"),
    ("tertiary", "tertiary_link", "unclassified"),
    ("residential", "living_street", "service", "track", "road"),
)
MARGINS_M = (1500.0, 4000.0, 12000.0)       # گسترش تدریجی محدوده‌ی جست‌وجو
MAX_ROUTE_FEATURES = 400_000
MAX_SNAP_M = 400.0
M_PER_DEG = 111_194.9
V_MAX_MS = 100 / 3.6


class RoutingError(Exception):
    """مسیریابی ممکن نشد؛ پیام برای کاربر قابل‌نمایش است."""


@dataclass(frozen=True)
class PlannedRoute:
    points: list
    road_types: list
    length_m: float
    snapped_start: tuple
    snapped_dest: tuple


def road_type_of(highway: str) -> str:
    """نوع جاده‌ی شبیه‌ساز: فقط آزادراه/بزرگراه «highway» است؛ بقیه شهری فرض می‌شوند."""
    return "highway" if highway in FAST_CLASSES else "urban"


def _dist(a, b) -> float:
    """فاصله‌ی سریع و تقریبی (متر) برای فاصله‌های کوتاه."""
    dy = (b[0] - a[0]) * M_PER_DEG
    dx = (b[1] - a[1]) * M_PER_DEG * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def _like(classes) -> tuple[str, list]:
    clause = " OR ".join("f.tags LIKE ?" for _ in classes)
    return f"({clause})", [f'%"highway": "{c}"%' for c in classes]


class RoadRouter:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._provider = OfflineRoadProvider(self.db_path)

    @property
    def available(self) -> bool:
        return self.db_path.is_file()

    # ------------------------------------------------------------ دیتابیس

    def _query(self, south, west, north, east, classes, limit=None) -> list[tuple[str, str]]:
        clause, params = _like(classes)
        sql = (
            "SELECT f.tags, f.geom FROM features_rtree r JOIN features f ON f.id = r.id "
            "WHERE f.kind = 'road' AND r.min_lat <= ? AND r.max_lat >= ? "
            f"AND r.min_lon <= ? AND r.max_lon >= ? AND {clause}"
        )
        args = [north, south, east, west, *params]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        conn = self._provider._connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            raise RoutingError(f"خواندن دیتابیس جاده ممکن نشد: {exc}") from exc
        finally:
            conn.close()
        return [(json.loads(t).get("highway", ""), g) for t, g in rows]

    def _coverage(self, *points) -> None:
        bbox = self._provider.meta().get("bbox")
        if not bbox:
            return
        min_lon, min_lat, max_lon, max_lat = bbox
        for lat, lon in points:
            if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                raise RoutingError("نقطه خارج از محدوده‌ی نقشه‌ی آفلاین پروژه است.")

    # ------------------------------------------------------------ نمایش جاده‌ها

    def roads_in_bbox(self, south, west, north, east, zoom: int, limit: int = 8000) -> dict:
        """جاده‌های ناحیه برای رسم روی نقشه؛ مهم‌ترین‌ها اول، با سقف تعداد."""
        if not self.available:
            raise RoutingError("دیتابیس آفلاین جاده پیدا نشد.")
        tiers = 0 if zoom < 12 else 2 if zoom == 12 else 3 if zoom == 13 else 4
        out, truncated = [], False
        for tier in DISPLAY_TIERS[:tiers]:
            room = limit - len(out)
            if room <= 0:
                truncated = True
                break
            rows = self._query(south, west, north, east, tier, room + 1)
            if len(rows) > room:
                rows, truncated = rows[:room], True
            for hw, geom in rows:
                pts = json.loads(geom)
                out.append({"t": hw, "p": [[round(p[0], 5), round(p[1], 5)] for p in pts]})
        return {"features": out, "truncated": truncated, "zoom_needed": zoom < 12}

    # ------------------------------------------------------------ مسیریابی

    def plan(self, start: tuple[float, float], dest: tuple[float, float]) -> PlannedRoute:
        if not self.available:
            raise RoutingError("دیتابیس آفلاین جاده (data/roads_tehran.sqlite) پیدا نشد.")
        for lat, lon in (start, dest):
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise RoutingError("مختصات نامعتبر است.")
        if haversine_m(start, dest) < 20:
            raise RoutingError("مبدأ و مقصد تقریباً یکی‌اند؛ مقصد را دورتر انتخاب کنید.")
        self._coverage(start, dest)

        classes = tuple(SPEED_KMH)
        last_error = "مسیری روی جاده‌های دیتابیس بین این دو نقطه پیدا نشد."
        direct = haversine_m(start, dest)
        for margin in MARGINS_M:
            m = max(margin, 0.35 * direct if margin > MARGINS_M[0] else margin)
            dlat = m / M_PER_DEG
            dlon = m / (M_PER_DEG * max(math.cos(math.radians(start[0])), 0.01))
            south, north = min(start[0], dest[0]) - dlat, max(start[0], dest[0]) + dlat
            west, east = min(start[1], dest[1]) - dlon, max(start[1], dest[1]) + dlon
            rows = self._query(south, west, north, east, classes, MAX_ROUTE_FEATURES + 1)
            if len(rows) > MAX_ROUTE_FEATURES:
                raise RoutingError("ناحیه‌ی مسیر بسیار بزرگ است؛ مبدأ و مقصد را نزدیک‌تر انتخاب کنید.")
            if not rows:
                last_error = "در اطراف این نقاط جاده‌ای در دیتابیس نیست."
                continue
            adj, segs = self._graph(rows)
            try:
                return self._search(adj, segs, start, dest)
            except RoutingError as exc:
                last_error = str(exc)
        raise RoutingError(last_error)

    @staticmethod
    def _graph(rows):
        adj: dict[tuple, list] = {}
        segs: list[tuple] = []
        for hw, geom in rows:
            speed = SPEED_KMH.get(hw, 30) / 3.6
            pts = [(round(p[0], 7), round(p[1], 7)) for p in json.loads(geom)]
            for a, b in zip(pts, pts[1:]):
                if a == b:
                    continue
                d = _dist(a, b)
                adj.setdefault(a, []).append((b, d, d / speed, hw))
                adj.setdefault(b, []).append((a, d, d / speed, hw))
                segs.append((a, b, hw))
        return adj, segs

    @staticmethod
    def _snap(segs, point, k=3):
        """k نزدیک‌ترین قطعه (با نقطه‌ی تصویر روی قطعه)؛ فقط تا MAX_SNAP_M."""
        lat0, lon0 = point
        cos0 = math.cos(math.radians(lat0))

        def candidates():
            for a, b, hw in segs:
                ax, ay = (a[1] - lon0) * cos0, a[0] - lat0
                bx, by = (b[1] - lon0) * cos0, b[0] - lat0
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
                px, py = ax + t * dx, ay + t * dy
                d = math.hypot(px, py) * M_PER_DEG
                if d <= MAX_SNAP_M:
                    yield d, t, a, b, hw

        return heapq.nsmallest(k, candidates(), key=lambda c: c[0])

    def _search(self, adj, segs, start, dest) -> PlannedRoute:
        starts, dests = self._snap(segs, start), self._snap(segs, dest)
        if not starts:
            raise RoutingError("مبدأ از نزدیک‌ترین جاده بیش از ۴۰۰ متر فاصله دارد؛ نزدیک یک جاده کلیک کنید.")
        if not dests:
            raise RoutingError("مقصد از نزدیک‌ترین جاده بیش از ۴۰۰ متر فاصله دارد؛ نزدیک یک جاده کلیک کنید.")

        pairs = sorted(((s, d) for s in starts for d in dests), key=lambda sd: sd[0][0] + sd[1][0])
        for s, d in pairs:
            result = self._route_between(adj, s, d)
            if result is not None:
                return result
        raise RoutingError("مسیری روی جاده‌های دیتابیس بین این دو نقطه پیدا نشد (جاده‌ها به هم وصل نیستند).")

    @staticmethod
    def _virtual(adj, cand):
        """نقطه‌ی تصویر را به‌صورت گره‌ی مجازی به دو سر قطعه وصل می‌کند."""
        _, t, a, b, hw = cand
        if t <= 1e-9:
            return a, []
        if t >= 1 - 1e-9:
            return b, []
        p = (round(a[0] + (b[0] - a[0]) * t, 7), round(a[1] + (b[1] - a[1]) * t, 7))
        return p, [(a, b, hw, t)]

    def _route_between(self, adj, s, d):
        extra: dict[tuple, list] = {}

        def link(u, v, hw):
            dist = _dist(u, v)
            speed = SPEED_KMH.get(hw, 30) / 3.6
            extra.setdefault(u, []).append((v, dist, dist / speed, hw))
            extra.setdefault(v, []).append((u, dist, dist / speed, hw))

        s_node, s_info = self._virtual(adj, s)
        d_node, d_info = self._virtual(adj, d)
        virt: dict[tuple, list] = {}
        for node, info in ((s_node, s_info), (d_node, d_info)):
            for a, b, hw, t in info:
                virt.setdefault((a, b, hw), []).append((t, node))
        for (a, b, hw), items in virt.items():
            chain = [a] + [n for _, n in sorted(set(items))] + [b]
            for u, v in zip(chain, chain[1:]):
                if u != v:
                    link(u, v, hw)

        def neighbors(node):
            yield from adj.get(node, ())
            yield from extra.get(node, ())

        if s_node == d_node:
            return None
        goal = d_node
        best = {s_node: 0.0}
        prev: dict[tuple, tuple] = {}
        heap = [(0.0, s_node)]
        while heap:
            f, node = heapq.heappop(heap)
            if node == goal:
                break
            g = best[node]
            if f - _dist(node, goal) / V_MAX_MS > g + 1e-6:
                continue
            for nbr, length, cost, hw in neighbors(node):
                ng = g + cost
                if ng < best.get(nbr, math.inf):
                    best[nbr] = ng
                    prev[nbr] = (node, hw)
                    heapq.heappush(heap, (ng + _dist(nbr, goal) / V_MAX_MS, nbr))
        if goal not in prev:
            return None

        nodes, classes = [goal], []
        while nodes[-1] != s_node:
            p, hw = prev[nodes[-1]]
            classes.append(hw)
            nodes.append(p)
        nodes.reverse()
        classes.reverse()

        points, types = [nodes[0]], []
        for node, hw in zip(nodes[1:], classes):
            if haversine_m(points[-1], node) <= 0.5:
                continue                                  # هم‌ترازی با Route: نقطه‌ی تکراری حذف می‌شود
            points.append(node)
            types.append(road_type_of(hw))
        if len(points) < 2:
            return None
        length = sum(haversine_m(a, b) for a, b in zip(points, points[1:]))
        return PlannedRoute([list(p) for p in points], types, length, s_node, d_node)
