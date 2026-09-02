"""Hermetic tests for power_schedule.py (no rtcwake, no clock dependency)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import power_schedule

ET = ZoneInfo("America/New_York")


def _dt(y, m, d, hh, mm, tz=ET):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def test_next_wake_is_next_weekday_morning():
    # Monday 12:00 ET -> Tuesday 04:00 ET
    wake = power_schedule._next_wake(_dt(2026, 9, 7, 12, 0))
    assert wake == _dt(2026, 9, 8, 4, 0)


def test_next_wake_same_day_when_before_wake_time():
    # Monday 03:00 ET -> Monday 04:00 ET (already past midnight, same day)
    wake = power_schedule._next_wake(_dt(2026, 9, 7, 3, 0))
    assert wake == _dt(2026, 9, 7, 4, 0)


def test_next_wake_friday_rolls_to_monday():
    wake = power_schedule._next_wake(_dt(2026, 9, 4, 12, 0))  # Friday
    assert wake == _dt(2026, 9, 7, 4, 0)  # Monday
    assert wake.weekday() == 0


def test_next_wake_weekend_rolls_to_monday():
    wake = power_schedule._next_wake(_dt(2026, 9, 5, 12, 0))  # Saturday
    assert wake == _dt(2026, 9, 7, 4, 0)


def test_seconds_until_wake_is_timezone_immune():
    # The box runs America/Edmonton; the wake target is ET. seconds-from-now
    # must be identical regardless of the input timezone.
    from zoneinfo import ZoneInfo as ZI
    edm = ZI("America/Edmonton")
    now_et = _dt(2026, 9, 7, 12, 0)
    now_edm = now_et.astimezone(edm)
    assert power_schedule._seconds_until_wake(now_et) == \
        power_schedule._seconds_until_wake(now_edm)


def test_rtcwake_off_command():
    # shutdown path: -m off with the computed seconds, via passwordless sudo
    with patch("power_schedule._run") as run:
        power_schedule.shutdown(now=_dt(2026, 9, 7, 12, 0))
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["sudo", "-n", "/usr/sbin/rtcwake"]
    assert cmd[3:5] == ["-m", "off"]
    assert cmd[5] == "-s"
    secs = int(cmd[6])
    assert secs > 0


def test_rtcwake_arm_command_uses_no_mode():
    # arm path: -m no (set the alarm, do NOT power off), via passwordless sudo
    with patch("power_schedule._run") as run:
        power_schedule.arm(now=_dt(2026, 9, 7, 12, 0))
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["sudo", "-n", "/usr/sbin/rtcwake"]
    assert cmd[3:5] == ["-m", "no"]


def test_dry_run_does_not_execute():
    with patch("power_schedule._run") as run, \
         patch("sys.argv", ["power_schedule.py", "--arm", "--dry-run"]):
        power_schedule.main()
    run.assert_not_called()


def test_main_requires_a_mode():
    with patch("sys.argv", ["power_schedule.py"]), \
         pytest.raises(SystemExit):
        power_schedule.main()
