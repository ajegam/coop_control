"""
Called by cron at 1:00 AM each day. --offset how much should the sunset time be offset (in minutes) 
This program will figure out the sunset time then add the offset passed in to the program.
It will then create an entry in crontab at the sunset + offset time to run coop_control.py
"""

import sys
import subprocess
import os
from datetime import datetime, timedelta
from pathlib import Path
import requests
import typer

app = typer.Typer()

# Los Gatos, CA coordinates
LOS_GATOS_LAT = 37.2358
LOS_GATOS_LON = -121.9623

# Marker comment to identify our cron entry.
# This is used to remove older cron entries. So don't modify
CRON_MARKER = "# AUTOMATICALLY ADDED by schedule_coop_control.py program. Do not modify"


def get_sunset_time():
    """Get today's sunset time for Los Gatos using sunrise-sunset.org API"""
    url = "https://api.sunrise-sunset.org/json"
    params = {
        "lat": LOS_GATOS_LAT,
        "lng": LOS_GATOS_LON,
        "formatted": 0,  # Get ISO 8601 format
        "date": "today"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data["status"] != "OK":
            raise Exception(f"API returned status: {data['status']}")
        
        # Parse the sunset time (in UTC) and convert to local time
        sunset_utc = datetime.fromisoformat(data["results"]["sunset"].replace("Z", "+00:00"))
        sunset_local = sunset_utc.astimezone()
        
        return sunset_local
    
    except requests.exceptions.RequestException as e:
        print(f"Error fetching sunset time: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error processing sunset data: {e}")
        sys.exit(1)


def get_current_crontab():
    """Get current crontab contents"""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            check=True,
            capture_output=True,
            text=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        # If crontab doesn't exist yet, return empty string
        if e.returncode == 1 and "no crontab" in e.stderr.lower():
            return ""
        print(f"Error reading crontab: {e.stderr}")
        sys.exit(1)


def remove_managed_cron_entry(crontab_content):
    """Remove any existing sunset scheduler managed entry"""
    lines = crontab_content.split('\n')
    filtered_lines = []
    
    skip_next = False
    for line in lines:
        if CRON_MARKER in line:
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        filtered_lines.append(line)
    
    return '\n'.join(filtered_lines)


def add_cron_entry(target_time, offset):
    """Add new cron entry for today's sunset-adjusted time"""
    
    # Get current crontab
    current_crontab = get_current_crontab()
    
    # Remove any existing managed entry
    cleaned_crontab = remove_managed_cron_entry(current_crontab)
    
    # Get absolute path to coop_control.py
    script_dir = Path(__file__).parent.absolute()
    coop_control_path = script_dir / "coop_control.py"
    venv_python = script_dir / "venv" / "bin" / "python3"
    log_file = script_dir / "logs" / "coop_control.log"
    
    # Create cron time format (minute hour day month day_of_week)
    minute = target_time.minute
    hour = target_time.hour
    day = target_time.day
    month = target_time.month
    
    # Build the new cron entry
    cron_command = (
        f"cd {script_dir} && {venv_python} {coop_control_path} "
        f">> {log_file} 2>&1"
    )
    
    new_entry = f"{CRON_MARKER}\n"
    new_entry += f"{minute} {hour} {day} {month} * {cron_command}"
    
    # Combine old and new crontab
    if cleaned_crontab.strip():
        new_crontab = cleaned_crontab.rstrip() + "\n" + new_entry + "\n"
    else:
        new_crontab = new_entry + "\n"
    
    # Install new crontab
    try:
        process = subprocess.Popen(
            ["crontab", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate(input=new_crontab)
        
        if process.returncode != 0:
            print(f"Error installing crontab: {stderr}")
            sys.exit(1)
        
        return new_entry
    
    except Exception as e:
        print(f"Error updating crontab: {e}")
        sys.exit(1)


@app.command()
def main(
    offset: int = typer.Option(
        0,
        "--offset",
        help="Minutes offset from sunset (positive or negative)"
    )
):
    """
    Schedule coop_control.py to run at today's sunset + offset.
    
    This should be run daily at 1 AM via cron to set up today's schedule.
    It removes yesterday's entry and adds today's entry.
    """
    
    print(f"\n{'='*70}")
    print(f"Sunset Cron Scheduler - {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}")
    print(f"{'='*70}\n")
    
    # Get sunset time for today
    sunset = get_sunset_time()
    
    # Calculate target time (sunset + offset)
    target_time = sunset + timedelta(minutes=offset)
    
    # Format times for display
    sunset_str = sunset.strftime("%I:%M:%S %p")
    target_str = target_time.strftime("%I:%M:%S %p")
    
    print(f"Los Gatos sunset today:  {sunset_str}")
    print(f"Offset:                  {offset:+d} minutes")
    print(f"Scheduled run time:      {target_str}")
    print(f"                         ({target_time.strftime('%Y-%m-%d %H:%M')})")
    print(f"\n{'='*70}\n")
    
    # Update crontab
    print("Updating crontab...")
    print("  - Removing previous sunset scheduler entry (if exists)")
    print("  - Adding new entry for today")
    
    new_entry = add_cron_entry(target_time, offset)
    
    print(f"\n✓ Crontab updated successfully!\n")
    print("New cron entry:")
    print("-" * 70)
    print(new_entry)
    print("-" * 70)
    print(f"\ncoop_control.py will run today at {target_str}\n")


if __name__ == "__main__":
    app()
