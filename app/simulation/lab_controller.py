# app/simulation/lab_controller.py
"""
کنترل اجرای شبیه‌سازی: Start / Pause / Resume / Stop / Reset / Restart و سرعت 1x..10x.

موتور در ریسمان جداگانه اجرا می‌شود؛ داشبورد (یا هر مصرف‌کننده‌ی دیگر) فقط از
`snapshot()` و `subscribe()` می‌خواند و هرگز موتور را مسدود نمی‌کند. هر اجرای
جدید یک `SimulationEngine` تازه می‌سازد، پس داده و رویداد اجراهای قبلی قاطی نمی‌شود.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from app.simulation.lab_engine import SimulationEngine

logger = logging.getLogger(__name__)

SPEEDS = (1, 2, 5, 10)
MAX_CONSECUTIVE_STEP_ERRORS = 5
IDLE, RUNNING, PAUSED, STOPPED, FINISHED, ERROR = (
    "idle", "running", "paused", "stopped", "finished", "error",
)


class SimulationController:
    def __init__(self, engine_factory: Callable[..., SimulationEngine] = SimulationEngine):
        self._factory = engine_factory
        self._lock = threading.RLock()        # وضعیت و کنترل
        self._data_lock = threading.Lock()    # گام موتور و خواندن داده
        self._engine: SimulationEngine | None = None
        self._params: dict | None = None
        self._state = IDLE
        self._speed = 1.0
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._run_id = 0
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._listeners: list[Callable[[dict], None]] = []
        self._end_listeners: list[Callable] = []
        self._run: dict | None = None      # اجرای جاری برای اعلان پایان

    # ------------------------------------------------------------ کنترل

    def start(self, scenario: str, **params) -> None:
        """شروع اجرای تازه. اجرای قبلی (در صورت وجود) متوقف می‌شود."""
        engine = self._factory(scenario, **params)   # خطای ورودی همین‌جا بالا می‌آید
        with self._lock:
            old, old_run = self._detach()
        self._join(old)                              # بیرون از قفل: بن‌بست نمی‌شود
        self._finish_run(old_run, "aborted")
        with self._lock:
            self._run = {"engine": engine, "started_at": datetime.now(timezone.utc),
                         "done": False}
            self._engine = engine
            self._params = dict(params, scenario=scenario)
            self._error = None
            self._state = RUNNING
            self._thread = threading.Thread(
                target=self._loop,
                args=(self._run_id, engine, self._stop, self._resume, self._run),
                name="sim-engine",
                daemon=True,
            )
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self._state == RUNNING:
                self._state = PAUSED
                self._resume.clear()

    def resume(self) -> None:
        with self._lock:
            if self._state == PAUSED:
                self._state = RUNNING
                self._resume.set()

    def stop(self) -> None:
        """متوقف می‌کند؛ داده‌ی اجرا برای گزارش و بازپخش باقی می‌ماند."""
        with self._lock:
            if self._state not in (RUNNING, PAUSED):
                return
            self._state = STOPPED
            old, run = self._detach()
        self._join(old)
        self._finish_run(run, "stopped")

    def reset(self) -> None:
        """همه‌چیز پاک و کنترلر به حالت idle برمی‌گردد."""
        with self._lock:
            old, run = self._detach()
            self._engine = None
            self._params = None
            self._error = None
            self._state = IDLE
        self._join(old)
        self._finish_run(run, "aborted")

    def restart(self) -> None:
        with self._lock:
            if self._params is None:
                raise RuntimeError("سناریویی برای راه‌اندازی مجدد وجود ندارد.")
            params = dict(self._params)
            scenario = params.pop("scenario")
        self.start(scenario, **params)

    def set_speed(self, multiplier: float) -> None:
        if not (0 < multiplier <= 50):
            raise ValueError("ضریب سرعت باید بین 0 و 50 باشد.")
        with self._lock:
            self._speed = float(multiplier)

    def subscribe_end(self, listener: Callable) -> None:
        """listener(engine, status, started_at, ended_at) — فقط یک‌بار برای هر اجرا."""
        with self._lock:
            self._end_listeners.append(listener)

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    # ------------------------------------------------------------ وضعیت

    @property
    def state(self) -> str:
        return self._state

    @property
    def engine(self) -> SimulationEngine | None:
        return self._engine

    def snapshot(self, frames_from: int = 0) -> dict:
        with self._lock:
            engine = self._engine
            state, speed, error = self._state, self._speed, self._error
            scenario = self._params["scenario"] if self._params else None
        with self._data_lock:
            frames = list(engine.frames[frames_from:]) if engine else []
            summary = engine.summary() if engine else None
            count = len(engine.frames) if engine else 0
        if True:
            return {
                "state": state,
                "speed": speed,
                "error": error,
                "scenario": scenario,
                "frame_count": count,
                "frames": frames,
                "summary": summary,
            }

    def wait(self, timeout: float | None = None) -> bool:
        """برای تست و اسکریپت: صبر تا پایان رشته. True یعنی تمام شد."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ------------------------------------------------------------ داخلی

    def _detach(self) -> tuple[threading.Thread | None, dict | None]:
        """رشته‌ی فعلی را به توقف علامت می‌دهد (زیر قفل). join جداگانه و بیرون از قفل."""
        self._run_id += 1            # هر نوشتن بعدی رشته‌ی قدیمی نادیده گرفته می‌شود
        self._stop.set()
        self._resume.set()           # رشته‌ی متوقف‌شده بیدار شود و خارج شود
        thread, self._thread = self._thread, None
        run, self._run = self._run, None
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        return thread, run

    def _finish_run(self, run: dict | None, status: str) -> None:
        """اعلان پایان اجرا؛ برای هر اجرا دقیقاً یک بار، بیرون از قفل وضعیت."""
        if run is None:
            return
        with self._lock:
            if run["done"]:
                return
            run["done"] = True
            listeners = list(self._end_listeners)
        ended = datetime.now(timezone.utc)
        with self._data_lock:                    # گام در حال اجرا تمام شده باشد
            for listener in listeners:
                try:
                    listener(run["engine"], status, run["started_at"], ended)
                except Exception:
                    logger.exception("Simulation end listener failed.")

    @staticmethod
    def _join(thread: threading.Thread | None) -> None:
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _loop(self, run_id: int, engine: SimulationEngine, stop: threading.Event,
              resume: threading.Event, run: dict | None = None) -> None:
        errors = 0
        while not stop.is_set():
            resume.wait()
            if stop.is_set():
                return
            try:
                if run_id != self._run_id:
                    return
                with self._data_lock:
                    frame = engine.step()
                errors = 0
            except Exception as exc:
                errors += 1
                logger.exception("Simulation step failed (%d/%d).",
                                 errors, MAX_CONSECUTIVE_STEP_ERRORS)
                if errors >= MAX_CONSECUTIVE_STEP_ERRORS:
                    with self._lock:
                        if run_id == self._run_id:
                            self._state = ERROR
                            self._error = f"{type(exc).__name__}: {exc}"
                    self._finish_run(run, "error")
                    return
                stop.wait(0.2)
                continue

            if frame is None or engine.finished:
                with self._lock:
                    if run_id == self._run_id and self._state in (RUNNING, PAUSED):
                        self._state = FINISHED
                if frame is not None:
                    self._notify(frame)
                self._finish_run(run, "finished")
                return
            self._notify(frame)
            stop.wait(1.0 / self._speed)

    def _notify(self, frame: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(frame)
            except Exception:
                logger.exception("Simulation listener failed.")
