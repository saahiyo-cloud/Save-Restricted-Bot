import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import re
import os
import sys
import json
import time
import math
import uuid
import logging
import functools
import shutil
from pathlib import Path
from typing import Optional, Set, Dict, Any, Union, Tuple, List

from dotenv import load_dotenv

import pyrogram
import pyrogram.utils
from pyrogram import Client, filters, idle
from pyrogram.errors import (
    UserAlreadyParticipant,
    InviteHashExpired,
    UsernameNotOccupied,
    FloodWait,
    MessageNotModified,
    MessageIdInvalid,
    RPCError,
)
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message,
)

# Monkey-patch Pyrogram for modern 64-bit Telegram Channel / Chat IDs
# Telegram creates supergroups and channels with IDs > 2147483647 (e.g. -1003533041485)
pyrogram.utils.MIN_CHANNEL_ID = -1009999999999999999
pyrogram.utils.MIN_CHAT_ID = -999999999999

_orig_get_peer_type = pyrogram.utils.get_peer_type


def _patched_get_peer_type(peer_id: int) -> str:
    if peer_id < 0:
        if peer_id <= pyrogram.utils.MAX_CHANNEL_ID:
            return "channel"
        return "chat"
    elif peer_id > 0:
        return "user"
    raise ValueError(f"Peer id invalid: {peer_id}")


pyrogram.utils.get_peer_type = _patched_get_peer_type

# Patch pyrogram.types.Message._parse to safely handle unresolvable reply messages without crashing dispatcher
_orig_message_parse = pyrogram.types.Message._parse


@staticmethod
async def _patched_message_parse(client, message, users, chats, is_scheduled: bool = False, replies: int = 1):
    try:
        return await _orig_message_parse(client, message, users, chats, is_scheduled=is_scheduled, replies=replies)
    except Exception as exc:
        if replies > 0:
            return await _orig_message_parse(client, message, users, chats, is_scheduled=is_scheduled, replies=0)
        raise exc


pyrogram.types.Message._parse = _patched_message_parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SaveRestrictedBot")

BOT_START_TIME = time.time()


def sync_telegram_time_offset() -> float:
    """Detect and compensate for system clock drift against Telegram servers to prevent MTProto msg_id errors."""
    try:
        import urllib.request
        from email.utils import parsedate_to_datetime
        res = urllib.request.urlopen("https://api.telegram.org", timeout=5)
        server_date_str = res.headers.get("Date")
        if server_date_str:
            server_time = parsedate_to_datetime(server_date_str).timestamp()
            local_time = time.time()
            skew = server_time - local_time
            if abs(skew) > 10:
                logger.info(f"System clock drift detected ({skew:+.2f}s). Synchronizing MTProto MsgId offset.")
                import pyrogram.session.internals.msg_id as msg_id_mod
                def patched_new(cls):
                    now = int(time.time() + skew)
                    cls.offset = (cls.offset + 4) if now == cls.last_time else 0
                    msg_id = (now * 2 ** 32) + cls.offset
                    cls.last_time = now
                    return msg_id
                msg_id_mod.MsgId.__new__ = patched_new
                return skew
    except Exception as e:
        logger.debug(f"Clock synchronization skipped: {e}")
    return 0.0


sync_telegram_time_offset()


# ==========================================
# R2: Configuration Loading & Fallback Logic
# ==========================================

def parse_owner_ids(raw_val: Any) -> Set[int]:
    """Parse owner ID(s) from various formats (int, list, comma-separated string, JSON array)."""
    if raw_val is None:
        return set()
    if isinstance(raw_val, (int, float)):
        return {int(raw_val)}
    if isinstance(raw_val, (list, tuple, set)):
        ids = set()
        for x in raw_val:
            try:
                ids.add(int(x))
            except (ValueError, TypeError):
                pass
        return ids
    if isinstance(raw_val, str):
        val = raw_val.strip()
        if not val:
            return set()
        if val.startswith("[") and val.endswith("]"):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return parse_owner_ids(parsed)
            except Exception:
                pass
        parts = re.split(r"[,;\s]+", val)
        ids = set()
        for p in parts:
            p = p.strip().strip("'\"")
            if p:
                try:
                    ids.add(int(p))
                except ValueError:
                    pass
        return ids
    return set()


