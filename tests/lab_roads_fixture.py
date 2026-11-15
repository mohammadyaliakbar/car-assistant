"""دیتابیس جاده‌ی مصنوعی برای تست مسیریابی: شبکه‌ی شطرنجی با یک رودخانه و فقط یک پل."""

from app.location.offline_provider import write_database

LAT0, LON0, STEP = 35.70, 51.30, 0.002
RIVER_COL, BRIDGE_ROW = 30, 10


def make_grid_db(path, n=60):
    feats = []
    oid = [1]

    def way(points, highway):
        feats.append({"osm_type": "way", "osm_id": oid[0], "kind": "road",
                      "tags": {"highway": highway}, "points": points})
        oid[0] += 1

    for r in range(n):
        lat = round(LAT0 + r * STEP, 7)
        row = [(lat, round(LON0 + c * STEP, 7)) for c in range(n)]
        if r == BRIDGE_ROW:
            way(row, "secondary")                       # تنها پل: ردیف ۱۰ پیوسته است
        else:
            highway = "motorway" if r == 0 else "residential"
            way(row[:RIVER_COL], highway)               # رودخانه: ردیف‌ها در ستون ۳۰ قطع‌اند
            way(row[RIVER_COL:], highway)
    for c in range(n):
        lon = round(LON0 + c * STEP, 7)
        way([(round(LAT0 + r * STEP, 7), lon) for r in range(n)], "residential")
    # میان‌بر service روی رودخانه (ردیف ۴۰، دور از پل): اگر service مسیریابی شود، دور زدن از پل لازم نمی‌شود
    way([(round(LAT0 + 40 * STEP, 7), round(LON0 + 29 * STEP, 7)),
         (round(LAT0 + 40 * STEP, 7), round(LON0 + 30 * STEP, 7))], "service")
    way([(36.00, 51.50), (36.00, 51.51), (36.01, 51.51)], "residential")   # جزیره‌ی جداافتاده
    return write_database(path, feats, {"bbox": [51.0, 35.5, 51.8, 36.2]})


def node(row, col):
    return (round(LAT0 + row * STEP, 7), round(LON0 + col * STEP, 7))
