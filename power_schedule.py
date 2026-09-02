"""power_schedule.py — self-managed power on/off for the trading PC.

The trading cron runs 06:00-10:00 ET on weekdays; the machine is otherwise
unused. This module shuts it down after the run and wakes it before the next
one, using the RTC alarm (rtcwake), so it is not always on:

- ``--arm`` (05:50 ET cron): set the RTC alarm for the next weekday 05:45 ET
  using ``rtcwake -m no`` (set the alarm only, do NOT power off). Arming
  early in the day means any manual shutdown later is safe — the alarm is
  already sitting in the RTC chip.
- ``--shutdown`` (10:00 ET cron): power off unconditionally via
  ``rtcwake -m off``. The user accepts being shut down on (typically asleep
  by then; can power back on manually from the Kasa app or the plug).

Wake math is done in America/New_York explicitly (the box runs
America/Edmonton; the cron already uses CRON_TZ=America/New_York), but
rtcwake takes seconds-from-now so the actual command is timezone-immune.

One-time setup (see SETUP.md):
- BIOS: enable "Wake on RTC Alarm" / "Resume by Alarm".
- sudoers: ``user ALL=(ALL) NOPASSWD: /usr/sbin/rtcwake``
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

WAKE_HOUR = 5
WAKE_MINUTE = 45
# rtcwake needs root (writes /sys/class/rtc/rtc0/wakealarm); the sudoers rule
# (user ALL=(ALL) NOPASSWD: /usr/sbin/rtcwake) makes this passwordless.
_RTCWAKE = ["sudo", "-n", "/usr/sbin/rtcwake"]


def _next_wake(now: datetime) -> datetime:
    """Next weekday (Mon-Fri) 05:45 ET at or after ``now``."""
    wake = datetime(now.year, now.month, now.day, WAKE_HOUR, WAKE_MINUTE,
                    tzinfo=ET)
    if now >= wake:
        wake += timedelta(days=1)
    while wake.weekday() >= 5:  # Saturday=5, Sunday=6
        wake += timedelta(days=1)
    return wake


def _seconds_until_wake(now: datetime) -> int:
    """Seconds from ``now`` to the next weekday wake, timezone-immune."""
    target = _next_wake(now)
    delta = target - now
    return int(delta.total_seconds()) + 1  # round up past the minute boundary


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def arm(now: datetime | None = None, dry_run: bool = False) -> int:
    """Arm the RTC alarm for the next wake; leave the machine running."""
    seconds = _seconds_until_wake(now or datetime.now(ET))
    cmd = [*_RTCWAKE, "-m", "no", "-s", str(seconds)]
    print(f"arm: {' '.join(cmd)} "
          f"(wake {_next_wake(now or datetime.now(ET))})")
    if not dry_run:
        _run(cmd)
    return 0


def shutdown(now: datetime | None = None, dry_run: bool = False) -> int:
    """Arm the RTC alarm and power off."""
    seconds = _seconds_until_wake(now or datetime.now(ET))
    cmd = [*_RTCWAKE, "-m", "off", "-s", str(seconds)]
    print(f"shutdown: {' '.join(cmd)} "
          f"(wake {_next_wake(now or datetime.now(ET))})")
    if not dry_run:
        _run(cmd)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self-managed PC power schedule")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--arm", action="store_true",
                       help="set RTC alarm for next weekday wake (stay on)")
    group.add_argument("--shutdown", action="store_true",
                       help="set RTC alarm and power off")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the command without executing it")
    args = parser.parse_args(argv)
    if args.arm:
        return arm(dry_run=args.dry_run)
    return shutdown(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
