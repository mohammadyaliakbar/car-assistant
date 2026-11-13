"""
تشخیص علت timeout/خطای Overpass. از ریشه‌ی پروژه اجرا شود:

    python check_overpass.py

برای هر سرور، دو query را با همان User-Agent و همان تابع `overpass_fetch`
پروژه امتحان می‌کند:
  - سبک: فقط راه‌های اطراف (بدون is_in و پارکینگ)
  - کامل: همان query واقعی پروژه (`build_query`)
"""

import time

import requests

from app.location.road_analyzer import (
    ROAD_TYPES,
    USER_AGENT,
    build_query,
    overpass_fetch,
)

LAT, LON = 35.7000, 51.4000  # مختصات آزمایشی؛ در صورت تمایل عوض کنید
TIMEOUT_S = 30

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

ROAD_REGEX = "|".join(sorted(ROAD_TYPES))
LIGHT_QUERY = (
    "[out:json][timeout:10];\n"
    f'way(around:80,{LAT:.6f},{LON:.6f})[highway~"^({ROAD_REGEX})$"];\n'
    "out geom;"
)
FULL_QUERY = build_query(LAT, LON, 80, 300)


def probe(url: str, query: str) -> str:
    start = time.monotonic()
    try:
        data = overpass_fetch(query, TIMEOUT_S, url=url)
    except requests.HTTPError as exc:
        return f"{time.monotonic() - start:5.1f}s  HTTP {exc.response.status_code}"
    except Exception as exc:
        return f"{time.monotonic() - start:5.1f}s  {type(exc).__name__}"

    n = len(data.get("elements", []))
    remark = f"  remark: {data['remark'][:60]}" if data.get("remark") else ""
    return f"{time.monotonic() - start:5.1f}s  OK, {n} عنصر{remark}"


print(f"User-Agent: {USER_AGENT}\n")
for url in ENDPOINTS:
    print(url)
    print("  سبک :", probe(url, LIGHT_QUERY))
    print("  کامل:", probe(url, FULL_QUERY))
print("\nپایان.")
