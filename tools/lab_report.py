"""اجرای همه‌ی سناریوهای آزمایشگاه شبیه‌سازی و چاپ جدول نتیجه (بدون داشبورد).

    python tools/lab_report.py [نام_سناریو ...]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.simulation.lab_engine import SimulationEngine  # noqa: E402
from app.simulation.scenarios import LAB_SCENARIOS  # noqa: E402

VERDICT_TEXT = {
    "detected_by_detector": "تشخیص واقعی توسط Detector",
    "no_events_as_expected": "بدون رویداد (مطابق انتظار)",
    "status_only_no_detector": "فقط وضعیت داشبورد؛ Detector ندارد",
    "missed_by_detector": "تشخیص داده نشد!",
    "false_alarm": "هشدار اشتباه!",
}


def main(names: list[str]) -> int:
    names = names or list(LAB_SCENARIOS)
    failed = 0
    print(f"{'scenario':20} {'verdict':38} {'events':>6} {'false':>5} {'latency(s)':>10}")
    for name in names:
        engine = SimulationEngine(name)
        engine.run_to_end()
        s = engine.summary()
        lat = ",".join(
            "-" if r["detection_latency_s"] is None else f"{r['detection_latency_s']:g}"
            for r in s["expected"]
        ) or "-"
        print(f"{name:20} {VERDICT_TEXT[s['verdict']]:38} {s['events']:>6} "
              f"{s['false_alarms']:>5} {lat:>10}")
        failed += s["verdict"] in ("missed_by_detector", "false_alarm")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
