# app/simulation/lab_report.py
"""گزارش ارزیابی عملکرد (به تفکیک سناریو) و خروجی CSV."""

import csv
import io
from statistics import mean

RUN_COLUMNS = [
    "run_id", "scenario", "status", "started_at", "ended_at", "simulated_seconds",
    "verdict", "coverage", "expected_detections", "correct_detections", "missed",
    "false_alarms", "events", "alerts_issued", "mean_detection_latency_s",
    "emergency_responses", "emergency_generation_ms", "max_danger_level",
]
SCENARIO_COLUMNS = [
    "scenario", "runs", "finished_runs", "incomplete_runs", "correct_detections", "missed",
    "false_alarms", "events", "alerts_issued", "mean_detection_latency_s",
    "min_detection_latency_s", "max_detection_latency_s", "emergency_responses",
    "mean_emergency_generation_ms",
]


def _avg(xs):
    return round(mean(xs), 3) if xs else None


def build_report(metas: list[dict]) -> dict:
    """
    شمارش تشخیص، هشدار و هشدار اشتباه از همه‌ی اجراها؛ «تشخیص‌ندادن» و میانگین
    تأخیر فقط از اجراهای کامل‌شده، چون اجرای نیمه‌کاره هنوز فرصت تشخیص نداشته است.
    """
    rows: dict[str, dict] = {}
    for m in metas:
        mt = m["metrics"]
        r = rows.setdefault(m["scenario"], {
            "scenario": m["scenario"], "title_fa": m["title_fa"], "coverage": m["coverage"],
            "runs": 0, "finished_runs": 0, "incomplete_runs": 0, "correct_detections": 0,
            "missed": 0, "false_alarms": 0, "events": 0, "alerts_issued": 0,
            "emergency_responses": 0, "_lat": [], "_gen": [],
        })
        r["runs"] += 1
        r["correct_detections"] += mt["detected"]
        r["false_alarms"] += mt["false_alarms"]
        r["events"] += mt["events"]
        r["alerts_issued"] += mt["alerts_issued"]
        r["emergency_responses"] += mt["emergency_responses"]
        r["_gen"] += mt["emergency_generation_ms"]
        if m["status"] == "finished":
            r["finished_runs"] += 1
            r["missed"] += mt["missed"]
            r["_lat"] += mt["detection_latencies_s"]
        else:
            r["incomplete_runs"] += 1

    scenarios = []
    for r in rows.values():
        lat, gen = r.pop("_lat"), r.pop("_gen")
        r.update(mean_detection_latency_s=_avg(lat),
                 min_detection_latency_s=min(lat) if lat else None,
                 max_detection_latency_s=max(lat) if lat else None,
                 mean_emergency_generation_ms=_avg(gen))
        scenarios.append(r)
    scenarios.sort(key=lambda r: r["scenario"])

    s = lambda k: sum(r[k] for r in scenarios)  # noqa: E731
    return {
        "totals": {
            "runs": s("runs"), "finished_runs": s("finished_runs"),
            "incomplete_runs": s("incomplete_runs"),
            "correct_detections": s("correct_detections"), "missed": s("missed"),
            "false_alarms": s("false_alarms"), "events": s("events"),
            "alerts_issued": s("alerts_issued"),
            "emergency_responses": s("emergency_responses"),
        },
        "scenarios": scenarios,
        "runs": metas,
        "note": "تشخیص‌نشدن و میانگین تأخیر فقط از اجراهای کامل‌شده حساب می‌شود.",
    }


def _safe(v):
    """جلوگیری از اجرای فرمول هنگام بازکردن CSV در Excel."""
    if v is None:
        return ""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _to_csv(columns: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_safe(r.get(c)) for c in columns])
    return "\ufeff" + buf.getvalue()           # BOM: فارسی در Excel درست باز شود


def runs_csv(metas: list[dict]) -> str:
    rows = []
    for m in metas:
        mt = m["metrics"]
        rows.append({
            **m,
            "expected_detections": mt["expected"], "correct_detections": mt["detected"],
            "missed": mt["missed"], "false_alarms": mt["false_alarms"], "events": mt["events"],
            "alerts_issued": mt["alerts_issued"],
            "mean_detection_latency_s": _avg(mt["detection_latencies_s"]),
            "emergency_responses": mt["emergency_responses"],
            "emergency_generation_ms": _avg(mt["emergency_generation_ms"]),
            "max_danger_level": mt["max_danger_level"], "verdict": mt["verdict"],
        })
    return _to_csv(RUN_COLUMNS, rows)


def scenarios_csv(report: dict) -> str:
    return _to_csv(SCENARIO_COLUMNS, report["scenarios"])
