from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = True
    LOG_FILE: str = str(BASE_DIR / "logs" / "app.log")

    # فقط برای اتصال واقعی به OBD لازم است؛ شبیه‌ساز به آن نیاز ندارد.
    OBD_PORT: str = ""

    VEHICLE_ID: str = "car_a"

    # خالی = تلگرام غیرفعال (main.py همین را بررسی می‌کند)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