def load_config(config_file: Optional[Union[str, Path]] = None, env_file: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """
    Load configuration with resolution order:
    1. System Environment Variables
    2. .env file
    3. config.json fallback
    """
    # Load .env without overriding existing environment variables
    if env_file:
        load_dotenv(dotenv_path=env_file, override=False)
    else:
        load_dotenv(override=False)

    # Read config.json fallback if available
    json_data: Dict[str, Any] = {}
    config_path = Path(config_file) if config_file else Path(__file__).with_name('config.json')
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
        except Exception as e:
            logger.warning(f"Unable to read JSON configuration from {config_path}: {e}")
            json_data = {}

    def get_val(key: str) -> Optional[Any]:
        env_val = os.environ.get(key)
        if env_val is not None and str(env_val).strip() != "":
            return env_val.strip()
        return json_data.get(key)

    token = get_val("TOKEN")

    api_id_raw = get_val("ID")
    try:
        api_id = int(api_id_raw) if api_id_raw is not None and str(api_id_raw).strip() != "" else None
    except (ValueError, TypeError):
        api_id = api_id_raw

    api_hash = get_val("HASH")

    ss_val = get_val("STRING")
    if ss_val is not None:
        ss_str = str(ss_val).strip()
        ss_val = None if (not ss_str or ss_str.lower() == "none") else ss_str

    owner_raw = os.environ.get("OWNER_ID")
    if owner_raw is None or str(owner_raw).strip() == "":
        owner_raw = json_data.get("OWNER_ID")
    owner_ids = parse_owner_ids(owner_raw)

    return {
        "TOKEN": token,
        "ID": api_id,
        "HASH": api_hash,
        "STRING": ss_val,
        "OWNER_ID": owner_ids,
        "RAW_JSON": json_data,
    }


CONFIG = load_config()

owner_ids = CONFIG["OWNER_ID"]
bot_token = CONFIG["TOKEN"]
api_hash = CONFIG["HASH"]
api_id = CONFIG["ID"]
ss = CONFIG["STRING"]


# ==========================================
# R1: FloodWait Automatic Retry Handling
# ==========================================

FLOODWAIT_METHODS = [
    "send_message",
    "edit_message_text",
    "delete_messages",
    "copy_message",
    "copy_media_group",
    "send_document",
    "send_video",
    "send_animation",
    "send_sticker",
    "send_voice",
    "send_audio",
    "send_photo",
    "send_media_group",
    "get_messages",
    "get_media_group",
    "join_chat",
    "download_media",
]


def retry_on_floodwait(func):
    """Decorator/wrapper to catch FloodWait and retry after awaiting (e.value + 1)."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        while True:
            try:
                return await func(*args, **kwargs)
            except FloodWait as e:
                wait_time = int(getattr(e, "value", getattr(e, "x", 0)) or 0) + 1
                logger.warning(
                    f"FloodWait exception in {getattr(func, '__name__', str(func))}. "
                    f"Awaiting {wait_time}s before retrying."
                )
                await asyncio.sleep(wait_time)
    return wrapper


async def call_with_floodwait(func, *args, **kwargs):
    """Execute a callable or coroutine with automatic FloodWait retry."""
    while True:
        try:
            return await func(*args, **kwargs)
        except FloodWait as e:
            wait_time = int(getattr(e, "value", getattr(e, "x", 0)) or 0) + 1
            logger.warning(f"FloodWait encountered: awaiting {wait_time}s before retrying.")
            await asyncio.sleep(wait_time)


def wrap_client_with_floodwait(client: Optional[Client]) -> Optional[Client]:
    """Automatically attach FloodWait retry handling to Telegram client RPC methods."""
    if client is None:
        return None
    for method_name in FLOODWAIT_METHODS:
        if hasattr(client, method_name):
            orig_method = getattr(client, method_name)
            if getattr(orig_method, "_is_floodwait_wrapped", False) is True:
                continue
            wrapped = retry_on_floodwait(orig_method)
            wrapped._is_floodwait_wrapped = True
            setattr(client, method_name, wrapped)
    return client


# max_concurrent_transmissions=10 enables up to 10 parallel DC connections for file transfers
bot = Client("mybot", api_id=api_id, api_hash=api_hash, bot_token=bot_token, max_concurrent_transmissions=10)
wrap_client_with_floodwait(bot)

if ss is not None:
    acc = Client("myacc", api_id=api_id, api_hash=api_hash, session_string=ss, max_concurrent_transmissions=10)
    wrap_client_with_floodwait(acc)
else:
    acc = None

# Lock for user session RPC calls (get_messages, join_chat) to prevent concurrent socket reads
acc_lock = asyncio.Lock()
# Semaphore for processing multiple messages concurrently
concurrency_sem = asyncio.Semaphore(5)

MAX_MESSAGE_RANGE = 100
MAX_MEDIA_GROUP_SIZE = 10
EDIT_THROTTLE_SECONDS = 1.8
MAX_BOT_FILE_SIZE = 2000 * 1024 * 1024  # 2 GB Telegram Bot API upload limit

THUMB_DIR = Path("downloads/thumbnails")
CAPTION_DIR = Path("downloads/captions")
DONE_MSG_DIR = Path("downloads/custom_msgs")
THUMB_DIR.mkdir(parents=True, exist_ok=True)
CAPTION_DIR.mkdir(parents=True, exist_ok=True)
DONE_MSG_DIR.mkdir(parents=True, exist_ok=True)


def get_user_thumb(user_id: int) -> Optional[str]:
    """Retrieve path to user's custom thumbnail if it exists."""
    thumb_path = THUMB_DIR / f"{user_id}.jpg"
    return str(thumb_path) if thumb_path.exists() else None


def set_user_thumb(user_id: int, file_path: str):
    """Save custom thumbnail for a user."""
    target = THUMB_DIR / f"{user_id}.jpg"
    shutil.copyfile(file_path, target)


def del_user_thumb(user_id: int) -> bool:
    """Delete custom thumbnail for a user."""
    target = THUMB_DIR / f"{user_id}.jpg"
    if target.exists():
        try:
            target.unlink()
            return True
        except Exception:
            pass
    return False


def get_user_caption_template(user_id: int) -> Optional[str]:
    """Retrieve user's custom caption template."""
    caption_path = CAPTION_DIR / f"{user_id}.txt"
    if caption_path.exists():
        try:
            return caption_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


def set_user_caption_template(user_id: int, template: str):
    """Save custom caption template for a user."""
    target = CAPTION_DIR / f"{user_id}.txt"
    target.write_text(template, encoding="utf-8")


def del_user_caption_template(user_id: int) -> bool:
    """Delete custom caption template for a user."""
    target = CAPTION_DIR / f"{user_id}.txt"
    if target.exists():
        try:
            target.unlink()
            return True
        except Exception:
            pass
    return False


def apply_caption_template(user_id: int, original_caption: Optional[str], filename: Optional[str] = None) -> Optional[str]:
    """Format caption according to user's saved template with {caption} and {filename} variables."""
    template = get_user_caption_template(user_id)
    if not template:
        return original_caption
    res = template.replace("{caption}", original_caption or "")
    res = res.replace("{filename}", filename or "")
    return res.strip()


def get_user_custom_msg(user_id: int) -> Optional[str]:
    """Retrieve user's custom completion message note."""
    msg_path = DONE_MSG_DIR / f"{user_id}.txt"
    if msg_path.exists():
        try:
            return msg_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


def set_user_custom_msg(user_id: int, message_text: str):
    """Save custom completion message note for a user."""
    target = DONE_MSG_DIR / f"{user_id}.txt"
    target.write_text(message_text, encoding="utf-8")


def del_user_custom_msg(user_id: int) -> bool:
    """Delete custom completion message note for a user."""
    target = DONE_MSG_DIR / f"{user_id}.txt"
    if target.exists():
        try:
            target.unlink()
            return True
        except Exception:
            pass
    return False


def format_time_duration(seconds: float) -> str:
    """Format elapsed seconds into readable human string."""
    if seconds < 1.0:
        return f"{seconds:.2f}s"
    elif seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes = int(seconds) // 60
    rem_sec = seconds - (minutes * 60)
    return f"{minutes}m {rem_sec:.1f}s"


def build_completion_message(
    file_name: str,
    file_size: int,
    msg_type: str,
    download_duration: float,
    upload_duration: float,
    bot_username: Optional[str] = None,
    custom_note: Optional[str] = None,
) -> str:
    """Build a detailed completion report card with download and upload statistics."""
    size_str = format_size(file_size) if file_size > 0 else "Unknown"
    down_str = format_time_duration(download_duration)
    up_str = format_time_duration(upload_duration)
    total_duration = download_duration + upload_duration
    total_str = format_time_duration(total_duration)

    down_speed_str = ""
    if download_duration > 0 and file_size > 0:
        avg_down_speed = file_size / download_duration
        down_speed_str = f" ({format_speed(avg_down_speed)})"

    up_speed_str = ""
    if upload_duration > 0 and file_size > 0:
        avg_up_speed = file_size / upload_duration
        up_speed_str = f" ({format_speed(avg_up_speed)})"

    via_str = f"\n🤖 **Downloaded via:** @{bot_username}" if bot_username else ""
    note_str = f"\n\n💬 **Note:** {custom_note}" if custom_note else ""

    return (
        "✅ **Download Completed!**\n\n"
        f"📄 **File:** `{file_name}`\n"
        f"📦 **Size:** `{size_str}`\n"
        f"📁 **Type:** `{msg_type}`\n\n"
        f"⏱ **Download Time:** `{down_str}`{down_speed_str}\n"
        f"🚀 **Upload Time:** `{up_str}`{up_speed_str}\n"
        f"⏳ **Total Time:** `{total_str}`"
        f"{note_str}"
        f"{via_str}"
    )


async def send_completion_report(
    chat_id: int,
    file_name: str,
    file_size: int,
    msg_type: str,
    download_duration: float,
    upload_duration: float,
    thumb_path: Optional[str] = None,
    user_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
):
    """Send a separate message detailing file transfer statistics as a clean text card (no thumbnail attached on completion)."""
    bot_username = getattr(getattr(bot, "me", None), "username", None)
    custom_note = get_user_custom_msg(user_id) if user_id else None
    text = build_completion_message(
        file_name=file_name,
        file_size=file_size,
        msg_type=msg_type,
        download_duration=download_duration,
        upload_duration=upload_duration,
        bot_username=bot_username,
        custom_note=custom_note,
    )

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
            )
        except Exception as e:
            logger.warning(f"Could not send completion report: {e}")


def check_file_size_limit(msg: Message) -> Tuple[bool, int]:
    """Check if media in message exceeds Telegram's 2 GB bot upload limit."""
    media_obj = getattr(msg, "document", None) or getattr(msg, "video", None) or getattr(msg, "audio", None)
    raw_size = getattr(media_obj, "file_size", 0)
    try:
        size = int(raw_size)
    except (ValueError, TypeError):
        size = 0
    return size > MAX_BOT_FILE_SIZE, size


def generate_video_thumbnail(video_path: str) -> Optional[str]:
    """Extract a thumbnail frame from video using ffmpeg if available."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin or not os.path.exists(video_path):
        return None
    thumb_path = video_path + "_thumb.jpg"
    try:
        import subprocess
        for ss in ["00:00:01", "00:00:00"]:
            cmd = [
                ffmpeg_bin,
                "-ss", ss,
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                "-y",
                thumb_path,
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if res.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                return thumb_path
    except Exception:
        pass
    return None


def extract_audio_thumbnail(audio_path: str) -> Optional[str]:
    """Extract embedded album artwork from audio file using ffmpeg if available."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin or not os.path.exists(audio_path):
        return None
    thumb_path = audio_path + "_thumb.jpg"
    try:
        import subprocess
        cmd = [
            ffmpeg_bin,
            "-i", audio_path,
            "-an",
            "-vcodec", "copy",
            "-y",
            thumb_path,
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if res.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None


async def start_web_server(port: int = 8080):
    """Lightweight HTTP server for cloud platform healthcheck pings (Render, Koyeb, Railway, InstaCloud)."""
    async def handle_client(reader, writer):
        try:
            await reader.readline()
            response_body = json.dumps({
                "status": "ok",
                "uptime": get_readable_time(time.time() - BOT_START_TIME),
                "active_tasks": len(ACTIVE_TASKS),
            }).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + response_body
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handle_client, "0.0.0.0", port)
        logger.info(f"Health check web server running on port {port}")
        return server
    except Exception as e:
        logger.warning(f"Failed to start health check server on port {port}: {e}")
        return None


# ==========================================
# R3: Rich Visual Progress Formatting
# ==========================================

def make_progress_bar(percentage: float, length: int = 10, filled_char: str = "▰", empty_char: str = "▱") -> str:
    """Generate visual block progress bar: e.g. [▰▰▰▰▰▰▱▱▱▱] 65.0%"""
    clamped_pct = max(0.0, min(100.0, float(percentage)))
    filled_length = int(round(clamped_pct / 100.0 * length))
    filled_length = max(0, min(length, filled_length))
    bar = (filled_char * filled_length) + (empty_char * (length - filled_length))
    return f"[{bar}] {clamped_pct:.1f}%"


