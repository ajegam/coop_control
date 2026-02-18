import os
import sys
import time
import argparse
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import shutil
import base64
import re

import requests
import cv2
from dotenv import load_dotenv
from onvif import ONVIFCamera
from openai import OpenAI
from pathlib import Path

# Need for Camera WSDL
WSDL_DIR = str(Path(__file__).resolve().parent / "wsdl")

# --------------------------------------------------
# CLI FIRST
# --------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--telegram_off", action="store_true")
args = parser.parse_args()
TELEGRAM_ENABLED = not args.telegram_off

# --------------------------------------------------
# Load .env (override system env!)
# --------------------------------------------------
load_dotenv(override=True)

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
# Validate Environment Variables
# --------------------------------------------------
def validate_env():
    required = [
        "ROOST_IP", "ROOST_USER", "ROOST_PASS", "ROOST_PRESET",
        "AUTO_DOOR_IP", "AUTO_DOOR_USER", "AUTO_DOOR_PASS", "AUTO_DOOR_PRESET",
        "TOTAL_CHICKENS", "DOOR_EXPECTED_STATE",
        "OPENAI_API_KEY",
    ]

    if TELEGRAM_ENABLED:
        required.extend(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])

    missing = [v for v in required if not (os.getenv(v) or "").strip()]
    if missing:
        print("\n❌ Missing required environment variables:\n")
        for m in missing:
            print(f"   - {m}")
        print("\nCheck your .env file.\n")
        sys.exit(1)

    # Validate TOTAL_CHICKENS
    try:
        tc = int(os.getenv("TOTAL_CHICKENS"))
        if tc <= 0:
            raise ValueError
    except ValueError:
        print("❌ TOTAL_CHICKENS must be a positive integer.")
        sys.exit(1)

    # Validate door expected state
    des = os.getenv("DOOR_EXPECTED_STATE").strip().upper()
    if des not in ("OPEN", "CLOSED"):
        print("❌ DOOR_EXPECTED_STATE must be OPEN or CLOSED.")
        sys.exit(1)

validate_env()

TOTAL_CHICKENS = int(os.getenv("TOTAL_CHICKENS"))
DOOR_EXPECTED_STATE = os.getenv("DOOR_EXPECTED_STATE").strip().upper()

log.info(f"TOTAL_CHICKENS loaded = {TOTAL_CHICKENS}")
log.info(f"DOOR_EXPECTED_STATE loaded = {DOOR_EXPECTED_STATE}")
log.info(f"TELEGRAM_ENABLED = {TELEGRAM_ENABLED}")

# --------------------------------------------------
# OpenAI Setup
# --------------------------------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
# Tried Models

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.0")

def image_to_data_url(image_path):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

def openai_roost_count(image_path):
    data_url = image_to_data_url(image_path)

    # -------------------------
    # PASS 1 (normal count)
    # -------------------------
    prompt_pass1 = (
        f"Count the number of chickens visible in this image.\n"
        f"Count the chickens one by one.\n"
        f"Identify chickens by locating heads or eye reflections.\n"
        f"If these are not visible then use body shapes.\n"
        f"Some chickens may be partially hidden or overlapping.\n"
        f"Assume no chicken is fully occluded unless proven otherwise.\n"
        f"Carefully check edges, corners, and underneath other chickens.\n"
        f"The amount we are looking for is {TOTAL_CHICKENS}.\n"
        f"If the count is less than {TOTAL_CHICKENS}, do a recount but don't make up numbers.\n"
        f"Return ONLY a single integer. No words."
    )

    def run_prompt(prompt_text):
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt_text},
                    {"type": "input_image", "image_url": data_url},
                ],
            }],
        )
        text = resp.output_text.strip()
        match = re.search(r"\d+", text)
        if not match:
            raise RuntimeError(f"Could not parse count from: {text}")
        return int(match.group(0))

    count1 = run_prompt(prompt_pass1)
    log.info(f"OpenAI pass1 chicken count = {count1}")

    # -------------------------
    # If correct, return immediately
    # -------------------------
    if count1 == TOTAL_CHICKENS:
        return count1

    # -------------------------
    # PASS 2 (strict recount)
    # -------------------------
    prompt_pass2 = (
        f"You previously counted {count1} chickens.\n"
        f"The expected total is {TOTAL_CHICKENS}.\n"
        f"Do a FULL recount carefully.\n"
        f"List chickens mentally one-by-one before giving final answer.\n"
        f"Look for hidden chickens underneath or overlapping.\n"
        f"Be conservative. Do not double count.\n"
        f"Return ONLY the final integer count. No words."
    )

    count2 = run_prompt(prompt_pass2)
    log.info(f"OpenAI pass2 chicken count = {count2}")

    return count2


