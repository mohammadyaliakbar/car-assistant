"""اجرای داشبورد شبیه‌ساز:  python tools/run_dashboard.py [--port 8765] [--no-browser]"""

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.simulation.lab_server import LabApp, make_server  # noqa: E402


def telegram_factory():
    from app.config import settings
    from app.notifications.telegram_notifier import TelegramNotifier

    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID):
        return None
    return TelegramNotifier(token=settings.TELEGRAM_BOT_TOKEN,
                            chat_id=settings.TELEGRAM_CHAT_ID, cooldown_seconds=300)


def main() -> int:
    p = argparse.ArgumentParser(description="داشبورد شبیه‌ساز دستیار هوشمند خودرو")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()

    app = LabApp(notifier_factory=telegram_factory)
    server = make_server(app, a.host, a.port)
    url = f"http://{a.host}:{a.port}/"
    print(f"داشبورد شبیه‌ساز: {url}   (توقف: Ctrl+C)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
