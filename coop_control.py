import os
import sys
import time
import argparse
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import shutil
import requests

import cv2
from dotenv import load_dotenv
from onvif import ONVIFCamera

# --------------------------------------------------
# CLI FIRST (needed for conditional validation)
# --------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--telegram_off", action="store_true")
args = parser.parse_args()

TELEGRAM_ENABLED = not args.telegram_off

# --------------------------------------------------
# Load Environment
# --------------------------------------------------
load_dotenv()

# --------------------------------------------------
# Validate Environment Variables
# --------------------------------------------------
def validate_env():
    required = [
        "ROOST_IP", "ROOST_USER", "ROOST_PASS", "ROOST_PRESET",
        "AUTO_DOOR_IP", "AUTO_DOOR_USER", "AUTO_DOOR_PASS", "AUTO_DOOR_PRESET",
    ]

    if TELEGRAM_ENABLED:
        required.extend([
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        ])

    missing = [v for v in required if not (os.getenv(v) or "").strip()]

    if missing:
        print("\n❌ Missing required environment variables:\n")
        for m in missing:
            print(f"   - {m}")
        print("\nCheck your .env file.\n")
        sys.exit(1)

    for port_var in ("ROOST_ONVIF_PORT", "AUTO_DOOR_ONVIF_PORT"):
        if os.getenv(port_var):
            try:
                int(os.getenv(port_var))
            except ValueError:
                print(f"❌ {port_var} must be an integer.")
                sys.exit(1)

validate_env()

# --------------------------------------------------
# Logging Setup
# --------------------------------------------------
def setup_logger():
    logger = logging.getLogger("coop_monitor")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    os.makedirs("logs", exist_ok=True)

    file_handler = RotatingFileHandler(
        "logs/coop_monitor.log",
        maxBytes=1_000_000,
        backupCount=5,
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger

log = setup_logger()

# --------------------------------------------------
# Camera Config
# --------------------------------------------------
CAMERAS = {
    "roost": {
        "ip": os.getenv("ROOST_IP"),
        "port": int(os.getenv("ROOST_ONVIF_PORT", "8000")),
        "user": os.getenv("ROOST_USER"),
        "pw": os.getenv("ROOST_PASS"),
        "preset": os.getenv("ROOST_PRESET"),
        "jpg_base": "roost",
    },
    "auto_door": {
        "ip": os.getenv("AUTO_DOOR_IP"),
        "port": int(os.getenv("AUTO_DOOR_ONVIF_PORT", "8000")),
        "user": os.getenv("AUTO_DOOR_USER"),
        "pw": os.getenv("AUTO_DOOR_PASS"),
        "preset": os.getenv("AUTO_DOOR_PRESET"),
        "jpg_base": "auto_door",
    },
}

def build_rtsp(user, pw, ip):
    return f"rtsp://{user}:{pw}@{ip}:554/h264Preview_01_main"

# --------------------------------------------------
# Retry Helper
# --------------------------------------------------
def with_retries(fn, tries=4, delay=1.0, backoff=2.0, label="operation"):
    current_delay = delay
    last_exception = None

    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last_exception = e
            if attempt == tries:
                break

            log.warning(f"{label} failed (attempt {attempt}/{tries}): {e}")
            time.sleep(current_delay)
            current_delay *= backoff

    raise last_exception

# --------------------------------------------------
# ONVIF Preset Movement
# --------------------------------------------------
def goto_preset(cam_cfg):
    def _move():
        cam = ONVIFCamera(
            cam_cfg["ip"],
            cam_cfg["port"],
            cam_cfg["user"],
            cam_cfg["pw"],
        )
        media = cam.create_media_service()
        ptz = cam.create_ptz_service()

        profile = media.GetProfiles()[0]
        presets = ptz.GetPresets({"ProfileToken": profile.token})

        match = None
        for p in presets:
            name = (getattr(p, "Name", "") or "").strip().lower()
            if name == cam_cfg["preset"].strip().lower():
                match = p
                break

        if not match:
            raise RuntimeError(f"Preset '{cam_cfg['preset']}' not found")

        req = ptz.create_type("GotoPreset")
        req.ProfileToken = profile.token
        req.PresetToken = match.token
        ptz.GotoPreset(req)

    with_retries(_move, label="GotoPreset")

# --------------------------------------------------
# Capture JPG
# --------------------------------------------------
def capture_jpg(cam_cfg):
    os.makedirs("logs", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base = cam_cfg["jpg_base"]

    timestamped_path = f"logs/{base}_{timestamp}.jpg"
    latest_path = f"logs/{base}.jpg"

    def _capture():
        rtsp = build_rtsp(cam_cfg["user"], cam_cfg["pw"], cam_cfg["ip"])
        cap = cv2.VideoCapture(rtsp)
        time.sleep(1.5)

        ok, frame = cap.read()
        cap.release()

        if not ok or frame is None:
            raise RuntimeError("RTSP frame capture failed")

        cv2.imwrite(timestamped_path, frame)
        shutil.copyfile(timestamped_path, latest_path)

    with_retries(_capture, label="RTSP Capture")

    return timestamped_path

# --------------------------------------------------
# Combined Move + Capture
# --------------------------------------------------
def move_then_capture(camera_name, settle_seconds=4.0):
    cam_cfg = CAMERAS[camera_name]

    log.info(f"Starting {camera_name} check")
    goto_preset(cam_cfg)

    time.sleep(settle_seconds)

    image_path = capture_jpg(cam_cfg)
    log.info(f"{camera_name} image saved: {image_path}")

    return image_path

# --------------------------------------------------
# Telegram Sender
# --------------------------------------------------
def send_telegram(text, image_path=None):
    if not TELEGRAM_ENABLED:
        log.info("Telegram disabled.")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    base = f"https://api.telegram.org/bot{token}"

    try:
        if image_path:
            with open(image_path, "rb") as photo:
                requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text},
                    files={"photo": photo},
                    timeout=20,
                )
        else:
            requests.post(
                f"{base}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=20,
            )

        log.info("Telegram message sent.")

    except Exception as e:
        log.warning(f"Telegram send failed: {e}")

# --------------------------------------------------
# Main Execution
# --------------------------------------------------
if __name__ == "__main__":

    roost_img = move_then_capture("roost")
    send_telegram(
        text=f"Roost snapshot captured (preset: {CAMERAS['roost']['preset']})",
        image_path=roost_img,
    )

    auto_door_img = move_then_capture("auto_door")
    send_telegram(
        text=f"Auto Door snapshot captured (preset: {CAMERAS['auto_door']['preset']})",
        image_path=auto_door_img,
    )