def format_size(size_bytes: Optional[Union[int, float]]) -> str:
    """Format size in human-readable units (B, KB, MB, GB, TB)."""
    if size_bytes is None or size_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    unit_idx = 0
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(size)} B"
    return f"{size:.2f} {units[unit_idx]}"


def format_speed(speed_bytes_per_sec: Optional[Union[int, float]]) -> str:
    """Format speed in human-readable units/sec (e.g. 4.2 MB/s)."""
    if speed_bytes_per_sec is None or speed_bytes_per_sec <= 0:
        return "0 B/s"
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    speed = float(speed_bytes_per_sec)
    unit_idx = 0
    while speed >= 1024.0 and unit_idx < len(units) - 1:
        speed /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(speed)} B/s"
    return f"{speed:.1f} {units[unit_idx]}"


def format_eta(seconds: Optional[Union[int, float]]) -> str:
    """Format ETA in mm:ss format."""
    if seconds is None or seconds < 0:
        return "00:00"
    total_seconds = int(seconds)
    minutes = total_seconds // 60
    sec = total_seconds % 60
    return f"{minutes:02d}:{sec:02d}"


def _safe_str(val: Any, default: str) -> str:
    """Safely convert attribute value to string, ignoring mock objects."""
    if val is None or not isinstance(val, str):
        return default
    return val


def extract_media_info(msg: Message) -> Tuple[str, int, str]:
    """Extract (file_name, file_size, media_type) from a Telegram message before downloading."""
    if getattr(msg, "document", None):
        doc = msg.document
        name = getattr(doc, "file_name", None)
        size = getattr(doc, "file_size", 0)
        try:
            size = int(size)
        except (ValueError, TypeError):
            size = 0
        return _safe_str(name, "document"), size, "Document"
    if getattr(msg, "video", None):
        vid = msg.video
        name = getattr(vid, "file_name", None)
        size = getattr(vid, "file_size", 0)
        try:
            size = int(size)
        except (ValueError, TypeError):
            size = 0
        return _safe_str(name, "video.mp4"), size, "Video"
    if getattr(msg, "audio", None):
        aud = msg.audio
        name = getattr(aud, "file_name", None) or getattr(aud, "title", None)
        size = getattr(aud, "file_size", 0)
        try:
            size = int(size)
        except (ValueError, TypeError):
            size = 0
        return _safe_str(name, "audio.mp3"), size, "Audio"
    if getattr(msg, "photo", None):
        return "photo.jpg", 0, "Photo"
    if getattr(msg, "animation", None):
        anim = msg.animation
        name = getattr(anim, "file_name", None)
        size = getattr(anim, "file_size", 0)
        try:
            size = int(size)
        except (ValueError, TypeError):
            size = 0
        return _safe_str(name, "animation.gif"), size, "Animation"
    if getattr(msg, "voice", None):
        raw_size = getattr(msg.voice, "file_size", 0)
        try:
            size = int(raw_size)
        except (ValueError, TypeError):
            size = 0
        return "voice_note.ogg", size, "Voice"
    if getattr(msg, "sticker", None):
        set_name = getattr(msg.sticker, "set_name", "sticker")
        return _safe_str(set_name, "sticker"), 0, "Sticker"
    return "file", 0, "Media"


def render_progress_text(
    action: str,
    current: int,
    total: int,
    speed: float,
    eta: float,
    elapsed: float = 0.0,
    file_name: Optional[str] = None,
    media_type: Optional[str] = None,
) -> str:
    """Calculate and render rich progress status message text."""
    try:
        total = max(int(total), 0)
    except (ValueError, TypeError):
        total = 0
    try:
        current = max(int(current), 0)
    except (ValueError, TypeError):
        current = 0
    try:
        speed = max(float(speed), 0.0)
    except (ValueError, TypeError):
        speed = 0.0
    try:
        eta = max(float(eta), 0.0)
    except (ValueError, TypeError):
        eta = 0.0
    try:
        elapsed = max(float(elapsed), 0.0)
    except (ValueError, TypeError):
        elapsed = 0.0

    if file_name is not None and not isinstance(file_name, str):
        file_name = str(file_name)
    if media_type is not None and not isinstance(media_type, str):
        media_type = str(media_type)
    pct = (current * 100.0 / total) if total > 0 else 0.0
    bar = make_progress_bar(pct)
    cur_str = format_size(current)
    tot_str = format_size(total) if total > 0 else "Unknown"
    spd_str = format_speed(speed) if speed > 0 else "Calculating..."
    eta_str = format_eta(eta) if (speed > 0 and eta > 0) else "Calculating..."
    elap_str = format_eta(elapsed)

    action_lower = action.lower()
    if "down" in action_lower:
        header = "📥 **Downloading Content...**"
        engine_str = "Fast MTProto Stream"
    elif "up" in action_lower:
        header = "📤 **Uploading to Telegram...**"
        engine_str = "Fast Bot Upload"
    else:
        header = f"⚡ **{action}...**"
        engine_str = "Processing"

    file_block = ""
    if file_name:
        display_name = file_name if len(file_name) <= 36 else file_name[:33] + "..."
        type_badge = f"  •  📁 `{media_type}`" if media_type else ""
        file_block = (
            f"📄 **File:** `{display_name}`\n"
            f"📦 **Size:** `{tot_str}`{type_badge}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )

    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{file_block}"
        f"{bar}\n\n"
        f"⚡ **Speed:** `{spd_str}`\n"
        f"⏳ **ETA:** `{eta_str}`  •  ⏱ **Elapsed:** `{elap_str}`\n"
        f"📊 **Progress:** `{cur_str}` / `{tot_str}`\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 _Engine: {engine_str}_"
    )


# ==========================================
# R4: Interactive Task Cancellation & Tracking
# ==========================================

class TaskContext:
    """Context holding state, files, and cancellation token for an active transfer task."""
    def __init__(self, task_id: str, initiator_id: int, chat_id: int, smsg_id: Optional[int] = None):
        self.task_id = task_id
        self.initiator_id = initiator_id
        self.chat_id = chat_id
        self.smsg_id = smsg_id
        self.smsg: Optional[Message] = None
        self.async_task: Optional[asyncio.Task] = None
        self.is_cancelled: bool = False
        self.tracked_files: Set[str] = set()
        self.start_time: float = time.time()

    def track_file(self, path: Optional[str]):
        if path:
            self.tracked_files.add(os.path.abspath(path))

    def cleanup_files(self):
        for file_path in list(self.tracked_files):
            remove_file(file_path)
            self.tracked_files.discard(file_path)

    def cancel(self):
        self.is_cancelled = True
        if self.async_task and not self.async_task.done():
            self.async_task.cancel()
        self.cleanup_files()


ACTIVE_TASKS: Dict[str, TaskContext] = {}
SMSG_TASK_MAP: Dict[Tuple[int, int], str] = {}


def register_task(task_ctx: TaskContext):
    ACTIVE_TASKS[task_ctx.task_id] = task_ctx
    if task_ctx.smsg_id:
        SMSG_TASK_MAP[(task_ctx.chat_id, task_ctx.smsg_id)] = task_ctx.task_id


def unregister_task(task_id: str):
    task_ctx = ACTIVE_TASKS.pop(task_id, None)
    if task_ctx and task_ctx.smsg_id:
        SMSG_TASK_MAP.pop((task_ctx.chat_id, task_ctx.smsg_id), None)


def get_task(task_id: str) -> Optional[TaskContext]:
    return ACTIVE_TASKS.get(task_id)


def get_task_by_smsg(chat_id: int, smsg_id: int) -> Optional[TaskContext]:
    task_id = SMSG_TASK_MAP.get((chat_id, smsg_id))
    if task_id:
        return ACTIVE_TASKS.get(task_id)
    return None


def is_task_cancelled(task_id: Optional[str]) -> bool:
    if not task_id:
        return False
    ctx = ACTIVE_TASKS.get(task_id)
    return ctx.is_cancelled if ctx else False


def get_cancel_button(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]
    ])


# In-memory status tracker to eliminate disk I/O during progress updates
STATUS_TRACKER = {}


def remove_file(path: Optional[str]):
    if path is not None and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        # Clean up any partial download temporary file
        temp_path = path + ".temp"
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


async def delete_status_message(message: Message, smsg: Message):
    try:
        await bot.delete_messages(message.chat.id, [smsg.id])
    except Exception:
        pass


async def delayed_delete_status(chat_id: int, smsg_id: int, delay: float = 2.0):
    try:
        await asyncio.sleep(delay)
        await bot.delete_messages(chat_id, [smsg_id])
    except Exception:
        pass


