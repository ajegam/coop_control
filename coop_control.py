#!/usr/bin/env python3
import os
import sys
import time
import argparse
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
import shutil
import base64
import re
import socket

import requests
import cv2
from dotenv import load_dotenv
from onvif import ONVIFCamera
from openai import OpenAI

# ----------------------------
# Networking safety
# ----------------------------
socket.setdefaulttimeout(20)

# ----------------------------
# CLI
# ----------------------------
parser = argparse.ArgumentParser(description="Chicken coop checks (roost + auto door)")

parser.add_argument(
    "--telegram_off",
    action="store_true",
    help="Disable Telegram sends (still logs what would be sent).",
)
parser.add_argument(
    "--chicken_count",
    action="store_true",
    help="Run only the roost chicken count check.",
)
parser.add_argument(
    "--auto_door_close",
    action="store_true",
    help="Run only the auto door check expecting CLOSED.",
)
parser.add_argument(
    "--auto_door_open",
    action="store_true",
    help="Run only the auto door check expecting OPEN.",
)

args = parser.parse_args()

TELEGRAM_ENABLED = not args.telegram_off

# Determine run plan:
# - If no mode flags provided -> default nightly: chicken_count + auto_door_close
any_mode = args.chicken_count or args.auto_door_close or args.auto_door_open
RUN_CHICKEN = args.chicken_count or (not any_mode)
RUN_DOOR = args.auto_door_close or args.auto_door_open or (not any_mode)

DOOR_EXPECTED_OVERRIDE = None
if args.auto_door_close:
    DOOR_EXPECTED_OVERRIDE = "CLOSED"
if args.auto_door_open:
    DOOR_EXPECTED_OVERRIDE = "OPEN"

if args.auto_door_close and args.auto_door_open:
    print("❌ Use only one: --auto_door_close or --auto_door_open")
    sys.exit(1)

# ----------------------------
# Load .env (override system env)
# ----------------------------
load_dotenv(override=True)

# ----------------------------
# Logging
# ----------------------------
def setup_logger():
    logger = logging.getLogger("coop_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    os.makedirs("logs", exist_ok=True)

    fh = RotatingFileHandler("logs/coop_monitor.log", maxBytes=1_000_000, backupCount=5)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)

    return logger


log = setup_logger()

# Optional: quiet down very chatty libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ----------------------------
# WSDL directory (repo-local)
# ----------------------------
WSDL_DIR = str(Path(__file__).resolve().parent / "wsdl")

# ----------------------------
# Validation
# ----------------------------
def _require(var: str, required_list: list[str]):
    required_list.append(var)


def validate_env():
    required = []

    _require("OPENAI_API_KEY", required)

    if TELEGRAM_ENABLED:
        _require("TELEGRAM_BOT_TOKEN", required)
        _require("TELEGRAM_CHAT_ID", required)

    if RUN_CHICKEN:
        _require("ROOST_IP", required)
        _require("ROOST_USER", required)
        _require("ROOST_PASS", required)
        _require("ROOST_PRESET", required)
        _require("TOTAL_CHICKENS", required)

    if RUN_DOOR:
        _require("AUTO_DOOR_IP", required)
        _require("AUTO_DOOR_USER", required)
        _require("AUTO_DOOR_PASS", required)
        _require("AUTO_DOOR_PRESET", required)

        if DOOR_EXPECTED_OVERRIDE is None:
            _require("DOOR_EXPECTED_STATE", required)

    missing = [v for v in required if not (os.getenv(v) or "").strip()]
    if missing:
        print("\n❌ Missing required environment variables:\n")
        for m in missing:
            print(f"   - {m}")
        print("\nCheck your .env file.\n")
        sys.exit(1)

    if RUN_CHICKEN:
        try:
            tc = int(os.getenv("TOTAL_CHICKENS"))
            if tc <= 0:
                raise ValueError
        except ValueError:
            print("❌ TOTAL_CHICKENS must be a positive integer.")
            sys.exit(1)

    if RUN_DOOR and DOOR_EXPECTED_OVERRIDE is None:
        des = os.getenv("DOOR_EXPECTED_STATE").strip().upper()
        if des not in ("OPEN", "CLOSED"):
            print("❌ DOOR_EXPECTED_STATE must be OPEN or CLOSED.")
            sys.exit(1)

    if RUN_CHICKEN or RUN_DOOR:
        if not Path(WSDL_DIR).exists():
            print(f"❌ WSDL_DIR not found: {WSDL_DIR}")
            print("Create wsdl/ in repo and populate it with the full ONVIF wsdl/xsd bundle.")
            sys.exit(1)
        if not (Path(WSDL_DIR) / "devicemgmt.wsdl").exists():
            print(f"❌ Missing devicemgmt.wsdl in {WSDL_DIR}")
            sys.exit(1)
        if not (Path(WSDL_DIR) / "onvif.xsd").exists():
            print(f"❌ Missing onvif.xsd in {WSDL_DIR}")
            sys.exit(1)


