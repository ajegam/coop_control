#!/usr/bin/env python3
import argparse
import datetime as dt
import subprocess
from zoneinfo import ZoneInfo
import requests

# -----------------------------
# Hard-coded project directory
# -----------------------------
PROJECT_DIR = "/home/pi/projects/coop_control"
PYTHON_BIN = f"{PROJECT_DIR}/venv/bin/python"
COOP_CONTROL_PY = f"{PROJECT_DIR}/coop_control.py"

# -----------------------------
# Los Gatos, CA coordinates
# -----------------------------
LOS_GATOS_LAT = 37.2358
LOS_GATOS_LON = -121.9623
TZ = ZoneInfo("America/Los_Angeles")

CRON_MARKER = "COOP_CONTROL_SCHEDULED"


def fetch_sunrise_sunset(date_str: str):
    url = "https://api.sunrise-sunset.org/json"
    params = {
        "lat": LOS_GATOS_LAT,
        "lng": LOS_GATOS_LON,
        "formatted": 0,
        "date": date_str,
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    if data.get("status") != "OK":
        raise RuntimeError(f"Sun API error: {data}")

    sunrise_utc = dt.datetime.fromisoformat(
        data["results"]["sunrise"].replace("Z", "+00:00")
    )
    sunset_utc = dt.datetime.fromisoformat(
        data["results"]["sunset"].replace("Z", "+00:00")
    )

    return sunrise_utc, sunset_utc


def to_local_with_offset(utc_dt: dt.datetime, offset_minutes: int):
    return utc_dt.astimezone(TZ) + dt.timedelta(minutes=offset_minutes)


def read_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        if "no crontab" in (result.stderr or "").lower():
            return []
        raise RuntimeError(result.stderr.strip())
    return result.stdout.splitlines()


def write_crontab(lines):
    content = "\n".join(lines).rstrip() + "\n"
    result = subprocess.run(["crontab", "-"], input=content, text=True)
    if result.returncode != 0:
        raise RuntimeError("Failed to write crontab")


def remove_previous_entries(lines):
    return [line for line in lines if CRON_MARKER not in line]


def build_cron_line(run_dt, command, tag):
    minute = run_dt.minute
    hour = run_dt.hour
    dom = run_dt.day
    month = run_dt.month

    return f"{minute} {hour} {dom} {month} * {command} # {CRON_MARKER} {tag}"


def main():
    parser = argparse.ArgumentParser(
        description="Schedules coop_control.py based on Los Gatos sunrise/sunset"
    )

    parser.add_argument(
        "--sunset_offset",
        type=int,
        default=60,
        help="Minutes offset applied to sunset (default: 60)",
    )

    parser.add_argument(
        "--sunrise_offset",
        type=int,
        default=30,
        help="Minutes offset applied to sunrise (default: 30)",
    )

    args = parser.parse_args()

    today_local = dt.datetime.now(TZ).date()
    date_str = today_local.strftime("%Y-%m-%d")

    sunrise_utc, sunset_utc = fetch_sunrise_sunset(date_str)

    run_sunset_local = to_local_with_offset(sunset_utc, args.sunset_offset)
    run_sunrise_local = to_local_with_offset(sunrise_utc, args.sunrise_offset)

    # Commands (cd ensures .env/wsdl/logs resolve correctly)
    night_cmd = f'cd "{PROJECT_DIR}" && "{PYTHON_BIN}" "{COOP_CONTROL_PY}"'
    morning_cmd = f'cd "{PROJECT_DIR}" && "{PYTHON_BIN}" "{COOP_CONTROL_PY}" --auto_door_close'

    current_cron = read_crontab()
    cleaned_cron = remove_previous_entries(current_cron)

    cleaned_cron.append(build_cron_line(run_sunset_local, night_cmd, "NIGHT_SUNSET"))
    cleaned_cron.append(build_cron_line(run_sunrise_local, morning_cmd, "MORNING_SUNRISE"))

    write_crontab(cleaned_cron)

    print(f"[schedule] Date: {date_str}")
    print(f"[schedule] Sunset UTC:  {sunset_utc}")
    print(f"[schedule] Sunrise UTC: {sunrise_utc}")
    print(f"[schedule] Run sunset + {args.sunset_offset} min  -> {run_sunset_local}")
    print(f"[schedule] Run sunrise + {args.sunrise_offset} min -> {run_sunrise_local}")
    print("[schedule] Cron updated successfully.")


if __name__ == "__main__":
    main()