async def status_updater(
    key: Tuple[int, int, str],
    message: Message,
    action_title: str,
    task_id: Optional[str] = None,
    file_name: Optional[str] = None,
    media_type: Optional[str] = None,
):
    """Periodically update Telegram status message with fast responsive progress edits (1.5-2.0s)."""
    cancel_markup = get_cancel_button(task_id) if task_id else None
    last_rendered_text = ""
    try:
        # Immediate fast check: wait briefly for initial packet to appear
        for _ in range(4):
            if key in STATUS_TRACKER:
                break
            await asyncio.sleep(0.1)

        while key in STATUS_TRACKER:
            data = STATUS_TRACKER.get(key)
            if data:
                now = time.time()
                elapsed = max(0.0, now - data.get("start_time", now))
                text = render_progress_text(
                    action=action_title,
                    current=data.get("current", 0),
                    total=data.get("total", 0),
                    speed=data.get("speed", 0.0),
                    eta=data.get("eta", 0.0),
                    elapsed=elapsed,
                    file_name=file_name or data.get("file_name"),
                    media_type=media_type or data.get("media_type"),
                )
                if text != last_rendered_text:
                    try:
                        await bot.edit_message_text(
                            message.chat.id,
                            message.id,
                            text,
                            reply_markup=cancel_markup,
                        )
                        last_rendered_text = text
                    except MessageNotModified:
                        pass
                    except MessageIdInvalid:
                        break
                    except Exception:
                        try:
                            await bot.edit_message_caption(
                                message.chat.id,
                                message.id,
                                caption=text,
                                reply_markup=cancel_markup,
                            )
                            last_rendered_text = text
                        except MessageNotModified:
                            pass
                        except MessageIdInvalid:
                            break
                        except Exception:
                            pass
            await asyncio.sleep(EDIT_THROTTLE_SECONDS)
    except asyncio.CancelledError:
        return


async def downstatus(key, message, task_id: Optional[str] = None, file_name: Optional[str] = None, media_type: Optional[str] = None):
    effective_task_id = task_id
    if not effective_task_id:
        task_ctx = get_task_by_smsg(message.chat.id, message.id)
        if task_ctx:
            effective_task_id = task_ctx.task_id
    await status_updater(key, message, "Downloading", effective_task_id, file_name, media_type)


async def upstatus(key, message, task_id: Optional[str] = None, file_name: Optional[str] = None, media_type: Optional[str] = None):
    effective_task_id = task_id
    if not effective_task_id:
        task_ctx = get_task_by_smsg(message.chat.id, message.id)
        if task_ctx:
            effective_task_id = task_ctx.task_id
    await status_updater(key, message, "Uploading", effective_task_id, file_name, media_type)


def progress(
    current: int,
    total: int,
    smsg: Message,
    type_str: str,
    task_id: Optional[str] = None,
    file_name: Optional[str] = None,
    media_type: Optional[str] = None,
):
    """Track download/upload progress with moving-window smoothed speed and accurate ETA."""
    effective_task_id = task_id
    if not effective_task_id:
        task_ctx = get_task_by_smsg(smsg.chat.id, smsg.id)
        if task_ctx:
            effective_task_id = task_ctx.task_id

    if effective_task_id and is_task_cancelled(effective_task_id):
        raise pyrogram.StopTransmission("Task was cancelled by user.")

    key = (smsg.chat.id, smsg.id, type_str)
    now = time.time()

    if key not in STATUS_TRACKER:
        STATUS_TRACKER[key] = {
            "start_time": now,
            "last_time": now,
            "last_current": current,
            "current": current,
            "total": total,
            "speed": 0.0,
            "eta": 0.0,
            "task_id": effective_task_id,
            "type": type_str,
            "file_name": file_name,
            "media_type": media_type,
        }
    else:
        entry = STATUS_TRACKER[key]
        dt = now - entry.get("last_time", now)
        # Update speed calculation every 0.3s for smooth and responsive metrics
        if dt >= 0.3:
            db = current - entry.get("last_current", 0)
            if db > 0 and dt > 0:
                instant_speed = db / dt
                prev_speed = entry.get("speed", 0.0)
                # Exponential smoothing: 75% instant, 25% previous
                speed = (instant_speed * 0.75) + (prev_speed * 0.25) if prev_speed > 0 else instant_speed
            else:
                speed = entry.get("speed", 0.0)

            remaining = max(0, total - current)
            eta = (remaining / speed) if speed > 0 else 0.0

            entry["speed"] = speed
            entry["eta"] = eta
            entry["last_time"] = now
            entry["last_current"] = current

        entry["current"] = current
        entry["total"] = total
        if file_name and not entry.get("file_name"):
            entry["file_name"] = file_name
        if media_type and not entry.get("media_type"):
            entry["media_type"] = media_type


@bot.on_callback_query(filters.regex(r"^cancel_(.+)"))
async def cancel_callback_handler(client: Client, callback_query: CallbackQuery):
    """Handle interactive cancellation button click."""
    data = callback_query.data or ""
    task_id = data.split("cancel_", 1)[1] if "cancel_" in data else ""
    task_ctx = get_task(task_id)

    if not task_ctx:
        await callback_query.answer("⚠️ Task has already completed or expired.", show_alert=False)
        try:
            await callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    caller_id = callback_query.from_user.id if callback_query.from_user else None
    if caller_id != task_ctx.initiator_id and caller_id not in owner_ids:
        await callback_query.answer("⛔ You are not authorized to cancel this task.", show_alert=True)
        return

    # Cancel task, abort network transfers, clean up disk
    task_ctx.cancel()
    await callback_query.answer("❌ Task cancelled.", show_alert=False)

    try:
        await bot.edit_message_text(
            task_ctx.chat_id,
            task_ctx.smsg_id,
            "❌ **Task Cancelled by user.**",
            reply_markup=None,
        )
    except Exception:
        pass