validate_env()

TOTAL_CHICKENS = int(os.getenv("TOTAL_CHICKENS")) if RUN_CHICKEN else None
DOOR_EXPECTED_STATE = DOOR_EXPECTED_OVERRIDE or (
    os.getenv("DOOR_EXPECTED_STATE").strip().upper() if RUN_DOOR and DOOR_EXPECTED_OVERRIDE is None else DOOR_EXPECTED_OVERRIDE
)

# ----------------------------
# OpenAI
# ----------------------------
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=45.0)

log.info(f"RUN_CHICKEN={RUN_CHICKEN}, RUN_DOOR={RUN_DOOR}, TELEGRAM_ENABLED={TELEGRAM_ENABLED}")
log.info(f"OPENAI_MODEL loaded = {OPENAI_MODEL}")
if RUN_CHICKEN:
    log.info(f"TOTAL_CHICKENS loaded = {TOTAL_CHICKENS}")
if RUN_DOOR:
    log.info(f"DOOR_EXPECTED_STATE = {DOOR_EXPECTED_STATE}")

# ----------------------------
# Helpers
# ----------------------------
def image_to_data_url(image_path: str) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def with_retries(fn, tries=4, delay=1.0, backoff=2.0, label="operation"):
    current_delay = delay
    last_exc = None

    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt == tries:
                break
            log.warning(f"{label} failed (attempt {attempt}/{tries}): {e}")
            time.sleep(current_delay)
            current_delay *= backoff

    raise last_exc


# ----------------------------
# OpenAI image analysis
# ----------------------------
def _openai_run_count_prompt(data_url: str, prompt: str) -> int:
    def _call():
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
        text = resp.output_text.strip()
        m = re.search(r"\d+", text)
        if not m:
            raise RuntimeError(f"Could not parse count from: {text!r}")
        return int(m.group(0))

    return with_retries(_call, tries=3, delay=2.0, backoff=2.0, label="OpenAI chicken count")


def openai_roost_count(image_path: str) -> int:
    """
    Two-pass logic:
      pass1 -> if != TOTAL_CHICKENS -> pass2 strict recount
    """
    data_url = image_to_data_url(image_path)

    prompt_pass1 = (
        f"Count the number of chickens visible in this image.\n"
        f"Count the chickens one by one.\n"
        f"Identify chickens by locating heads or eye reflections. If these are not visible then use body shapes.\n"
        f"Some chickens may be partially hidden or overlapping.\n"
        f"Assume no chicken is fully occluded unless proven otherwise.\n"
        f"Carefully check edges, corners, and underneath other chickens.\n"
        f"The amount we are looking for is {TOTAL_CHICKENS}.\n"
        f"If the count is less than {TOTAL_CHICKENS}, do a recount but don't make up numbers.\n"
        f"These chickens are roosting in a coop and so there won't be spaces between them.\n"
        f"Sometimes one of the chicken can be sitting underneath the chickens sitting in the front roost.\n"
        f"Return ONLY a single integer. No words."
    )

    count1 = _openai_run_count_prompt(data_url, prompt_pass1)
    log.info(f"OpenAI pass1 chicken count = {count1}")

    if count1 == TOTAL_CHICKENS:
        return count1

    prompt_pass2 = (
        f"You previously counted {count1} chickens, but the expected total is {TOTAL_CHICKENS}.\n"
        f"Do a FULL recount carefully.\n"
        f"Count one-by-one and avoid double counting.\n"
        f"Look for hidden chickens underneath or overlapping.\n"
        f"Be conservative. Do not invent chickens.\n"
        f"Return ONLY the final integer count. No words."
    )

    count2 = _openai_run_count_prompt(data_url, prompt_pass2)
    log.info(f"OpenAI pass2 chicken count = {count2}")
    return count2