def openai_door_state(image_path):
    data_url = image_to_data_url(image_path)

    prompt = (
        "Is the chicken coop door OPEN or CLOSED?\n"
        "Return ONLY one word: OPEN or CLOSED."
    )

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_url},
            ],
        }],
    )

    text = resp.output_text.strip().upper()
    if "CLOSED" in text:
        return "CLOSED"
    if "OPEN" in text:
        return "OPEN"
    raise RuntimeError(f"Could not parse door state from: {text}")

# --------------------------------------------------
# Format Messages
# --------------------------------------------------
def format_roost_message(found):
    if found == TOTAL_CHICKENS:
        return f"🐔 All {found} out of {TOTAL_CHICKENS} chickens found."
    return f"🔴 PROBLEM: Only {found} out of {TOTAL_CHICKENS} chickens found."

def format_door_message(state):
    if state == DOOR_EXPECTED_STATE:
        return f"🚪 Door is {state} (OK)."
    return f"🔴 PROBLEM: Door is {state}, expected {DOOR_EXPECTED_STATE}."

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
# Move to Preset
# --------------------------------------------------
def goto_preset(cam_cfg):
    def _move():
        cam = ONVIFCamera(cam_cfg["ip"], cam_cfg["port"], cam_cfg["user"], cam_cfg["pw"], wsdl_dir=WSDL_DIR)
        media = cam.create_media_service()
        ptz = cam.create_ptz_service()

        profile = media.GetProfiles()[0]
        presets = ptz.GetPresets({"ProfileToken": profile.token})

        wanted = cam_cfg["preset"].strip().lower()
        for p in presets:
            name = (getattr(p, "Name", "") or "").strip().lower()
            if name == wanted:
                req = ptz.create_type("GotoPreset")
                req.ProfileToken = profile.token
                req.PresetToken = p.token
                ptz.GotoPreset(req)
                return

        raise RuntimeError(f"Preset '{cam_cfg['preset']}' not found")

    with_retries(_move, label="GotoPreset")

# --------------------------------------------------
# Capture JPG
# --------------------------------------------------
def capture_jpg(cam_cfg):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base = cam_cfg["jpg_base"]

    timestamped = f"logs/{base}_{timestamp}.jpg"
    latest = f"logs/{base}.jpg"

    def _capture():
        rtsp = build_rtsp(cam_cfg["user"], cam_cfg["pw"], cam_cfg["ip"])
        cap = cv2.VideoCapture(rtsp)
        time.sleep(1.5)
        ok, frame = cap.read()
        cap.release()

        if not ok or frame is None:
            raise RuntimeError("RTSP capture failed")

        cv2.imwrite(timestamped, frame)
        shutil.copyfile(timestamped, latest)

    with_retries(_capture, label="RTSP Capture")
    return timestamped

# --------------------------------------------------
# Move + Capture
# --------------------------------------------------
def move_then_capture(camera_name, settle_seconds=4.0):
    cam_cfg = CAMERAS[camera_name]
    log.info(f"Starting {camera_name} check")

    goto_preset(cam_cfg)
    time.sleep(settle_seconds)

    img = capture_jpg(cam_cfg)
    log.info(f"{camera_name} image saved: {img}")
    return img

# --------------------------------------------------
# Telegram Sender (ALWAYS logs message)
# --------------------------------------------------
from datetime import datetime

def send_telegram(text, image_path=None):
    """
    Always logs what would be sent.
    Caption format:
      🕒 YYYY-MM-DD HH:MM:SS AM/PM
      <message>
    """

    ts = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    caption = f"🕒 {ts}\n{text}"

    # Always log intended message (with timestamp)
    if image_path:
        log.info(f"[TELEGRAM] caption={caption!r} | image={image_path}")
    else:
        log.info(f"[TELEGRAM] text={caption!r}")

    if not TELEGRAM_ENABLED:
        log.info("Telegram disabled (--telegram_off). Not sending.")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    base = f"https://api.telegram.org/bot{token}"

    try:
        if image_path:
            with open(image_path, "rb") as photo:
                r = requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"photo": photo},
                    timeout=20,
                )
        else:
            r = requests.post(
                f"{base}/sendMessage",
                data={"chat_id": chat_id, "text": caption},
                timeout=20,
            )

        if not r.ok:
            log.warning(f"Telegram send failed: {r.status_code} {r.text}")
        else:
            log.info("Telegram message sent.")

    except Exception as e:
        log.warning(f"Telegram send failed: {e}")

# --------------------------------------------------
# Main
# --------------------------------------------------
if __name__ == "__main__":

    # ROOST
    roost_img = move_then_capture("roost")
    try:
        count = openai_roost_count(roost_img)
        roost_msg = format_roost_message(count)
    except Exception as e:
        roost_msg = f"🔴 PROBLEM: Roost analysis failed ({e})"

    send_telegram(roost_msg, roost_img)

    # AUTO DOOR
    door_img = move_then_capture("auto_door")
    try:
        state = openai_door_state(door_img)
        door_msg = format_door_message(state)
    except Exception as e:
        door_msg = f"🔴 PROBLEM: Door analysis failed ({e})"

    send_telegram(door_msg, door_img)