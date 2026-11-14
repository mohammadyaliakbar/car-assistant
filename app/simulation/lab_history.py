# app/simulation/lab_history.py
"""
تاریخچه‌ی اجراها (ذخیره روی دیسک) و موتور بازپخش.

هر اجرا دو فایل دارد: `<id>.json` (داده‌ی کامل) و `<id>.meta.json` (خلاصه‌ی سبک
برای فهرست و گزارش). نوشتن اتمیک است و وجود فایل meta یعنی اجرا کامل ذخیره شده.
بازپخش همان فریم‌های ذخیره‌شده را به‌ترتیب زمانی پخش می‌کند، پس Pause/سرعت/Stop
همان کنترلر معمولی کار می‌کند و هیچ دادهٔ تازه‌ای ساخته نمی‌شود.
"""

import copy
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "lab_history"
MAX_RUNS = 200
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[a-z_]+-\d+$")
DANGER_RANK = {"normal": 0, "info": 1, "warning": 2, "critical": 3}


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def compute_metrics(engine) -> dict:
    s = engine.summary()
    initial = [r["generation_ms"] for r in engine.emergency_log if r["phase"] == "initial"]
    status_alerts = sum(len(f["status_alerts"]) for f in engine.frames)
    danger = max((f["danger_level"] for f in engine.frames),
                 key=DANGER_RANK.get, default="normal")
    detected = [r for r in s["expected"] if r["detected"]]
    return {
        "expected": len(s["expected"]),
        "detected": len(detected),
        "missed": len(s["expected"]) - len(detected),
        "false_alarms": s["false_alarms"],
        "events": s["events"],
        "alerts_issued": s["events"] + status_alerts,
        "detection_latencies_s": [r["detection_latency_s"] for r in detected],
        "emergency_responses": s["emergency_responses"],
        "emergency_generation_ms": initial,
        "max_danger_level": danger,
        "verdict": s["verdict"],
    }


class HistoryStore:
    def __init__(self, directory: Path | str = HISTORY_DIR, max_runs: int = MAX_RUNS):
        self.dir = Path(directory)
        self.max_runs = max_runs
        self._lock = threading.Lock()

    def _paths(self, run_id: str) -> tuple[Path, Path]:
        if not RUN_ID_RE.match(run_id or ""):
            raise ValueError("شناسه‌ی اجرا نامعتبر است.")
        return self.dir / f"{run_id}.json", self.dir / f"{run_id}.meta.json"

    def save_run(self, engine, status: str, started_at: datetime, ended_at: datetime) -> str | None:
        if not engine.frames:
            return None                                  # اجرای بدون هیچ فریم ذخیره نمی‌شود
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            k = 1
            while True:
                run_id = f"{started_at:%Y%m%d-%H%M%S}-{engine.spec.name}-{k}"
                if not self._paths(run_id)[0].exists():
                    break
                k += 1
            meta = {
                "run_id": run_id,
                "scenario": engine.spec.name,
                "title_fa": engine.spec.title_fa,
                "title_en": engine.spec.title_en,
                "status": status,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "simulated_seconds": engine.t,
                "vehicle_id": engine.profile.vehicle_id,
                "event_time_s": engine.event_time_s,
                "coverage": engine.spec.coverage,
                "coverage_note": engine.spec.coverage_note,
                "metrics": compute_metrics(engine),
            }
            full = {
                "meta": meta,
                "route": engine.route.to_dict(),
                "frames": engine.frames,            # شامل داده‌ی خودرو و GPS هر ثانیه
                "events": engine.events,
                "alerts": engine.active_alerts(),
                "emergency": engine.emergency_log,
                "summary": engine.summary(),
            }
            data_path, meta_path = self._paths(run_id)
            _atomic_write(data_path, full)
            _atomic_write(meta_path, meta)           # آخر: نشانه‌ی کامل‌بودن
            self._prune()
        return run_id

    def list_runs(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.meta.json"), reverse=True):
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                if RUN_ID_RE.match(meta.get("run_id", "")):
                    out.append(meta)
            except (OSError, ValueError):
                logger.warning("Skipping unreadable history file %s", p.name)
        return out

    def load_run(self, run_id: str) -> dict:
        data_path, _ = self._paths(run_id)
        try:
            return json.loads(data_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("اجرای موردنظر پیدا نشد.") from None
        except (OSError, ValueError):
            raise ValueError("فایل تاریخچه خراب است.") from None

    def delete_run(self, run_id: str) -> None:
        for p in self._paths(run_id):
            p.unlink(missing_ok=True)

    def _prune(self) -> None:
        metas = sorted(self.dir.glob("*.meta.json"))
        for p in metas[: max(0, len(metas) - self.max_runs)]:
            self.delete_run(p.name.removesuffix(".meta.json"))


class ReplayEngine:
    """بازپخش یک اجرای ذخیره‌شده؛ واسط همان SimulationEngine برای کنترلر."""

    is_replay = True

    def __init__(self, scenario: str, store: HistoryStore):
        self.run_id = scenario.split(":", 1)[1]
        self.run = store.load_run(self.run_id)
        self.frames: list[dict] = []
        self._i = 0

    @property
    def finished(self) -> bool:
        return self._i >= len(self.run["frames"])

    def step(self) -> dict | None:
        if self.finished:
            return None
        frame = copy.deepcopy(self.run["frames"][self._i])
        frame["replayed"] = True
        frame["replay_of"] = self.run_id
        self._i += 1
        self.frames.append(frame)
        return frame

    def summary(self) -> dict:
        return dict(self.run["summary"], replay_of=self.run_id)