def openai_door_state(image_path: str) -> str:
    data_url = image_to_data_url(image_path)

    prompt = (
        "Is the chicken coop door OPEN or CLOSED?\n"
        "Return ONLY one word: OPEN or CLOSED."
    )

    def _call():
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
        raise RuntimeError(f"Could not parse door state from: {text!r}")

    return with_retries(_call, tries=3, delay=2.0, backoff=2.0, label="OpenAI door state")


# ----------------------------
# Message formatting
# ----------------------------
def format_roost_message(found: int) -> str:
    if found == TOTAL_CHICKENS:
        return f"🐔 All {found} out of {TOTAL_CHICKENS} chickens found."
    return f"🔴 PROBLEM: Only {found} out of {TOTAL_CHICKENS} chickens found."


def format_door_message(state: str) -> str:
    if state == DOOR_EXPECTED_STATE:
        return f"🚪 Door is {state} (OK)."
    return f"🔴 PROBLEM: Door is {state}, expected {DOOR_EXPECTED_STATE}."


def format_camera_unreachable_message(camera_label: str) -> str:
    return f"🔴 PROBLEM: {camera_label} camera not accessible."


# ----------------------------
# ONVIF + RTSP
# ----------------------------
def build_rtsp(user: str, pw: str, ip: str) -> str:
    return f"rtsp://{user}:{pw}@{ip}:554/h264Preview_01_main"


def goto_preset(ip: str, port: int, user: str, pw: str, preset_name: str) -> None:
    def _move():
        cam = ONVIFCamera(ip, port, user, pw, wsdl_dir=WSDL_DIR)
        media = cam.create_media_service()
        ptz = cam.create_ptz_service()

        profile = media.GetProfiles()[0]
        presets = ptz.GetPresets({"ProfileToken": profile.token}) or []

        wanted = preset_name.strip().lower()
        match = None
        for p in presets:
            name = (getattr(p, "Name", "") or "").strip().lower()
            if name == wanted:
                match = p
                break

        if not match:
            available = [getattr(p, "Name", "") for p in presets]
            raise RuntimeError(f"Preset '{preset_name}' not found. Available: {available}")

        req = ptz.create_type("GotoPreset")
        req.ProfileToken = profile.token
        req.PresetToken = match.token
        ptz.GotoPreset(req)

    with_retries(_move, label="GotoPreset")


def capture_jpg(ip: str, user: str, pw: str, base_name: str) -> str:
    os.makedirs("logs", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    timestamped = f"logs/{base_name}_{timestamp}.jpg"
    latest = f"logs/{base_name}.jpg"

    rtsp = build_rtsp(user, pw, ip)

    def _cap():
        cap = cv2.VideoCapture(rtsp)

        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000)
        except Exception:
            pass

        time.sleep(1.5)
        ok, frame = cap.read()
        cap.release()

        if not ok or frame is None:
            raise RuntimeError("RTSP frame capture failed")

        if not cv2.imwrite(timestamped, frame):
            raise RuntimeError("Failed to write JPG")

        shutil.copyfile(timestamped, latest)

    with_retries(_cap, label="RTSP Capture")
    return timestamped


def move_then_capture_roost(settle_seconds: float = 4.0) -> str:
    ip = os.getenv("ROOST_IP")
    port = int(os.getenv("ROOST_ONVIF_PORT", "8000"))
    user = os.getenv("ROOST_USER")
    pw = os.getenv("ROOST_PASS")
    preset = os.getenv("ROOST_PRESET")

    log.info("Starting roost check")
    goto_preset(ip, port, user, pw, preset)
    time.sleep(settle_seconds)

    img = capture_jpg(ip, user, pw, base_name="roost")
    log.info(f"roost image saved: {img}")
    return img


