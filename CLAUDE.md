# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A chicken coop monitoring system for a Raspberry Pi. Two ONVIF/PTZ cameras (a "roost" camera and an "auto_door" camera) are moved to presets, an RTSP frame is captured, and the frame is sent to OpenAI's vision API to (1) count chickens on the roost against an expected total and (2) determine whether the coop door is OPEN or CLOSED. Results and photos are pushed to Telegram. There is no test suite, build step, or package manifest beyond `requirements.txt` — this is a small two-script operational tool, not a library.

## Most Important

The chicking counting needs to be accurate. Incorrect counting can leave chickens outside the coop and will be eaten by predators.  
The chicken coop door open or closed needs to be accurate as well. An open door at night will give predators a chance to get the chickens. Also a closed door in the morning means the chicken will be stuck in the coop and not allowed to go to the run when the sun rises. 

## Setup / running

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Requires a `.env` file (gitignored) and a `wsdl/` directory containing the full ONVIF WSDL/XSD bundle (checked at minimum for `wsdl/devicemgmt.wsdl` and `wsdl/onvif.xsd`) — the ONVIF client uses this local bundle instead of fetching WSDL over the network so it works reliably on a Pi.

```bash
# Default: chicken count + door check (door expects DOOR_EXPECTED_STATE from .env)
python coop_control.py

# Individual checks
python coop_control.py --chicken_count
python coop_control.py --auto_door_close   # expect CLOSED
python coop_control.py --auto_door_open    # expect OPEN

# Disable Telegram sends (still logs what would have been sent)
python coop_control.py --telegram_off
```

`coop_control.py` validates required env vars up front based on which checks/flags are active (see the table in README.md) and exits with a clear message listing what's missing rather than failing deep in the call stack — preserve that pattern when adding new required config.

There are no automated tests or linters configured in this repo. "Testing" a change means running `coop_control.py` against real (or `--telegram_off`) cameras and checking `logs/coop_monitor.log` plus the captured JPGs in `logs/`.

## Scheduling (`schedule_coop_control.py`)

Run once daily (e.g. from cron at 00:01) on the Pi. It fetches sunrise/sunset for Los Gatos, CA from sunrise-sunset.org (retries 5x, then falls back to a cached `logs/sun_times_cache.json` if the API is unreachable), then **rewrites the crontab**: removes any line containing the `COOP_CONTROL_SCHEDULED` marker and adds two fresh entries — one at sunset+offset (default 60 min) running the default `coop_control.py` (nightly chicken+door-closed check), one at sunrise+offset (default 30 min) running `coop_control.py --auto_door_open`.

This script has hard-coded Pi paths at the top (`PROJECT_DIR`, `PYTHON_BIN`, `COOP_CONTROL_PY`) that must match wherever it's actually deployed — update these together if the install location changes, they are not derived from `__file__`.

```bash
python schedule_coop_control.py --sunset_offset 45 --sunrise_offset 20
```

## Architecture notes worth knowing before editing

- **Two independent pipelines, same shape**: roost (chicken count) and auto_door (door state) each follow move-camera → capture-frame → ask-OpenAI → format-message → send-Telegram, with per-stage failure isolation (e.g. if OpenAI analysis fails, a "PROBLEM: analysis failed" message is still sent with the photo; if the camera itself is unreachable, a separate "camera not accessible" message is sent instead). When adding a third check, follow this same `run_*_check()` shape in `coop_control.py`.
- **`with_retries()`** wraps all network-ish operations (ONVIF preset move, RTSP capture, OpenAI calls, Telegram sends) with exponential backoff. Reuse it rather than adding ad-hoc retry loops.
- **Chicken counting is two-pass**: pass 1 asks for a count; if it doesn't match `TOTAL_CHICKENS`, pass 2 re-prompts with the mismatch called out and asks for a careful recount. The README notes counting accuracy is very model-sensitive (`OPENAI_MODEL` env var) — don't assume swapping models is a no-op.
- **Telegram sends downgrade gracefully**: photo send is retried, and only on total photo-send failure does it fall back to a text-only message (`send_telegram()` in `coop_control.py`). Images are also resized/recompressed via `make_telegram_image_copy()` before upload to reduce timeout risk.
- **RTSP URL format** is hard-coded in `build_rtsp()` (`rtsp://user:pass@ip:554/h264Preview_01_main`) — specific to the camera model in use, not a generic ONVIF stream URI lookup.
- **`schedule_coop_control.py` and `coop_control.py` are decoupled**: the scheduler only shells out to the CLI via cron; it does not import or share code with `coop_control.py`.
- Logs and captured JPGs go to `logs/` (gitignored): `coop_monitor.log` (rotating, 5 backups), plus timestamped and "latest" JPGs per camera (`roost.jpg`, `auto_door.jpg`).

## Documentation

- Keep README.md and CLAUDE.md updated with any significant change.
