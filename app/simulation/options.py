# app/simulation/options.py
"""گزینه‌های شبیه‌ساز و قاعده‌ی تعیین سیاست تأیید راننده."""

from dataclasses import dataclass, field

from app.motion.crash_response import (
    DEFAULT_CONFIRM_TIMEOUT_S,
    DEFAULT_ON_TIMEOUT,
    TIMEOUT_POLICIES,
)

DRIVER_ACTIONS = ("confirm", "decline", "none")


@dataclass(frozen=True)
class DriverBehavior:
    """رفتار راننده‌ی شبیه‌سازی‌شده وقتی پرسش تصادف را می‌بیند."""

    action: str = "none"      # confirm | decline | none (پاسخ نمی‌دهد)
    delay_s: float = 10.0     # بعد از پرسش، چند ثانیه تا پاسخ

    def __post_init__(self) -> None:
        if self.action not in DRIVER_ACTIONS:
            raise ValueError(f"action باید یکی از {DRIVER_ACTIONS} باشد.")
        if self.delay_s < 0:
            raise ValueError("delay_s نباید منفی باشد.")


@dataclass(frozen=True)
class SimulationOptions:
    scenario: str
    event_time_s: float = 15.0          # رویداد سناریو چند ثانیه بعد از شروع رخ دهد
    latitude: float = 35.6892
    longitude: float = 51.3890
    gps_mode: str = "good"              # good | poor_accuracy | stale | no_fix
    driver: DriverBehavior = field(default_factory=DriverBehavior)
    # شبیه‌ساز می‌تواند سیاست تأیید را عوض کند (اولویت: خط فرمان > شبیه‌ساز > تنظیمات > پیش‌فرض)
    confirm_timeout_s: float | None = None
    on_timeout: str | None = None


def resolve_crash_policy(
    cli_timeout_s: float | None = None,
    cli_on_timeout: str | None = None,
    simulation: SimulationOptions | None = None,
    settings_timeout_s: float | None = None,
    settings_on_timeout: str | None = None,
) -> tuple[float, str]:
    """(timeout_s, on_timeout). اولویت: خط فرمان > شبیه‌ساز > تنظیمات > پیش‌فرض."""
    sim_timeout = simulation.confirm_timeout_s if simulation else None
    sim_policy = simulation.on_timeout if simulation else None

    def first(*values, default):
        return next((v for v in values if v is not None), default)

    timeout = first(cli_timeout_s, sim_timeout, settings_timeout_s,
                    default=DEFAULT_CONFIRM_TIMEOUT_S)
    policy = first(cli_on_timeout, sim_policy, settings_on_timeout,
                   default=DEFAULT_ON_TIMEOUT)

    if not timeout > 0:
        raise ValueError(f"زمان انتظار تأیید باید مثبت باشد (دریافت شد: {timeout}).")
    if policy not in TIMEOUT_POLICIES:
        raise ValueError(f"on_timeout باید یکی از {TIMEOUT_POLICIES} باشد (دریافت شد: {policy!r}).")
    return float(timeout), policy