def is_owner(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in owner_ids


async def deny_access(message: Message):
    await bot.send_message(message.chat.id, "**You are not authorized to use this bot.**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["start"]))
async def send_start(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    await bot.send_message(
        message.chat.id,
        f"__👋 Hi **{message.from_user.mention}**, I am Save Restricted Bot, I can send you restricted content by it's post link__\n\n{USAGE}",
        reply_to_message_id=message.id,
    )


def get_readable_time(seconds: Union[int, float]) -> str:
    """Format seconds into human-readable duration (e.g. 2d 4h 15m 30s)."""
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def get_system_stats() -> Dict[str, Any]:
    """Collect system and process performance metrics."""
    uptime = get_readable_time(time.time() - BOT_START_TIME)

    try:
        total_disk, used_disk, free_disk = shutil.disk_usage(".")
        disk_str = f"{format_size(free_disk)} free / {format_size(total_disk)}"
    except Exception:
        disk_str = "N/A"

    try:
        import psutil
        process = psutil.Process()
        proc_mem = format_size(process.memory_info().rss)
        sys_mem = psutil.virtual_memory()
        mem_str = f"{proc_mem} (Bot) | {sys_mem.percent}% (System)"
        cpu_pct = f"{psutil.cpu_percent(interval=None)}%"
    except Exception:
        mem_str = "N/A"
        cpu_pct = "N/A"

    return {
        "uptime": uptime,
        "active_tasks": len(ACTIVE_TASKS),
        "disk": disk_str,
        "memory": mem_str,
        "cpu": cpu_pct,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "pyrogram_version": getattr(pyrogram, "__version__", "unknown"),
        "user_session": "Connected" if acc is not None else "Not Set",
    }


@bot.on_message(filters.command(["ping"]))
async def ping_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    start = time.time()
    reply = await bot.send_message(message.chat.id, "🏓 **Pinging...**", reply_to_message_id=message.id)
    latency_ms = (time.time() - start) * 1000.0
    await bot.edit_message_text(
        message.chat.id,
        reply.id,
        f"🏓 **Pong!**\n📶 **Latency:** `{latency_ms:.2f} ms`",
    )


@bot.on_message(filters.command(["stats", "status"]))
async def stats_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    stats = get_system_stats()
    text = (
        "📊 **Bot Diagnostics & System Stats**\n\n"
        f"⏱️ **Uptime:** `{stats['uptime']}`\n"
        f"🔄 **Active Tasks:** `{stats['active_tasks']}`\n"
        f"🧠 **Memory:** `{stats['memory']}`\n"
        f"⚡ **CPU Usage:** `{stats['cpu']}`\n"
        f"💾 **Disk Space:** `{stats['disk']}`\n"
        f"👤 **User Session:** `{stats['user_session']}`\n"
        f"🐍 **Python:** `v{stats['python_version']}` | **Pyrogram:** `v{stats['pyrogram_version']}`"
    )
    await bot.send_message(message.chat.id, text, reply_to_message_id=message.id)


@bot.on_message(filters.command(["setthumb"]))
async def setthumb_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    photo_msg = message.reply_to_message if message.reply_to_message and message.reply_to_message.photo else None
    if not photo_msg and message.photo:
        photo_msg = message
    if not photo_msg:
        await bot.send_message(
            message.chat.id,
            "⚠️ **Please reply to a photo with `/setthumb` to set your custom thumbnail.**",
            reply_to_message_id=message.id,
        )
        return
    downloaded = await bot.download_media(photo_msg)
    if downloaded:
        set_user_thumb(message.from_user.id if message.from_user else 0, downloaded)
        remove_file(downloaded)
        await bot.send_message(message.chat.id, "✅ **Custom thumbnail saved successfully!**", reply_to_message_id=message.id)
    else:
        await bot.send_message(message.chat.id, "❌ **Failed to download thumbnail.**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["delthumb"]))
async def delthumb_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    user_id = message.from_user.id if message.from_user else 0
    if del_user_thumb(user_id):
        await bot.send_message(message.chat.id, "🗑️ **Custom thumbnail deleted.**", reply_to_message_id=message.id)
    else:
        await bot.send_message(message.chat.id, "ℹ️ **No custom thumbnail was found to delete.**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["showthumb", "viewthumb"]))
async def showthumb_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    user_id = message.from_user.id if message.from_user else 0
    thumb = get_user_thumb(user_id)
    if thumb:
        await bot.send_photo(message.chat.id, thumb, caption="🖼️ **Your Current Custom Thumbnail**", reply_to_message_id=message.id)
    else:
        await bot.send_message(message.chat.id, "ℹ️ **You do not have a custom thumbnail set.**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["setcaption"]))
async def setcaption_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        await bot.send_message(
            message.chat.id,
            "⚠️ **Usage:** `/setcaption <template>`\n\n"
            "Supported variables:\n"
            "• `{caption}` - Original post caption\n"
            "• `{filename}` - File name\n\n"
            "Example:\n`/setcaption 📁 {filename}\n\n{caption}\n\nSaved by MyBot`",
            reply_to_message_id=message.id,
        )
        return
    template = parts[1].strip()
    user_id = message.from_user.id if message.from_user else 0
    set_user_caption_template(user_id, template)
    await bot.send_message(message.chat.id, "✅ **Custom caption template updated!**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["delcaption"]))
async def delcaption_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    user_id = message.from_user.id if message.from_user else 0
    if del_user_caption_template(user_id):
        await bot.send_message(message.chat.id, "🗑️ **Custom caption template deleted.**", reply_to_message_id=message.id)
    else:
        await bot.send_message(message.chat.id, "ℹ️ **No custom caption template was set.**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["showcaption", "viewcaption"]))
async def showcaption_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    user_id = message.from_user.id if message.from_user else 0
    template = get_user_caption_template(user_id)
    if template:
        await bot.send_message(message.chat.id, f"📝 **Your Active Caption Template:**\n\n`{template}`", reply_to_message_id=message.id)
    else:
        await bot.send_message(message.chat.id, "ℹ️ **No custom caption template set. Using original captions.**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["setmsg", "setdone"]))
async def setmsg_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        await bot.send_message(
            message.chat.id,
            "⚠️ **Usage:** `/setmsg <text>`\n\n"
            "Set a custom note to include in the download completion card.\n\n"
            "Example:\n`/setmsg Thank you for downloading! Join @mychannel`",
            reply_to_message_id=message.id,
        )
        return
    custom_text = parts[1].strip()
    user_id = message.from_user.id if message.from_user else 0
    set_user_custom_msg(user_id, custom_text)
    await bot.send_message(
        message.chat.id,
        "✅ **Custom completion message saved!** It will appear on your download summary cards.",
        reply_to_message_id=message.id,
    )


@bot.on_message(filters.command(["delmsg", "deldone"]))
async def delmsg_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    user_id = message.from_user.id if message.from_user else 0
    if del_user_custom_msg(user_id):
        await bot.send_message(message.chat.id, "🗑️ **Custom completion message deleted.**", reply_to_message_id=message.id)
    else:
        await bot.send_message(message.chat.id, "ℹ️ **No custom completion message was found.**", reply_to_message_id=message.id)


@bot.on_message(filters.command(["showmsg", "viewmsg", "showdone"]))
async def showmsg_handler(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return
    user_id = message.from_user.id if message.from_user else 0
    custom_text = get_user_custom_msg(user_id)
    if custom_text:
        await bot.send_message(
            message.chat.id,
            f"📝 **Your Active Completion Message Note:**\n\n`{custom_text}`",
            reply_to_message_id=message.id,
        )
    else:
        await bot.send_message(message.chat.id, "ℹ️ **No custom completion message note set.**", reply_to_message_id=message.id)


async def send_with_user_session(message: Message, chatid: Union[int, str], msgid: int, processed_media_groups: set):
    if acc is None:
        await bot.send_message(message.chat.id, f"**String Session is not Set**", reply_to_message_id=message.id)
        return
    await handle_private(message, chatid, msgid, processed_media_groups)


def parse_tme_link(text: str) -> Optional[Dict[str, Any]]:
    link = text.strip()

    invite_match = re.match(r"^https://t\.me/(?:\+|joinchat/)[^/\s]+/?$", link)
    if invite_match:
        return {"type": "invite", "link": link}

    # Private channel with topic/thread: https://t.me/c/CHAT_ID/TOPIC_ID/MSG_RANGE
    match = re.match(r"^https://t\.me/c/([^/?#\s]+)/[^/?#\s]+/([0-9\s]+(?:-[0-9\s]+)?)(?:\?single)?$", link)
    if match:
        chat_id, message_range = match.groups()
        parsed_range = parse_message_range(message_range)
        if parsed_range is None:
            return None
        from_id, to_id = parsed_range
        return {"type": "private", "chatid": int("-100" + chat_id), "from_id": from_id, "to_id": to_id}

    # Private channel without topic: https://t.me/c/CHAT_ID/MSG_RANGE
    match = re.match(r"^https://t\.me/c/([^/?#\s]+)/([0-9\s]+(?:-[0-9\s]+)?)(?:\?single)?$", link)
    if match:
        chat_id, message_range = match.groups()
        parsed_range = parse_message_range(message_range)
        if parsed_range is None:
            return None
        from_id, to_id = parsed_range
        return {"type": "private", "chatid": int("-100" + chat_id), "from_id": from_id, "to_id": to_id}

    # Bot chat: https://t.me/b/BOTNAME/MSG_RANGE
    match = re.match(r"^https://t\.me/b/([^/?#\s]+)/([0-9\s]+(?:-[0-9\s]+)?)(?:\?single)?$", link)
    if match:
        target, message_range = match.groups()
        parsed_range = parse_message_range(message_range)
        if parsed_range is None:
            return None
        from_id, to_id = parsed_range
        return {"type": "bot", "chatid": target, "from_id": from_id, "to_id": to_id}

    # Public channel with topic/thread: https://t.me/CHANNEL/TOPIC_ID/MSG_RANGE
    match = re.match(r"^https://t\.me/([^/?#\s]+)/[^/?#\s]+/([0-9\s]+(?:-[0-9\s]+)?)(?:\?single)?$", link)
    if match:
        username, message_range = match.groups()
        parsed_range = parse_message_range(message_range)
        if parsed_range is None:
            return None
        from_id, to_id = parsed_range
        return {"type": "public", "chatid": username, "from_id": from_id, "to_id": to_id}

    # Public channel without topic: https://t.me/CHANNEL/MSG_RANGE
    match = re.match(r"^https://t\.me/([^/?#\s]+)/([0-9\s]+(?:-[0-9\s]+)?)(?:\?single)?$", link)
    if match:
        username, message_range = match.groups()
        parsed_range = parse_message_range(message_range)
        if parsed_range is None:
            return None
        from_id, to_id = parsed_range
        return {"type": "public", "chatid": username, "from_id": from_id, "to_id": to_id}

    return None


def parse_message_range(message_range: str) -> Optional[Tuple[int, int]]:
    parts = [part.strip() for part in message_range.split("-", 1)]
    try:
        from_id = int(parts[0])
        to_id = int(parts[1]) if len(parts) == 2 else from_id
    except ValueError:
        return None
    return from_id, to_id


async def process_single_message(message: Message, parsed_link: Dict[str, Any], msgid: int, processed_media_groups: set):
    """Process a single message from a range concurrently."""
    async with concurrency_sem:
        chatid = parsed_link["chatid"]

        if parsed_link["type"] in ("private", "bot"):
            try:
                await send_with_user_session(message, chatid, msgid, processed_media_groups)
            except (asyncio.CancelledError, pyrogram.StopTransmission):
                return
            except Exception as e:
                await bot.send_message(message.chat.id, f"**Error** : __{e}__", reply_to_message_id=message.id)
        else:
            try:
                msg = await bot.get_messages(chatid, msgid)
            except UsernameNotOccupied:
                await bot.send_message(message.chat.id, "**The username is not occupied by anyone**", reply_to_message_id=message.id)
                return
            except Exception:
                # Channel may be private or restricted for the bot; fall back to user session
                try:
                    await send_with_user_session(message, chatid, msgid, processed_media_groups)
                except (asyncio.CancelledError, pyrogram.StopTransmission):
                    return
                except Exception as e:
                    await bot.send_message(message.chat.id, f"**Error** : __{e}__", reply_to_message_id=message.id)
                return
            try:
                if msg.empty:
                    # Message is empty/deleted via bot API; fall back to user session
                    await send_with_user_session(message, chatid, msgid, processed_media_groups)
                    return
                if getattr(msg, "media_group_id", None):
                    media_group_key = (msg.chat.id, msg.media_group_id)
                    if media_group_key in processed_media_groups:
                        return
                    try:
                        await bot.copy_media_group(message.chat.id, msg.chat.id, msg.id, reply_to_message_id=message.id)
                        processed_media_groups.add(media_group_key)
                    except ValueError:
                        await bot.copy_message(message.chat.id, msg.chat.id, msg.id, reply_to_message_id=message.id)
                else:
                    await bot.copy_message(message.chat.id, msg.chat.id, msg.id, reply_to_message_id=message.id)
            except (asyncio.CancelledError, pyrogram.StopTransmission):
                return
            except Exception:
                try:
                    await send_with_user_session(message, msg.chat.id, msgid, processed_media_groups)
                except (asyncio.CancelledError, pyrogram.StopTransmission):
                    return
                except Exception as e:
                    await bot.send_message(message.chat.id, f"**Error** : __{e}__", reply_to_message_id=message.id)


@bot.on_message(filters.text)
async def save(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return

    parsed_link = parse_tme_link(message.text)
    if parsed_link is None:
        await bot.send_message(message.chat.id, "**Invalid Link**", reply_to_message_id=message.id)
        return

    if parsed_link["type"] == "invite":
        if acc is None:
            await bot.send_message(message.chat.id, "**String Session is not Set**", reply_to_message_id=message.id)
            return

        try:
            async with acc_lock:
                await acc.join_chat(parsed_link["link"])
            await bot.send_message(message.chat.id, "**Chat Joined**", reply_to_message_id=message.id)
        except UserAlreadyParticipant:
            await bot.send_message(message.chat.id, "**Chat already Joined**", reply_to_message_id=message.id)
        except InviteHashExpired:
            await bot.send_message(message.chat.id, "**Invalid Link**", reply_to_message_id=message.id)
        except Exception as e:
            await bot.send_message(message.chat.id, f"**Error** : __{e}__", reply_to_message_id=message.id)
        return

    from_id = parsed_link["from_id"]
    to_id = parsed_link["to_id"]
    if to_id < from_id:
        await bot.send_message(message.chat.id, "**Invalid Range**", reply_to_message_id=message.id)
        return
    if to_id - from_id + 1 > MAX_MESSAGE_RANGE:
        await bot.send_message(
            message.chat.id,
            f"**Range is too large. Maximum {MAX_MESSAGE_RANGE} messages allowed.**",
            reply_to_message_id=message.id,
        )
        return

    processed_media_groups = set()

    # Process messages concurrently in parallel tasks
    tasks = []
    for msgid in range(from_id, to_id + 1):
        tasks.append(process_single_message(message, parsed_link, msgid, processed_media_groups))

    batch_start = time.time()
    await asyncio.gather(*tasks, return_exceptions=True)

    if to_id > from_id:
        elapsed = time.time() - batch_start
        total_msgs = to_id - from_id + 1
        await bot.send_message(
            message.chat.id,
            f"✅ **Batch Completed:** Processed `{total_msgs}` messages in `{elapsed:.1f}s`.",
            reply_to_message_id=message.id,
        )


@bot.on_message(filters.incoming & filters.media)
async def save_media(client: Client, message: Message):
    if not is_owner(message):
        await deny_access(message)
        return

    try:
        if message.media_group_id:
            await asyncio.sleep(1)
            media_group = await bot.get_media_group(message.chat.id, message.id)
            if message.id != media_group[0].id:
                return
            await bot.copy_media_group(message.chat.id, message.chat.id, message.id, reply_to_message_id=message.id)
        else:
            await bot.copy_message(message.chat.id, message.chat.id, message.id, reply_to_message_id=message.id)
    except Exception as e:
        await bot.send_message(message.chat.id, f"**Error** : __{e}__", reply_to_message_id=message.id)


async def handle_private(message: Message, chatid: Union[int, str], msgid: int, processed_media_groups: Optional[set] = None):
    async with acc_lock:
        msg = await acc.get_messages(chatid, msgid)

    if msg is None or msg.empty:
        return

    media_group_id = getattr(msg, "media_group_id", None)

    if media_group_id:
        media_group_key = (chatid, media_group_id)
        if processed_media_groups is not None and media_group_key in processed_media_groups:
            return
        try:
            async with acc_lock:
                messages = await acc.get_media_group(chatid, msgid)
        except ValueError:
            messages = [msg]
        if processed_media_groups is not None:
            processed_media_groups.add(media_group_key)
        if len(messages) > 1:
            await handle_private_media_group(message, messages)
            return

    await handle_private_message(message, msg)


async def handle_private_message(message: Message, msg: Message):
    msg_type = get_message_type(msg)

    if "Text" == msg_type:
        await bot.send_message(message.chat.id, msg.text, entities=msg.entities, reply_to_message_id=message.id)
        return
    if msg_type is None:
        await bot.send_message(message.chat.id, "**Unsupported Message Type**", reply_to_message_id=message.id)
        return

    is_oversized, file_size = check_file_size_limit(msg)
    if is_oversized:
        size_str = format_size(file_size)
        await bot.send_message(
            message.chat.id,
            f"⚠️ **File Too Large:** `{size_str}`\nTelegram limits bot uploads to **2 GB**. This file exceeds the upload limit.",
            reply_to_message_id=message.id,
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    custom_thumb = get_user_thumb(user_id)

    # Extract upfront media information before downloading for instant UI feedback
    init_name, init_size, init_type = extract_media_info(msg)

    task_id = uuid.uuid4().hex[:10]
    task_ctx = TaskContext(
        task_id=task_id,
        initiator_id=user_id,
        chat_id=message.chat.id,
    )
    task_ctx.async_task = asyncio.current_task()
    register_task(task_ctx)

    cancel_markup = get_cancel_button(task_id)
    initial_down_text = render_progress_text(
        action="Downloading",
        current=0,
        total=init_size,
        speed=0.0,
        eta=0.0,
        elapsed=0.0,
        file_name=init_name,
        media_type=init_type,
    )

    # Fetch thumbnail preview upfront if available to display live preview during download
    preview_thumb = custom_thumb
    if not preview_thumb:
        try:
            if msg_type == "Video" and getattr(msg, "video", None) and getattr(msg.video, "thumbs", None):
                preview_thumb = await acc.download_media(msg.video.thumbs[0].file_id)
                task_ctx.track_file(preview_thumb)
            elif msg_type == "Document" and getattr(msg, "document", None) and getattr(msg.document, "thumbs", None):
                preview_thumb = await acc.download_media(msg.document.thumbs[0].file_id)
                task_ctx.track_file(preview_thumb)
            elif msg_type == "Audio" and getattr(msg, "audio", None) and getattr(msg.audio, "thumbs", None):
                preview_thumb = await acc.download_media(msg.audio.thumbs[0].file_id)
                task_ctx.track_file(preview_thumb)
        except Exception:
            preview_thumb = None

    smsg = None
    if preview_thumb and os.path.exists(preview_thumb) and os.path.getsize(preview_thumb) > 0:
        try:
            smsg = await bot.send_photo(
                message.chat.id,
                photo=preview_thumb,
                caption=initial_down_text,
                reply_to_message_id=message.id,
                reply_markup=cancel_markup,
            )
        except Exception as e:
            logger.debug(f"Failed to send thumbnail preview status message: {e}")
            smsg = None

    if not smsg:
        smsg = await bot.send_message(
            message.chat.id,
            initial_down_text,
            reply_to_message_id=message.id,
            reply_markup=cancel_markup,
        )

    task_ctx.smsg_id = smsg.id
    task_ctx.smsg = smsg
    register_task(task_ctx)

    # Key progress by the unique status message to avoid collisions across concurrent downloads
    down_key = (smsg.chat.id, smsg.id, "down")
    up_key = (smsg.chat.id, smsg.id, "up")

    down_task = asyncio.create_task(downstatus(down_key, smsg, task_id, init_name, init_type))
    up_task = None
    file = None
    thumb = preview_thumb
    sent_media = None
    file_display_name = init_name or "File"
    smsg_deleted = False

    try:
        download_start = time.time()
        # download_media runs with max_concurrent_transmissions=10 connections
        file = await acc.download_media(
            msg,
            progress=progress,
            progress_args=[smsg, "down", task_id, init_name, init_type],
        )
        download_duration = max(time.time() - download_start, 0.01)

        if task_ctx.is_cancelled or file is None:
            return
        task_ctx.track_file(file)

        file_size = os.path.getsize(file) if os.path.exists(file) else init_size

        STATUS_TRACKER.pop(down_key, None)
        if down_task and not down_task.done():
            down_task.cancel()

        if task_ctx.is_cancelled:
            return

        initial_up_text = render_progress_text(
            action="Uploading",
            current=0,
            total=file_size,
            speed=0.0,
            eta=0.0,
            elapsed=0.0,
            file_name=file_display_name,
            media_type=msg_type,
        )
        try:
            await bot.edit_message_text(message.chat.id, smsg.id, initial_up_text, reply_markup=cancel_markup)
        except Exception:
            try:
                await bot.edit_message_caption(message.chat.id, smsg.id, caption=initial_up_text, reply_markup=cancel_markup)
            except Exception:
                pass
        up_task = asyncio.create_task(upstatus(up_key, smsg, task_id, file_display_name, msg_type))

        upload_start = time.time()
        if "Document" == msg_type:
            doc_name = getattr(msg.document, "file_name", None) or os.path.basename(file)
            file_display_name = doc_name
            caption = apply_caption_template(user_id, msg.caption, doc_name)
            thumb = custom_thumb or preview_thumb
            if not thumb:
                try:
                    if msg.document.thumbs:
                        thumb = await acc.download_media(msg.document.thumbs[0].file_id)
                        task_ctx.track_file(thumb)
                except Exception:
                    thumb = None
            if not thumb and file:
                file_lower = file.lower()
                if any(file_lower.endswith(ext) for ext in [".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v"]):
                    generated_thumb = generate_video_thumbnail(file)
                    if generated_thumb:
                        thumb = generated_thumb
                        task_ctx.track_file(thumb)

            if task_ctx.is_cancelled:
                return
            sent_media = await bot.send_document(
                message.chat.id,
                file,
                thumb=thumb,
                caption=caption,
                caption_entities=msg.caption_entities if caption == msg.caption else None,
                reply_to_message_id=message.id,
                progress=progress,
                progress_args=[smsg, "up", task_id, file_display_name, msg_type],
            )

        elif "Video" == msg_type:
            vid_name = getattr(msg.video, "file_name", None) or os.path.basename(file)
            file_display_name = vid_name
            caption = apply_caption_template(user_id, msg.caption, vid_name)
            thumb = custom_thumb or preview_thumb
            if not thumb:
                try:
                    if msg.video.thumbs:
                        thumb = await acc.download_media(msg.video.thumbs[0].file_id)
                        task_ctx.track_file(thumb)
                except Exception:
                    thumb = None
            if not thumb and file:
                generated_thumb = generate_video_thumbnail(file)
                if generated_thumb:
                    thumb = generated_thumb
                    task_ctx.track_file(thumb)

            if task_ctx.is_cancelled:
                return
            sent_media = await bot.send_video(
                message.chat.id,
                file,
                duration=getattr(msg.video, "duration", 0) or 0,
                width=getattr(msg.video, "width", 0) or 0,
                height=getattr(msg.video, "height", 0) or 0,
                thumb=thumb,
                caption=caption,
                caption_entities=msg.caption_entities if caption == msg.caption else None,
                supports_streaming=True,
                reply_to_message_id=message.id,
                progress=progress,
                progress_args=[smsg, "up", task_id, file_display_name, msg_type],
            )

        elif "Animation" == msg_type:
            file_display_name = getattr(msg.animation, "file_name", None) or os.path.basename(file)
            if task_ctx.is_cancelled:
                return
            sent_media = await bot.send_animation(message.chat.id, file, reply_to_message_id=message.id)

        elif "Sticker" == msg_type:
            file_display_name = getattr(msg.sticker, "set_name", None) or "Sticker"
            if task_ctx.is_cancelled:
                return
            sent_media = await bot.send_sticker(message.chat.id, file, reply_to_message_id=message.id)

        elif "Voice" == msg_type:
            file_display_name = "Voice Message"
            caption = apply_caption_template(user_id, msg.caption)
            if task_ctx.is_cancelled:
                return
            sent_media = await bot.send_voice(
                message.chat.id,
                file,
                caption=caption,
                caption_entities=msg.caption_entities if caption == msg.caption else None,
                reply_to_message_id=message.id,
                progress=progress,
                progress_args=[smsg, "up", task_id, file_display_name, msg_type],
            )

        elif "Audio" == msg_type:
            audio_name = getattr(msg.audio, "file_name", None) or getattr(msg.audio, "title", None) or os.path.basename(file)
            file_display_name = audio_name
            caption = apply_caption_template(user_id, msg.caption, audio_name)
            thumb = custom_thumb
            if not thumb:
                try:
                    if msg.audio.thumbs:
                        thumb = await acc.download_media(msg.audio.thumbs[0].file_id)
                        task_ctx.track_file(thumb)
                except Exception:
                    thumb = None
            if not thumb and file:
                audio_thumb = extract_audio_thumbnail(file)
                if audio_thumb:
                    thumb = audio_thumb
                    task_ctx.track_file(thumb)

            if task_ctx.is_cancelled:
                return
            sent_media = await bot.send_audio(
                message.chat.id,
                file,
                caption=caption,
                caption_entities=msg.caption_entities if caption == msg.caption else None,
                reply_to_message_id=message.id,
                progress=progress,
                progress_args=[smsg, "up", task_id, file_display_name, msg_type],
            )

        elif "Photo" == msg_type:
            file_display_name = "Photo"
            if task_ctx.is_cancelled:
                return
            sent_media = await bot.send_photo(
                message.chat.id,
                file,
                caption=msg.caption,
                caption_entities=msg.caption_entities,
                reply_to_message_id=message.id,
            )

        upload_duration = max(time.time() - upload_start, 0.01)

        # Stop upstatus updater before sending completion report
        STATUS_TRACKER.pop(up_key, None)
        if up_task and not up_task.done():
            up_task.cancel()

        # Delete status message
        await delete_status_message(message, smsg)
        smsg_deleted = True

        # Send separate completion message showing thumbnail preview and time taken
        report_thumb = thumb
        if not report_thumb and msg_type == "Photo" and file and os.path.exists(file):
            report_thumb = file

        report_reply_id = sent_media.id if sent_media else message.id
        await send_completion_report(
            chat_id=message.chat.id,
            file_name=file_display_name,
            file_size=file_size,
            msg_type=msg_type,
            download_duration=download_duration,
            upload_duration=upload_duration,
            thumb_path=report_thumb,
            user_id=user_id,
            reply_to_message_id=report_reply_id,
        )

    except (asyncio.CancelledError, pyrogram.StopTransmission):
        logger.info(f"Task {task_id} was cancelled cleanly.")
        return
    finally:
        STATUS_TRACKER.pop(down_key, None)
        STATUS_TRACKER.pop(up_key, None)
        if down_task and not down_task.done():
            down_task.cancel()
        if up_task and not up_task.done():
            up_task.cancel()
        task_ctx.cleanup_files()
        if thumb and thumb != custom_thumb:
            remove_file(thumb)
        remove_file(file)
        unregister_task(task_id)
        if task_ctx.is_cancelled:
            asyncio.create_task(delayed_delete_status(message.chat.id, smsg.id, delay=2.0))
        elif not smsg_deleted:
            await delete_status_message(message, smsg)


async def handle_private_media_group(message: Message, messages: List[Message]):
    task_id = uuid.uuid4().hex[:10]
    user_id = message.from_user.id if message.from_user else 0
    task_ctx = TaskContext(
        task_id=task_id,
        initiator_id=user_id,
        chat_id=message.chat.id,
    )
    task_ctx.async_task = asyncio.current_task()
    register_task(task_ctx)

    cancel_markup = get_cancel_button(task_id)
    album_name = f"Media Album ({len(messages)} items)"
    initial_down_text = render_progress_text(
        action="Downloading",
        current=0,
        total=0,
        speed=0.0,
        eta=0.0,
        elapsed=0.0,
        file_name=album_name,
        media_type="Album",
    )
    smsg = await bot.send_message(
        message.chat.id,
        initial_down_text,
        reply_to_message_id=message.id,
        reply_markup=cancel_markup,
    )
    task_ctx.smsg_id = smsg.id
    task_ctx.smsg = smsg
    register_task(task_ctx)

    down_key = (smsg.chat.id, smsg.id, "down")
    down_task = asyncio.create_task(downstatus(down_key, smsg, task_id, album_name, "Album"))
    files = []
    thumbs = []
    media = []
    smsg_deleted = False

    try:
        download_start = time.time()
        for idx, msg in enumerate(messages, 1):
            if task_ctx.is_cancelled:
                return

            msg_type = get_message_type(msg)
            if msg_type not in ("Photo", "Video", "Document", "Audio"):
                raise ValueError(f"Message type {msg_type} can't be sent in a media group.")

            item_name = f"Item {idx}/{len(messages)} ({msg_type})"
            file = await acc.download_media(
                msg,
                progress=progress,
                progress_args=[smsg, "down", task_id, item_name, msg_type],
            )
            if task_ctx.is_cancelled or file is None:
                return
            files.append(file)
            task_ctx.track_file(file)

            raw_caption = msg.caption or ""
            caption = apply_caption_template(user_id, raw_caption) or ""
            caption_entities = msg.caption_entities if caption == raw_caption else None

            if "Photo" == msg_type:
                media.append(InputMediaPhoto(file, caption=caption, caption_entities=caption_entities))
            elif "Video" == msg_type:
                thumb = None
                try:
                    if msg.video.thumbs:
                        thumb = await acc.download_media(msg.video.thumbs[0].file_id)
                        task_ctx.track_file(thumb)
                except Exception:
                    thumb = None
                if not thumb and file:
                    generated_thumb = generate_video_thumbnail(file)
                    if generated_thumb:
                        thumb = generated_thumb
                        task_ctx.track_file(thumb)
                if thumb:
                    thumbs.append(thumb)
                media.append(InputMediaVideo(
                    file,
                    thumb=thumb,
                    caption=caption,
                    caption_entities=caption_entities,
                    duration=msg.video.duration or 0,
                    width=msg.video.width or 0,
                    height=msg.video.height or 0,
                ))
            elif "Document" == msg_type:
                thumb = None
                try:
                    if msg.document.thumbs:
                        thumb = await acc.download_media(msg.document.thumbs[0].file_id)
                        task_ctx.track_file(thumb)
                except Exception:
                    thumb = None
                if not thumb and file:
                    file_lower = file.lower()
                    if any(file_lower.endswith(ext) for ext in [".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v"]):
                        generated_thumb = generate_video_thumbnail(file)
                        if generated_thumb:
                            thumb = generated_thumb
                            task_ctx.track_file(thumb)
                if thumb:
                    thumbs.append(thumb)
                media.append(InputMediaDocument(file, thumb=thumb, caption=caption, caption_entities=caption_entities))
            elif "Audio" == msg_type:
                thumb = None
                try:
                    if msg.audio.thumbs:
                        thumb = await acc.download_media(msg.audio.thumbs[0].file_id)
                        task_ctx.track_file(thumb)
                except Exception:
                    thumb = None
                if not thumb and file:
                    audio_thumb = extract_audio_thumbnail(file)
                    if audio_thumb:
                        thumb = audio_thumb
                        task_ctx.track_file(thumb)
                if thumb:
                    thumbs.append(thumb)
                media.append(InputMediaAudio(
                    file,
                    thumb=thumb,
                    caption=caption,
                    caption_entities=caption_entities,
                    duration=msg.audio.duration or 0,
                    performer=msg.audio.performer or "",
                    title=msg.audio.title or "",
                ))

        download_duration = max(time.time() - download_start, 0.01)

        STATUS_TRACKER.pop(down_key, None)
        if down_task and not down_task.done():
            down_task.cancel()

        if task_ctx.is_cancelled:
            return

        total_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        initial_up_text = render_progress_text(
            action="Uploading",
            current=0,
            total=total_size,
            speed=0.0,
            eta=0.0,
            elapsed=0.0,
            file_name=album_name,
            media_type="Album",
        )
        await bot.edit_message_text(message.chat.id, smsg.id, initial_up_text, reply_markup=cancel_markup)

        upload_start = time.time()
        # Send media in chunks of MAX_MEDIA_GROUP_SIZE to respect Telegram's 10-item limit
        for i in range(0, len(media), MAX_MEDIA_GROUP_SIZE):
            if task_ctx.is_cancelled:
                return
            chunk = media[i:i + MAX_MEDIA_GROUP_SIZE]
            await bot.send_media_group(message.chat.id, chunk, reply_to_message_id=message.id)
        upload_duration = max(time.time() - upload_start, 0.01)

        # Delete status message
        await delete_status_message(message, smsg)
        smsg_deleted = True

        # Send separate completion message for media group
        report_thumb = thumbs[0] if (thumbs and os.path.exists(thumbs[0])) else None
        if not report_thumb:
            for f in files:
                if f.lower().endswith(('.jpg', '.jpeg', '.png')) and os.path.exists(f):
                    report_thumb = f
                    break

        await send_completion_report(
            chat_id=message.chat.id,
            file_name=f"Media Album ({len(files)} items)",
            file_size=total_size,
            msg_type="Media Group",
            download_duration=download_duration,
            upload_duration=upload_duration,
            thumb_path=report_thumb,
            user_id=user_id,
            reply_to_message_id=message.id,
        )

    except (asyncio.CancelledError, pyrogram.StopTransmission):
        logger.info(f"Media group task {task_id} was cancelled cleanly.")
        return
    finally:
        STATUS_TRACKER.pop(down_key, None)
        if down_task and not down_task.done():
            down_task.cancel()
        task_ctx.cleanup_files()
        for thumb in thumbs:
            remove_file(thumb)
        for file in files:
            remove_file(file)
        unregister_task(task_id)
        if task_ctx.is_cancelled:
            asyncio.create_task(delayed_delete_status(message.chat.id, smsg.id, delay=2.0))
        elif not smsg_deleted:
            await delete_status_message(message, smsg)


def get_message_type(msg: Message) -> Optional[str]:
    if msg.document:
        return "Document"
    if msg.video:
        return "Video"
    if msg.animation:
        return "Animation"
    if msg.sticker:
        return "Sticker"
    if msg.voice:
        return "Voice"
    if msg.audio:
        return "Audio"
    if msg.photo:
        return "Photo"
    if msg.text:
        return "Text"
    return None


USAGE = """**How to use**

Send a Telegram message link and the bot will send the content back to you.

**Public channels / groups**

Send a normal post link:

```
https://t.me/channelname/123
```

**Private channels / groups / restricted content**

If the user session has not joined the target chat yet, send the invite link first:

```
https://t.me/+invite_code
```

Then send the post link:

```
https://t.me/c/123456789/123
```

These links require a valid `STRING`.

**Bot chat messages**

Use the `/b/` format:

```
https://t.me/b/botusername/4321
```

These links also require a valid `STRING`.

**Forum / topic messages**

Use the topic format:

```
https://t.me/channelname/45/123

https://t.me/c/123456789/45/123
```

**Multiple messages**

Use `start_id-end_id` in the message ID position:

```
https://t.me/channelname/1001-1010

https://t.me/c/123456789/101-120
```

The bot processes up to 100 messages per request. Albums / media groups are sent as a group when possible.

**Custom Thumbnails, Captions & Completion Messages**
- `/setthumb`: Reply to any image to set as your default thumbnail
- `/delthumb`: Delete your custom thumbnail
- `/showthumb`: View your current active thumbnail
- `/setcaption <template>`: Set custom caption with `{caption}` and `{filename}`
- `/delcaption`: Remove custom caption template
- `/showcaption`: View active caption template
- `/setmsg <text>`: Add a custom note/branding to download completion cards
- `/delmsg`: Remove custom note from completion cards
- `/showmsg`: View active completion note

**Diagnostics & Info**
- `/ping`: Check bot response latency
- `/stats` or `/status`: View bot uptime, CPU, RAM, disk space, and active tasks
"""


async def main():
    web_server = None
    port_env = os.getenv("PORT")
    if port_env:
        try:
            web_server = await start_web_server(int(port_env))
        except (ValueError, OSError) as e:
            logger.warning(f"Could not start web server on port {port_env}: {e}")

    if acc is not None:
        await acc.start()
    await bot.start()
    print("Bot is running...")
    await idle()
    await bot.stop()
    if acc is not None:
        await acc.stop()
    if web_server:
        web_server.close()
        await web_server.wait_closed()


if __name__ == "__main__":
    bot.run(main())
