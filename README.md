# coop_control

Chicken coop monitoring using two ONVIF PTZ cameras, OpenAI vision (chicken count + door state), and Telegram. Optional scheduler uses sunrise/sunset (Los Gatos, CA) to run nightly and morning checks via cron.

## What it does

1. **Roost check** — Moves the roost camera to a preset, captures a JPG over RTSP, sends the image to OpenAI to count chickens, then sends the result and image to Telegram.
2. **Auto door check** — Moves the auto-door camera to a preset, captures a JPG, asks OpenAI whether the door is OPEN or CLOSED, then sends the result and image to Telegram.

You can run one or both checks. By default (no flags), both run: chicken count + door check using `DOOR_EXPECTED_STATE` from `.env` (e.g. CLOSED for “nightly”).

## Requirements

- Python 3.9+ (for `zoneinfo` if using the scheduler)
- Two ONVIF/PTZ cameras with presets and RTSP
- OpenAI API key (vision model)
- Optional: Telegram Bot Token and Chat ID
- **ONVIF WSDL bundle** in a `wsdl/` directory (see below)

## Setup

### 1. Clone and virtual environment

```bash
git clone <repo-url>
cd coop_control
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. ONVIF WSDL directory

The script uses a local WSDL directory so ONVIF works reliably (e.g. on Raspberry Pi). Create `wsdl/` in the project root and put the full ONVIF WSDL/XSD bundle there. Required files include:

- `wsdl/devicemgmt.wsdl`
- `wsdl/onvif.xsd`

(Plus any other WSDL/XSD files the ONVIF client needs; the script checks for the two above.)

### 3. Environment variables

Create `.env` in the project root. Variables are validated based on which checks you run.

| Variable | Required when | Description |
|----------|----------------|-------------|
| `OPENAI_API_KEY` | Always | OpenAI API key |
| `OPENAI_MODEL` | Optional | Vision model (default: `gpt-4.1-mini`) |
| `TELEGRAM_BOT_TOKEN` | When Telegram on | Bot token |
| `TELEGRAM_CHAT_ID` | When Telegram on | Chat ID for photos/messages |
| **Roost (chicken count)** | | |
| `ROOST_IP` | Chicken check | Roost camera IP |
| `ROOST_USER` | Chicken check | Camera username |
| `ROOST_PASS` | Chicken check | Camera password |
| `ROOST_PRESET` | Chicken check | Preset name (case-insensitive) |
| `ROOST_ONVIF_PORT` | Optional | ONVIF port (default: 8000). Use 80 if camera uses HTTP. |
| `TOTAL_CHICKENS` | Chicken check | Expected number of chickens (positive integer) |
| **Auto door** | | |
| `AUTO_DOOR_IP` | Door check | Auto-door camera IP |
| `AUTO_DOOR_USER` | Door check | Camera username |
| `AUTO_DOOR_PASS` | Door check | Camera password |
| `AUTO_DOOR_PRESET` | Door check | Preset name |
| `AUTO_DOOR_ONVIF_PORT` | Optional | ONVIF port (default: 8000) |
| `DOOR_EXPECTED_STATE` | Door check (if not overridden by CLI) | `OPEN` or `CLOSED` (e.g. CLOSED for nightly) |

If you run with `--telegram_off`, Telegram env vars are not required.

## Usage

### coop_control.py

```bash
# Default: chicken count + door check (uses DOOR_EXPECTED_STATE from .env)
python coop_control.py

# Only chicken count
python coop_control.py --chicken_count

# Only door check, expect CLOSED
python coop_control.py --auto_door_close

# Only door check, expect OPEN
python coop_control.py --auto_door_open

# Disable sending to Telegram (still logs what would be sent)
python coop_control.py --telegram_off
```

- **Default (no mode flags):** Runs both chicken check and door check. Door expected state comes from `DOOR_EXPECTED_STATE` (typical nightly: CLOSED).
- **`--chicken_count`:** Roost only.
- **`--auto_door_close`:** Door check only, expect CLOSED.
- **`--auto_door_open`:** Door check only, expect OPEN.

Images and logs go to `logs/` (timestamped JPGs plus `roost.jpg` / `auto_door.jpg`, and `coop_monitor.log`).

### schedule_coop_control.py (Raspberry Pi / cron)

Schedules two daily runs using [sunrise-sunset.org](https://sunrise-sunset.org) for **Los Gatos, CA**:

- **After sunset** (sunset + offset, default 60 min): runs `coop_control.py` (chicken count + door check with `DOOR_EXPECTED_STATE` from env, typically CLOSED).
- **After sunrise** (sunrise + offset, default 30 min): runs `coop_control.py --auto_door_open`.

It **rewrites your crontab** and removes any existing lines containing `COOP_CONTROL_SCHEDULED`, then adds the two new entries.

**Paths:** The script uses hard-coded paths for a Pi:

- `PROJECT_DIR = "/home/pi/projects/coop_control"`
- `PYTHON_BIN = "/home/pi/projects/coop_control/venv/bin/python"`
- `COOP_CONTROL_PY = "/home/pi/projects/coop_control/coop_control.py"`

If your install is elsewhere, edit these at the top of `schedule_coop_control.py`.

```bash
# Default: sunset+60 min, sunrise+30 min
python schedule_coop_control.py

# Custom offsets (minutes)
python schedule_coop_control.py --sunset_offset 45 --sunrise_offset 20
```

Requires: `requests` (and a system with `crontab`). Uses `zoneinfo` (Python 3.9+). Run once per day (e.g. from cron at 00:01) to refresh the two scheduled times for the current date.

## Project layout

```
coop_control/
├── .env                    # Not in git; secrets and config
├── .gitignore
├── coop_control.py         # Main checks (roost + auto door)
├── schedule_coop_control.py # Sunrise/sunset cron scheduler (Pi)
├── requirements.txt
├── README.md
├── wsdl/                   # ONVIF WSDL/XSD bundle (you provide)
│   ├── devicemgmt.wsdl
│   ├── onvif.xsd
│   └── ...
└── logs/                   # Created at run time (gitignored)
    ├── coop_monitor.log
    ├── roost_YYYYMMDDHHMMSS.jpg
    ├── roost.jpg
    ├── auto_door_YYYYMMDDHHMMSS.jpg
    └── auto_door.jpg
```

## Dependencies

- **python-dotenv** — Load `.env`
- **requests** — Telegram (and sunrise-sunset in scheduler)
- **onvif-zeep**, **zeep** — ONVIF camera control (PTZ presets)
- **opencv-python-headless** — RTSP capture and JPG write (Pi-friendly)
- **openai** — OpenAI API (vision)

See `requirements.txt` for versions.

## Raspberry Pi notes

- Use **opencv-python-headless** (already in requirements) to avoid GUI deps and long builds.
- Ensure `wsdl/` is present and contains the ONVIF bundle so ONVIF works without network WSDL fetch issues.
- For scheduling, install the project under `/home/pi/projects/coop_control` and use a venv named `venv`, or edit the paths in `schedule_coop_control.py`.
