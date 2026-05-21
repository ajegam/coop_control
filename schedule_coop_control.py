#!/usr/bin/env python3

import argparse
import datetime as dt
import json
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------
# Configuration
# --------------------------------------------------

PROJECT_DIR = "/home/pi/projects/coop_control"

PYTHON_BIN = f"{PROJECT_DIR}/venv/bin/python"
COOP_CONTROL_PY = f"{PROJECT_DIR}/coop_control.py"

CACHE_FILE = Path(PROJECT_DIR) / "logs" / "sun_times_cache.json"

LOS_GATOS_LAT = 37.2358
LOS_GATOS_LON = -121.9623

TZ = ZoneInfo("America/Los_Angeles")

CRON_MARKER = "COOP_CONTROL_SCHEDULED"

# --------------------------------------------------
# Sunrise / Sunset
# --------------------------------------------------


def fetch_sunrise_sunset(date_str: str):
    """
    Get sunrise/sunset from sunrise-sunset.org.
    Retries on failure.
    Falls back to cached values if API unavailable.
    """

    url = "https://api.sunrise-sunset.org/json"

    params = {
        "lat": LOS_GATOS_LAT,
        "lng": LOS_GATOS_LON,
        "formatted": 0,
        "date": date_str,
    }

    last_error = None

    for attempt in range(1, 6):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=20,
            )

            response.raise_for_status()

            data = response.json()

            if data.get("status") != "OK":
                raise RuntimeError(
                    f"Sunrise API returned status={data.get('status')}"
                )

            sunrise_utc = dt.datetime.fromisoformat(
                data["results"]["sunrise"].replace("Z", "+00:00")
            )

            sunset_utc = dt.datetime.fromisoformat(
                data["results"]["sunset"].replace("Z", "+00:00")
            )

            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

            CACHE_FILE.write_text(
                json.dumps(
                    {
                        "date": date_str,
                        "sunrise": sunrise_utc.isoformat(),
                        "sunset": sunset_utc.isoformat(),
                    }
                )
            )

            return sunrise_utc, sunset_utc

        except Exception as e:
            last_error = e

            print(
                f"[schedule] sunrise API attempt "
                f"{attempt}/5 failed: {e}"
            )

            time.sleep(attempt * 2)

    print(
        "[schedule] sunrise API unavailable after retries."
    )

    # ---------------------------------------------
    # Fallback to cache
    # ---------------------------------------------

    if CACHE_FILE.exists():
        print("[schedule] Using cached sunrise/sunset values.")

        cached = json.loads(CACHE_FILE.read_text())

        cached_sunrise = dt.datetime.fromisoformat(
            cached["sunrise"]
        )

        cached_sunset = dt.datetime.fromisoformat(
            cached["sunset"]
        )

        today = dt.datetime.now(TZ).date()

        sunrise_local = cached_sunrise.astimezone(TZ).replace(
            year=today.year,
            month=today.month,
            day=today.day,
        )

        sunset_local = cached_sunset.astimezone(TZ).replace(
            year=today.year,
            month=today.month,
            day=today.day,
        )

        return (
            sunrise_local.astimezone(dt.timezone.utc),
            sunset_local.astimezone(dt.timezone.utc),
        )

    raise RuntimeError(
        f"Unable to retrieve sunrise/sunset. "
        f"No cache available. Last error: {last_error}"
    )


def apply_offset(
    utc_time: dt.datetime,
    offset_minutes: int,
):
    return utc_time.astimezone(TZ) + dt.timedelta(
        minutes=offset_minutes
    )


# --------------------------------------------------
# Crontab
# --------------------------------------------------


def read_crontab():
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        stderr = (result.stderr or "").lower()

        if "no crontab" in stderr:
            return []

        raise RuntimeError(result.stderr)

    return result.stdout.splitlines()


def write_crontab(lines):
    content = "\n".join(lines).rstrip() + "\n"

    result = subprocess.run(
        ["crontab", "-"],
        input=content,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed writing crontab: {result.stderr}"
        )


def remove_old_entries(lines):
    return [
        line
        for line in lines
        if CRON_MARKER not in line
    ]


def build_cron_line(
    run_time,
    command,
    tag,
):
    return (
        f"{run_time.minute} "
        f"{run_time.hour} "
        f"{run_time.day} "
        f"{run_time.month} "
        f"* "
        f"{command} "
        f"# {CRON_MARKER} {tag}"
    )


# --------------------------------------------------
# Main
# --------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Schedule coop control jobs"
    )

    parser.add_argument(
        "--sunset_offset",
        type=int,
        default=60,
        help="Minutes after sunset "
             "(default 60)",
    )

    parser.add_argument(
        "--sunrise_offset",
        type=int,
        default=30,
        help="Minutes after sunrise "
             "(default 30)",
    )

    args = parser.parse_args()

    today = dt.datetime.now(TZ).date()

    date_str = today.strftime("%Y-%m-%d")

    sunrise_utc, sunset_utc = fetch_sunrise_sunset(
        date_str
    )

    sunset_run_time = apply_offset(
        sunset_utc,
        args.sunset_offset,
    )

    sunrise_run_time = apply_offset(
        sunrise_utc,
        args.sunrise_offset,
    )

    night_command = (
        f'cd "{PROJECT_DIR}" && '
        f'"{PYTHON_BIN}" '
        f'"{COOP_CONTROL_PY}"'
    )

    morning_command = (
        f'cd "{PROJECT_DIR}" && '
        f'"{PYTHON_BIN}" '
        f'"{COOP_CONTROL_PY}" '
        f'--auto_door_open'
    )

    lines = read_crontab()

    lines = remove_old_entries(lines)

    lines.append(
        build_cron_line(
            sunset_run_time,
            night_command,
            "SUNSET",
        )
    )

    lines.append(
        build_cron_line(
            sunrise_run_time,
            morning_command,
            "SUNRISE",
        )
    )

    write_crontab(lines)

    print(
        f"[schedule] Sunset  : "
        f"{sunset_run_time.strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
    )

    print(
        f"[schedule] Sunrise : "
        f"{sunrise_run_time.strftime('%Y-%m-%d %I:%M:%S %p %Z')}"
    )

    print(
        "[schedule] Crontab updated successfully."
    )


if __name__ == "__main__":
    main()