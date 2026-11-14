"""
نمایش CrashDetector روی سناریوهای شبیه‌سازی‌شده (بدون خودرو و بدون اینترنت).

    python demo_crash.py            # همه‌ی سناریوها
    python demo_crash.py crash      # یک سناریو
"""

import sys

from app.motion import SCENARIOS, CrashDetector, MotionSimulator

EXPECTED = {
    "crash": True,
    "crash_no_speed": True,       # فقط حسگر گوشی؛ اطمینان کم
    "crash_engine_off": True,     # سرعت نامعلوم، موتور خاموش؛ اطمینان متوسط
    "crash_engine_running": True,  # موتور روشن ماند؛ باز هم تشخیص داده می‌شود
}


def run(name: str) -> bool:
    detector = CrashDetector("demo")
    events = []
    peak_g = 0.0
    for step in MotionSimulator().scenario(name):
        peak_g = max(peak_g, step.sample.accel_magnitude_g)
        detector.update_vehicle(step.sample.timestamp, step.speed_kmh, step.rpm)
        events += detector.process(step.sample)

    expected = EXPECTED.get(name, False)
    got = bool(events)
    mark = "✅" if got == expected else "❌"
    print(f"{mark} {name:21} بیشینه‌ی شتاب {peak_g:5.1f} g → ", end="")
    if not events:
        print("رویدادی تولید نشد")
    for e in events:
        d = e.details
        print(
            f"{e.severity.value.upper()} ({d['confidence']}) | "
            f"افت سرعت={d['speed_drop']} چرخش={d['spin']} "
            f"سکون={d['still']} موتور‌خاموش={d['engine_off']}"
        )
    return got == expected


def main() -> int:
    names = sys.argv[1:] or list(SCENARIOS)
    results = [run(n) for n in names]
    print("\nهمه‌ی سناریوها مطابق انتظار" if all(results) else "\nمغایرت دیده شد")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