def move_then_capture_auto_door(settle_seconds: float = 4.0) -> str:
    ip = os.getenv("AUTO_DOOR_IP")
    port = int(os.getenv("AUTO_DOOR_ONVIF_PORT", "8000"))
    user = os.getenv("AUTO_DOOR_USER")
    pw = os.getenv("AUTO_DOOR_PASS")
    preset = os.getenv("AUTO_DOOR_PRESET")

    log.info("Starting auto_door check")
    goto_preset(ip, port, user, pw, preset)
    time.sleep(settle_seconds)

    img = capture_jpg(ip, user, pw, base_name="auto_door")
    log.info(f"auto_door image saved: {img}")
    return img


# ----------------------------
# Telegram
# ----------------------------
def make_telegram_image_copy(src_path: str) -> str:
    """
    Create a smaller jpeg for Telegram upload to reduce timeout risk.
    If anything fails, fall back to the original file.
    """
    try:
        img = cv2.imread(src_path)
        if img is None:
            return src_path

        h, w = img.shape[:2]
        max_width = 1280

        if w > max_width:
            new_height = int(h * (max_width / w))
            img = cv2.resize(img, (max_width, new_height))

        out_path = src_path.replace(".jpg", "_telegram.jpg")
        ok = cv2.imwrite(out_path, img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if ok:
            return out_path
        return src_path
    except Exception as e:
        log.warning(f"Failed to create Telegram image copy: {e}")
        return src_path


def send_telegram(text: str, image_path: str | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    caption = f"🕒 {ts}\n{text}"

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

    telegram_image_path = None
    if image_path:
        telegram_image_path = make_telegram_image_copy(image_path)

    def _send_photo():
        with open(telegram_image_path, "rb") as photo:
            r = requests.post(
                f"{base}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": photo},
                timeout=(10, 60),
            )
        if not r.ok:
            raise RuntimeError(f"Telegram photo send failed: {r.status_code} {r.text}")

    def _send_text():
        r = requests.post(
            f"{base}/sendMessage",
            data={"chat_id": chat_id, "text": caption},
            timeout=(10, 30),
        )
        if not r.ok:
            raise RuntimeError(f"Telegram text send failed: {r.status_code} {r.text}")

    try:
        if image_path:
            try:
                with_retries(_send_photo, tries=4, delay=2.0, backoff=2.0, label="Telegram photo")
                log.info("Telegram photo message sent.")
            except Exception as e:
                log.warning(f"Telegram photo send failed after retries: {e}")
                log.info("Falling back to text-only Telegram message.")
                with_retries(_send_text, tries=3, delay=2.0, backoff=2.0, label="Telegram text fallback")
                log.info("Telegram text fallback sent.")
        else:
            with_retries(_send_text, tries=3, delay=2.0, backoff=2.0, label="Telegram text")
            log.info("Telegram text message sent.")
    except Exception as e:
        log.warning(f"Telegram send completely failed: {e}")


# ----------------------------
# Main flow
# ----------------------------
def run_chicken_check():
    try:
        img = move_then_capture_roost()
        try:
            count = openai_roost_count(img)
            msg = format_roost_message(count)
        except Exception as e:
            msg = f"🔴 PROBLEM: Roost analysis failed ({e})"
            log.warning(msg)

        send_telegram(msg, img)

    except Exception as e:
        log.warning(f"Roost camera not accessible: {e}")
        send_telegram(format_camera_unreachable_message("Roost"))


def run_door_check():
    try:
        img = move_then_capture_auto_door()
        try:
            state = openai_door_state(img)
            msg = format_door_message(state)
        except Exception as e:
            msg = f"🔴 PROBLEM: Door analysis failed ({e})"
            log.warning(msg)

        send_telegram(msg, img)

    except Exception as e:
        log.warning(f"Auto Door camera not accessible: {e}")
        send_telegram(format_camera_unreachable_message("Auto Door"))


if __name__ == "__main__":
    if RUN_CHICKEN:
        run_chicken_check()

    if RUN_DOOR:
        run_door_check()