# coop_control

Controls two ONVIF PTZ cameras (roost and auto door), moves each to a preset, captures a snapshot via RTSP, and sends the images to Telegram.

## What it does

1. **Roost camera** — Connects via ONVIF, moves to the configured preset, captures a JPG over RTSP, and sends it to Telegram.
2. **Auto door camera** — Same flow: goto preset → capture → send to Telegram.

Images are saved under `logs/` as timestamped files (e.g. `roost_20260212174341.jpg`) plus a latest copy (`roost.jpg`, `auto_door.jpg`). Logs go to the console and to `logs/coop_monitor.log` (rotating, 1 MB × 5 backups).

## Requirements

- Python 3.7+
- Two ONVIF/PTZ cameras with presets and RTSP
- Optional: Telegram Bot Token and Chat ID for notifications

## Setup

### 1. Clone and virtual environment

```bash
git clone <repo-url>
cd coop_control
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment variables

Copy `.env.example` to `.env` (or create `.env`) and set:

| Variable | Description |
|----------|-------------|
| `ROOST_IP` | Roost camera IP |
| `ROOST_USER` | Roost camera username |
| `ROOST_PASS` | Roost camera password |
| `ROOST_PRESET` | Preset name to move to (case-insensitive) |
| `ROOST_ONVIF_PORT` | ONVIF port (default: 8000). Use 80 if the camera uses HTTP. |
| `AUTO_DOOR_IP` | Auto door camera IP |
| `AUTO_DOOR_USER` | Auto door camera username |
| `AUTO_DOOR_PASS` | Auto door camera password |
| `AUTO_DOOR_PRESET` | Preset name for auto door camera |
| `AUTO_DOOR_ONVIF_PORT` | ONVIF port (default: 8000). Use 80 if needed. |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (required unless `--telegram_off`) |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for sending photos |

If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` are missing, run with `--telegram_off` (see below).

## Usage

```bash
# With Telegram (default)
python coop_control.py

# Without Telegram (e.g. no token set)
python coop_control.py --telegram_off
```

The script moves the roost camera to its preset, captures and sends the image, then does the same for the auto door camera. ONVIF and RTSP operations are retried up to 4 times with exponential backoff.

## Project layout

```
coop_control/
├── .env                 # Not in git; your secrets and config
├── .gitignore
├── coop_control.py      # Main script
├── requirements.txt
├── README.md
└── logs/                # Created at run time
    ├── coop_monitor.log
    ├── roost_YYYYMMDDHHMMSS.jpg
    ├── roost.jpg
    ├── auto_door_YYYYMMDDHHMMSS.jpg
    └── auto_door.jpg
```

## Dependencies

- **opencv-python** — RTSP capture and JPG write
- **python-dotenv** — Load `.env`
- **onvif-zeep** — ONVIF camera control (PTZ presets)
- **requests** — Telegram API

See `requirements.txt` for versions.
