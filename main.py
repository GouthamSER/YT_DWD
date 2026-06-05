"""
╔══════════════════════════════════════════════════╗
║   Advanced YouTube Downloader Bot — Pyrofork     ║
║   Playlist • WZML-X Quality • Progress           ║
║   + Admin Cookies Manager (MongoDB)              ║
╚══════════════════════════════════════════════════╝
"""

import os, re, time, math, uuid, logging, asyncio, requests, yt_dlp, shutil
from contextlib import suppress
from datetime import datetime

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web

# Import config + cookies handler
import config
from cookies_handler import (
    setup_cookies_handlers,
    auto_import_local_cookies,
    get_cookies_path,
    ADMINS,
    is_admin,
)

# ╔══════════════════════════════════════╗
# ║             CONFIG                   ║
# ╚══════════════════════════════════════╝
API_ID    = int(os.environ.get("API_ID", ""))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

PROXY_URL     = None
AUTH_USERS    = []
MAX_PLAYLIST  = 50
SESSION_TTL   = 600
PORT          = int(os.environ.get("PORT", 8000))

SELF_PING_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PING_INTERVAL = 10 * 60

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# NOTE: COOKIES_FILE is now dynamic — resolved per-download via get_cookies_path()
# The module-level constant below is kept only for the /ping status display.
def _local_cookies_exist():
    p = os.path.join(BASE_DIR, "cookies.txt")
    return os.path.exists(p) and os.path.getsize(p) > 0

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("YTBot")
logger.info(f"Proxy     : {'SET' if PROXY_URL else 'NONE'}")
logger.info(f"Self-ping : {SELF_PING_URL or 'DISABLED (set RENDER_EXTERNAL_URL)'}")
logger.info(f"Admins    : {ADMINS or '(none set)'}")

app = Client("yt_bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

_BOT_START = time.time()

# ═══════════════════════════════════════════
#             SESSION STORE
# ═══════════════════════════════════════════
URL_SESSIONS = {}
PL_SESSIONS  = {}
WAITING_SEL  = {}

def _new_uid():
    return uuid.uuid4().hex[:10]

def _cleanup():
    now = time.time()
    for d in [URL_SESSIONS, PL_SESSIONS]:
        for k in list(d.keys()):
            if now - d[k].get("created", 0) > SESSION_TTL:
                d.pop(k, None)

# ═══════════════════════════════════════════
#             HELPERS
# ═══════════════════════════════════════════
def humanbytes(size):
    if not size: return "0 B"
    for u in ["B","KB","MB","GB","TB"]:
        if size < 1024.0: return f"{size:.2f} {u}"
        size /= 1024.0

def time_fmt(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600); m, s = divmod(r, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def pbar(pct, w=10):
    f = math.floor(pct / (100/w))
    return f"[{'█'*f}{'░'*(w-f)}] {pct:.1f}%"

def is_auth(uid):    return not AUTH_USERS or uid in AUTH_USERS
def safe_name(s, n=180):
    if not s: return "file"
    s = re.sub(r'[/\\:*?"<>|]', '', s)
    s = re.sub(r' +', ' ', s)
    return s[:n].strip() or "file"

def clean_url(url):
    url = url.strip().rstrip("/")
    url = re.sub(r"[?&]si=[^&]+", "", url)
    url = re.sub(r"[?&]utm_[^&\s]+", "", url)
    return url.rstrip("?&")

def extract_url(text):
    m = re.search(r'((?:rtmps?|mms|rtsp|https?|ftp)://[^\s]+)', text)
    return m.group(0) if m else None

def is_playlist_url(url):
    if re.search(r"[?&]list=PL[a-zA-Z0-9_-]+", url): return True
    if re.search(r"(playlist\?list=|/playlist/)", url, re.I): return True
    return False

async def _safe_edit(msg, text):
    with suppress(Exception):
        await msg.edit_text(text)

# ═══════════════════════════════════════════
#    RENDER KEEP-ALIVE
# ═══════════════════════════════════════════
async def keep_alive():
    if not SELF_PING_URL:
        logger.info("keep_alive: RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return
    await asyncio.sleep(30)
    while True:
        try:
            r = requests.get(SELF_PING_URL, timeout=15)
            logger.info(f"keep_alive: pinged {SELF_PING_URL} → {r.status_code}")
        except Exception as e:
            logger.warning(f"keep_alive: ping failed — {e}")
        await asyncio.sleep(PING_INTERVAL)

# ═══════════════════════════════════════════
#    PROGRESS TRACKER  (6-second refresh)
# ═══════════════════════════════════════════
PROGRESS_INTERVAL = 6

class YtDlpProgress:
    def __init__(self, msg, loop, title="", prefix="", is_pl=False):
        self.msg    = msg
        self.loop   = loop
        self.title  = title
        self.prefix = prefix
        self.is_pl  = is_pl
        self._dl    = 0
        self._last  = 0
        self._speed = 0
        self._eta   = 0
        self._size  = 0
        self._t     = 0

    def hook(self, d):
        now = time.time()
        if now - self._t < PROGRESS_INTERVAL: return
        self._t = now
        if d["status"] == "finished":
            if self.is_pl: self._last = 0
            return
        if d["status"] != "downloading": return
        self._speed = d.get("speed") or 0
        self._eta   = d.get("eta")   or 0
        if self.is_pl:
            chunk = (d.get("downloaded_bytes") or 0) - self._last
            self._last = d.get("downloaded_bytes") or 0
            self._dl  += chunk
        else:
            self._size = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            self._dl   = d.get("downloaded_bytes") or 0
        try:
            pct = (self._dl / self._size * 100) if self._size else 0
        except ZeroDivisionError:
            pct = 0
        bar  = pbar(pct)
        title_line = f"🎬 `{self.title[:55]}`\n" if self.title else ""
        text = (
            f"{self.prefix}{title_line}"
            f"⬇️ **Downloading...**\n\n{bar}\n\n"
            f"📦 `{humanbytes(self._dl)}`" +
            (f" / `{humanbytes(self._size)}`" if self._size else "") +
            f"\n⚡ Speed: `{humanbytes(self._speed)}/s`" +
            (f"\n⏳ ETA: `{time_fmt(self._eta)}`" if self._eta else "")
        )
        asyncio.run_coroutine_threadsafe(_safe_edit(self.msg, text), self.loop)

async def progress_for_upload(current, total, msg, start_time, label="Uploading", title=""):
    now = time.time(); diff = now - start_time
    if current != total and (diff < 1 or round(diff) % PROGRESS_INTERVAL != 0):
        return
    pct   = current * 100 / total if total else 0
    speed = current / diff if diff > 0 else 0
    eta   = (total - current) / speed if speed > 0 else 0
    title_line = f"🎬 `{title[:55]}`\n" if title else ""
    text  = (
        f"{title_line}"
        f"📤 **{label}**\n\n{pbar(pct)}\n\n"
        f"📦 `{humanbytes(current)}` / `{humanbytes(total)}`\n"
        f"⚡ Speed: `{humanbytes(speed)}/s`\n"
        f"⏳ ETA: `{time_fmt(eta)}`"
    )
    await _safe_edit(msg, text)

# ═══════════════════════════════════════════
#    yt-dlp OPTIONS
#    ── cookies_path resolved async via get_cookies_path()
# ═══════════════════════════════════════════
def _base_opts(cookies_path: str | None = None):
    """
    Build base yt-dlp options.
    `cookies_path` should come from `await get_cookies_path()` so it reflects
    the latest cookies stored in MongoDB.
    """
    o = {
        "usenetrc": True,
        "allow_multiple_video_streams": True,
        "allow_multiple_audio_streams": True,
        "noprogress": True,
        "overwrites": True,
        "writethumbnail": True,
        "trim_file_name": 200,
        "fragment_retries": 10,
        "retries": 10,
        "nocheckcertificate": True,
        "retry_sleep_functions": {
            "http": lambda n: 3, "fragment": lambda n: 3,
            "file_access": lambda n: 3, "extractor": lambda n: 3,
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"youtube": {"player_client": ["web","tv"], "skip": ["dash"]}},
        "remote_components": ["ejs:github"],
    }
    # ── cookies: use the path passed in (freshly resolved from DB) ──────────
    if cookies_path:
        o["cookiefile"] = cookies_path
    if PROXY_URL:
        o["proxy"] = PROXY_URL
    return o

def _info_opts(cookies_path: str | None = None):
    o = _base_opts(cookies_path)
    o.update({
        "quiet": True,
        "no_warnings": True,
        "playlist_items": "0",
        "format": "bv*+ba/b"
    })
    return o

def _dl_opts(fmt, out_tmpl, tracker=None, is_pl=False, cookies_path: str | None = None):
    o = _base_opts(cookies_path)
    o["outtmpl"] = {"default": out_tmpl, "thumbnail": out_tmpl.replace(".%(ext)s","_t.%(ext)s")}
    o["postprocessors"] = [{"add_chapters":True,"add_infojson":"if_exists","add_metadata":True,"key":"FFmpegMetadata"}]
    if tracker: o["progress_hooks"] = [tracker.hook]
    if is_pl:   o["ignoreerrors"]   = True

    is_audio = fmt.startswith("ba/b") or fmt in ("mp3",)
    if is_audio:
        parts      = fmt.split("-") if "-" in fmt else ["ba/b","mp3","192"]
        afmt       = parts[1] if len(parts) > 1 else "mp3"
        arate      = parts[2] if len(parts) > 2 else "192"
        o["format"] = "ba/b"
        o["postprocessors"].append({"key":"FFmpegExtractAudio","preferredcodec":afmt,"preferredquality":arate})
    else:
        o["format"] = fmt

    o["postprocessors"].append({"format":"jpg","key":"FFmpegThumbnailsConvertor","when":"before_dl"})
    ext = ".mp3" if is_audio else ".mp4"
    if ext in [".mp3",".mkv",".mka",".ogg",".opus",".flac",".m4a",".mp4",".mov",".m4v"]:
        o["postprocessors"].append({"already_have_thumbnail": True, "key": "EmbedThumbnail"})
    return o

# ═══════════════════════════════════════════
#    FORMAT PARSER
# ═══════════════════════════════════════════
def parse_formats(result):
    if "entries" in result:
        fmts = {}
        for h in ["144","240","360","480","720","1080","1440","2160"]:
            fmts[f"{h}|mp4"]  = f"bv*[height<=?{h}][ext=mp4]+ba[ext=m4a]/b[height<=?{h}]"
            fmts[f"{h}|webm"] = f"bv*[height<=?{h}][ext=webm]+ba/b[height<=?{h}]"
        return fmts, True

    fmts   = {}
    is_m4a = False
    for item in result.get("formats",[]):
        if not item.get("tbr"): continue
        fid  = item["format_id"]
        size = item.get("filesize") or item.get("filesize_approx") or 0
        if item.get("video_ext") == "none" and (item.get("resolution") == "audio only" or item.get("acodec") != "none"):
            if item.get("audio_ext") == "m4a": is_m4a = True
            b_name = f"{item.get('acodec') or fid}-{item['ext']}"
            v_fmt  = fid
        elif item.get("height"):
            h = item["height"]; ext = item["ext"]
            fps = item["fps"] if item.get("fps") else ""
            b_name = f"{h}p{fps}-{ext}"
            ba_ext = "[ext=m4a]" if is_m4a and ext == "mp4" else ""
            v_fmt  = f"{fid}+ba{ba_ext}/b[height=?{h}]"
        else:
            continue
        fmts.setdefault(b_name, {})[f"{item['tbr']}"] = [size, v_fmt]
    return fmts, False

# ═══════════════════════════════════════════
#    KEYBOARDS
# ═══════════════════════════════════════════
def _kb_main(fmts, uid, is_pl, tl):
    btns = []; row = []
    if is_pl:
        for key in ["144|mp4","240|mp4","360|mp4","480|mp4","720|mp4","1080|mp4","1440|mp4","2160|mp4"]:
            row.append(InlineKeyboardButton(f"{key.split('|')[0]}p-mp4", callback_data=f"q|{uid}|fmt|{key}"))
            if len(row) == 3: btns.append(row); row = []
        if row: btns.append(row); row = []
        for key in ["144|webm","240|webm","360|webm","480|webm","720|webm","1080|webm","1440|webm","2160|webm"]:
            row.append(InlineKeyboardButton(f"{key.split('|')[0]}p-webm", callback_data=f"q|{uid}|fmt|{key}"))
            if len(row) == 3: btns.append(row); row = []
        if row: btns.append(row)
        msg = f"📋 **Playlist Quality:**\n⏳ `{time_fmt(tl)}`"
    else:
        for b_name, tbr_dict in fmts.items():
            if len(tbr_dict) == 1:
                tbr, vl = next(iter(tbr_dict.items()))
                row.append(InlineKeyboardButton(f"{b_name} ({humanbytes(vl[0])})", callback_data=f"q|{uid}|sub|{b_name}|{tbr}"))
            else:
                row.append(InlineKeyboardButton(b_name, callback_data=f"q|{uid}|dict|{b_name}"))
            if len(row) == 2: btns.append(row); row = []
        if row: btns.append(row)
        msg = f"🎬 **Select Quality:**\n⏳ `{time_fmt(tl)}`"
    btns.append([
        InlineKeyboardButton("🎵 MP3", callback_data=f"q|{uid}|mp3|"),
        InlineKeyboardButton("🎧 Audio Formats", callback_data=f"q|{uid}|audiofmt|"),
    ])
    btns.append([
        InlineKeyboardButton("⭐ Best Video", callback_data=f"q|{uid}|fmt|bv*+ba/b"),
        InlineKeyboardButton("🔊 Best Audio", callback_data=f"q|{uid}|fmt|ba/b"),
    ])
    btns.append([InlineKeyboardButton("❌ Cancel", callback_data=f"q|{uid}|cancel|")])
    return msg, InlineKeyboardMarkup(btns)

def _kb_sub(b_name, tbr_dict, uid, tl):
    btns = []; row = []
    for tbr, vl in tbr_dict.items():
        row.append(InlineKeyboardButton(f"{tbr}K ({humanbytes(vl[0])})", callback_data=f"q|{uid}|sub|{b_name}|{tbr}"))
        if len(row) == 2: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("◀️ Back", callback_data=f"q|{uid}|back|"), InlineKeyboardButton("❌ Cancel", callback_data=f"q|{uid}|cancel|")])
    return f"🎚️ **{b_name}** bitrate:\n⏳ `{time_fmt(tl)}`", InlineKeyboardMarkup(btns)

def _kb_mp3(uid, tl):
    return (f"🎵 MP3 Bitrate:\n⏳ `{time_fmt(tl)}`", InlineKeyboardMarkup([[
        InlineKeyboardButton("64K",  callback_data=f"q|{uid}|fmt|ba/b-mp3-64"),
        InlineKeyboardButton("128K", callback_data=f"q|{uid}|fmt|ba/b-mp3-128"),
        InlineKeyboardButton("192K", callback_data=f"q|{uid}|fmt|ba/b-mp3-192"),
        InlineKeyboardButton("320K", callback_data=f"q|{uid}|fmt|ba/b-mp3-320"),
    ],[InlineKeyboardButton("◀️ Back", callback_data=f"q|{uid}|back|"), InlineKeyboardButton("❌ Cancel", callback_data=f"q|{uid}|cancel|")]]))

def _kb_audiofmt(uid, tl):
    btns = []; row = []
    for f in ["aac","alac","flac","m4a","mp3","opus","vorbis","wav"]:
        row.append(InlineKeyboardButton(f, callback_data=f"q|{uid}|audioq|ba/b-{f}-"))
        if len(row) == 4: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("◀️ Back", callback_data=f"q|{uid}|back|"), InlineKeyboardButton("❌ Cancel", callback_data=f"q|{uid}|cancel|")])
    return f"🎧 Audio Format:\n⏳ `{time_fmt(tl)}`", InlineKeyboardMarkup(btns)

def _kb_audioq(prefix, uid, tl):
    btns = []; row = []
    for q in range(11):
        row.append(InlineKeyboardButton(str(q), callback_data=f"q|{uid}|fmt|{prefix}{q}"))
        if len(row) == 4: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("◀️ Back", callback_data=f"q|{uid}|audiofmt|"), InlineKeyboardButton("❌ Cancel", callback_data=f"q|{uid}|cancel|")])
    return f"🎚️ Quality (0=best, 10=worst):\n⏳ `{time_fmt(tl)}`", InlineKeyboardMarkup(btns)

def _kb_playlist(uid, total):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Download All ({total})", callback_data=f"pl|{uid}|all")],
        [InlineKeyboardButton("🎯 Select Videos", callback_data=f"pl|{uid}|select")],
        [InlineKeyboardButton("🖼️ Thumbnails", callback_data=f"pl|{uid}|thumbs"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"pl|{uid}|cancel")],
    ])

# ═══════════════════════════════════════════
#    INFO EXTRACTION  (cookies resolved from DB each call)
# ═══════════════════════════════════════════
def _blocking_info(url, cookies_path=None):
    try:
        with yt_dlp.YoutubeDL(_info_opts(cookies_path)) as ydl:
            r = ydl.extract_info(url, download=False)
            if r is None: raise ValueError("Info result is None")
            return r
    except Exception as e:
        logger.error(f"Info: {e}")
        return None

def _blocking_playlist_info(url, cookies_path=None):
    opts = _info_opts(cookies_path)
    opts.pop("playlist_items", None)
    opts.pop("format", None)
    opts.update({
        "extract_flat":  True,
        "playlistend":   MAX_PLAYLIST,
        "ignoreerrors":  True,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        logger.error(f"PL info: {e}")
        return None

# ═══════════════════════════════════════════
#    DOWNLOAD
# ═══════════════════════════════════════════
def _find_thumb(dl_dir):
    for f in os.listdir(dl_dir):
        if f.endswith((".jpg",".jpeg")):
            return os.path.join(dl_dir, f)
    return None

def _blocking_download(url, fmt, out_tmpl, smsg, loop, title="", is_pl=False, cookies_path=None):
    try:
        tracker = YtDlpProgress(smsg, loop, title=title, is_pl=is_pl)
        opts    = _dl_opts(fmt, out_tmpl, tracker, is_pl, cookies_path=cookies_path)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info: return None
            actual = ydl.prepare_filename(info)
            if fmt.startswith("ba/b") or fmt == "mp3":
                parts = fmt.split("-") if "-" in fmt else ["ba/b","mp3","192"]
                afmt  = parts[1] if len(parts) > 1 else "mp3"
                ext   = "ogg" if afmt == "vorbis" else "m4a" if afmt == "alac" else afmt
                actual = os.path.splitext(actual)[0] + f".{ext}"
            if not os.path.exists(actual):
                dl_dir = os.path.dirname(actual)
                base   = os.path.splitext(os.path.basename(actual))[0][:30]
                for f in os.listdir(dl_dir or "."):
                    if base in f and not f.endswith((".jpg",".jpeg",".png",".webp",".part")):
                        actual = os.path.join(dl_dir or ".", f); break
            if not os.path.exists(actual) or os.path.getsize(actual) == 0:
                found = None; best_sz = 0
                dl_dir = os.path.dirname(actual)
                for f in os.listdir(dl_dir or "."):
                    if f.endswith((".jpg",".jpeg",".png",".webp",".part",".ytdl")): continue
                    fp2 = os.path.join(dl_dir or ".", f)
                    fsz = os.path.getsize(fp2)
                    if fsz > best_sz:
                        best_sz = fsz; found = fp2
                if found and best_sz > 0:
                    actual = found
                else:
                    return None
            if not os.path.exists(actual) or os.path.getsize(actual) == 0:
                return None

            real_info = info
            if "entries" in info and info["entries"]:
                real_info = info["entries"][0] or info
            return {
                "filepath": actual,
                "info":     real_info,
                "duration": real_info.get("duration", 0),
                "title":    real_info.get("title","Video"),
            }
    except Exception as e:
        logger.error(f"DL: {e}")
        return None

# ═══════════════════════════════════════════
#    UPLOAD
# ═══════════════════════════════════════════
async def upload_file(client, chat_id, result, fmt, smsg):
    fp    = result["filepath"]
    info  = result["info"]
    dur   = result["duration"]
    title = result.get("title","")
    upl   = info.get("uploader","") or info.get("channel","")
    sz    = os.path.getsize(fp)

    filename = os.path.basename(fp)
    cap = f"📁 `{filename}`"

    thumb = _find_thumb(os.path.dirname(fp))
    if sz > 2*1024**3:
        await _safe_edit(smsg, "❌ File size exceeds 2GB!"); return False
    start = time.time()
    try:
        is_audio = fmt.startswith("ba/b") or fmt == "mp3"
        if is_audio:
            await client.send_audio(
                chat_id, fp, caption=cap, duration=dur,
                title=filename[:64], performer=upl, thumb=thumb,
                progress=progress_for_upload,
                progress_args=(smsg, start, "Uploading Audio...", title),
            )
        else:
            await client.send_video(
                chat_id, fp, caption=cap, duration=dur,
                width=info.get("width",1280), height=info.get("height",720),
                thumb=thumb, supports_streaming=True,
                progress=progress_for_upload,
                progress_args=(smsg, start, "Uploading Video...", title),
            )
        return True
    except Exception as e:
        logger.error(f"Upload: {e}")
        await _safe_edit(smsg, f"❌ Upload failed: `{str(e)[:200]}`")
        return False

# ═══════════════════════════════════════════
#    QUALITY PICKER SHOW
# ═══════════════════════════════════════════
async def show_quality_picker(url, smsg, user_id=None):
    loop         = asyncio.get_event_loop()
    cookies_path = await get_cookies_path()          # ← fresh from DB each time
    info = await loop.run_in_executor(None, _blocking_info, url, cookies_path)
    if not info:
        await _safe_edit(smsg,
            "❌ **Could not fetch video info!**\n\n"
            "• Is the video private or age-restricted?\n"
            "• Try updating cookies: `/setcookies`\n"
            "• Is the URL correct?")
        return

    fmts, is_pl = parse_formats(info)
    uid = _new_uid()
    URL_SESSIONS[uid] = {
        "url": url, "info": info, "fmts": fmts,
        "is_pl": is_pl, "created": time.time(), "timeout": 120,
        "user_id": user_id,
    }

    title  = (info.get("title","") or "")[:60]
    upl    = info.get("uploader","") or info.get("channel","")
    dur    = time_fmt(info.get("duration",0))
    views  = info.get("view_count",0) or 0
    likes  = info.get("like_count",0) or 0
    udate  = info.get("upload_date","")
    if udate:
        try: udate = datetime.strptime(udate,"%Y%m%d").strftime("%d %b %Y")
        except: pass

    info_txt = (
        f"🎬 **{title}**\n\n"
        f"👤 `{upl}`\n"
        f"⏱️ `{dur}`  📅 `{udate}`\n"
        f"👁️ `{views:,}`  ❤️ `{likes:,}`\n\n"
    )
    q_msg, kb = _kb_main(fmts, uid, is_pl, 120)

    thumb = info.get("thumbnail")
    with suppress(Exception): await smsg.delete()
    try:
        if thumb:
            await smsg.reply_photo(photo=thumb, caption=info_txt+q_msg, reply_markup=kb)
        else: raise Exception()
    except Exception:
        with suppress(Exception):
            await smsg.reply_text(info_txt+q_msg, reply_markup=kb)

# ═══════════════════════════════════════════
#    COMMAND HANDLERS
# ═══════════════════════════════════════════
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    admin_hint = "\n\n**Admin Commands:**\n`/setcookies` `/getcookies` `/cookiesstatus`" if is_admin(message.from_user.id) else ""
    await message.reply_text(
        "🎬 **Advanced YouTube Downloader**\n\n"
        "_WZML-X Style Quality Picker_\n\n"
        "**Supported Sites:**\n"
        "▶️ YouTube & Playlists & Shorts\n"
        "📸 Instagram  🎵 TikTok  𝕏 Twitter\n"
        "📘 Facebook  🎬 Vimeo  ☁️ SoundCloud\n\n"
        "**Audio:** MP3 (64/128/192/320K)\n"
        "AAC • FLAC • M4A • OPUS • WAV\n\n"
        "/ping — Status  /help — Help"
        + admin_hint
    )

@app.on_message(filters.command("ping"))
async def ping_cmd(client, message):
    t = time.time()
    m = await message.reply_text("🏓 Pong!")
    ms = (time.time()-t)*1000
    cookies_meta = await config.get_cookies_meta()
    cookies_info = "❌ None"
    if cookies_meta:
        ts = cookies_meta.get("updated_at")
        ts_str = ts.strftime("%d %b %Y %H:%M UTC") if ts else "?"
        cookies_info = f"✅ {cookies_meta['size']:,} chars (updated {ts_str})"
    await m.edit_text(
        f"🏓 **Pong!** `{ms:.0f}ms`\n\n"
        f"⏱️ Uptime: `{time_fmt(time.time()-_BOT_START)}`\n"
        f"🍪 Cookies: `{cookies_info}`\n"
        f"🔌 Proxy: `{'✅' if PROXY_URL else '❌'}`\n"
        f"🔄 Self-ping: `{'✅ ' + SELF_PING_URL if SELF_PING_URL else '❌ Set RENDER_EXTERNAL_URL'}`\n"
        f"💾 MongoDB: `{'✅' if config.get_db() else '❌'}`\n"
        f"📂 Sessions: `{len(URL_SESSIONS)+len(PL_SESSIONS)}`"
    )

@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    admin_section = ""
    if is_admin(message.from_user.id):
        admin_section = (
            "\n\n**Admin — Cookies Management:**\n"
            "`/setcookies` — attach or reply-to a `cookies.txt` to update\n"
            "`/getcookies` — download current cookies from DB\n"
            "`/delcookies` — delete cookies from DB\n"
            "`/cookiesstatus` — show cookies metadata & DB status"
        )
    await message.reply_text(
        "**How to use:**\n"
        "1. Send a video or playlist URL\n"
        "2. Choose your quality\n"
        "3. Download! 🎉\n\n"
        "**Playlist:**\n"
        "• Download all videos\n"
        "• Select specific: `1,3,5` or `1-10`\n"
        "• Download thumbnails\n\n"
        "**Quality Picker:**\n"
        "• Per-format bitrate selection\n"
        "• MP3 64/128/192/320K\n"
        "• AAC/FLAC/M4A/OPUS/WAV/VORBIS\n"
        "• Best Video / Best Audio\n"
        "• 2 minute timeout"
        + admin_section
    )

# ═══════════════════════════════════════════
#    MESSAGE HANDLER
# ═══════════════════════════════════════════
@app.on_message(filters.text & ~filters.command(["start","ping","help","setcookies","getcookies","delcookies","cookiesstatus"]))
async def handle_url(client, message):
    uid  = message.from_user.id
    text = (message.text or "").strip()
    if not is_auth(uid): await message.reply_text("⛔ You are not authorized."); return

    if uid in WAITING_SEL:
        pl_uid = WAITING_SEL[uid]
        e      = PL_SESSIONS.get(pl_uid)
        if e:
            total   = len(e["entries"])
            indices = _parse_sel(text, total)
            if indices is None:
                await message.reply_text(f"❌ Invalid selection!\nFormat: `1,3,5` or `1-10`\nTotal videos: `{total}`")
                return
            e["sel"] = indices
            WAITING_SEL.pop(uid, None)
            smsg = await message.reply_text(f"✅ Selected `{len(indices)}` videos. Fetching quality options...")
            loop         = asyncio.get_event_loop()
            cookies_path = await get_cookies_path()
            fu   = e["entries"][indices[0]].get("url") or e["entries"][indices[0]].get("webpage_url")
            try:
                info = await loop.run_in_executor(None, _blocking_info, fu, cookies_path)
                if not info: raise ValueError()
                fmts, _ = parse_formats(info)
                s_uid   = _new_uid()
                URL_SESSIONS[s_uid] = {
                    "url": e["url"], "info": info, "fmts": fmts,
                    "is_pl": True, "created": time.time(), "timeout": 120,
                    "user_id": uid, "pl_uid": pl_uid, "pl_indices": indices,
                }
                msg_t, kb = _kb_main(fmts, s_uid, False, 120)
                await smsg.edit_text(f"🎨 **Select Quality** for `{len(indices)}` videos:\n\n{msg_t}", reply_markup=kb)
            except Exception as ex:
                await _safe_edit(smsg, f"❌ {ex}")
            return

    url = extract_url(text) or (text if text.startswith(("http://","https://","rtmp://")) else None)
    if not url: return

    url    = clean_url(url)
    status = await message.reply_text("🔍 **Fetching info...**")
    loop   = asyncio.get_event_loop()
    _cleanup()
    cookies_path = await get_cookies_path()          # ← fresh from DB

    if is_playlist_url(url):
        await _safe_edit(status, "📋 **Fetching playlist info...**")
        info = await loop.run_in_executor(None, _blocking_playlist_info, url, cookies_path)
        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e][:MAX_PLAYLIST]
            if entries:
                p_uid = _new_uid()
                PL_SESSIONS[p_uid] = {"url":url,"entries":entries,"info":info,"created":time.time(),"user_id":uid}
                total_dur = sum(e.get("duration",0) or 0 for e in entries)
                pl_title  = (info.get("title","Playlist"))[:60]
                channel   = info.get("uploader") or info.get("channel","")
                vl = ""
                for i, e in enumerate(entries[:12], 1):
                    vl += f"`{i:02d}.` {(e.get('title') or f'Video {i}')[:35]} `[{time_fmt(e.get('duration',0))}]`\n"
                if len(entries) > 12: vl += f"_...and {len(entries)-12} more_"
                cap = (f"📋 **{pl_title}**\n👤 `{channel}`\n🎬 `{len(entries)}` videos  ⏱️ `{time_fmt(total_dur)}`\n\n{vl}\n\n**What would you like to download?**")
                with suppress(Exception): await status.delete()
                try:
                    th = (info.get("thumbnails") or [{}])[-1].get("url","")
                    if th: await message.reply_photo(photo=th, caption=cap, reply_markup=_kb_playlist(p_uid, len(entries)))
                    else:  raise Exception()
                except Exception:
                    await message.reply_text(cap, reply_markup=_kb_playlist(p_uid, len(entries)))
                return

    await show_quality_picker(url, status, uid)

# ═══════════════════════════════════════════
#    CALLBACK HANDLERS
# ═══════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^q\|"))
async def quality_cb(client, query: CallbackQuery):
    parts  = query.data.split("|")
    uid    = parts[1]; action = parts[2]; rest = parts[3:] if len(parts) > 3 else []
    e      = URL_SESSIONS.get(uid)
    if not e:
        await query.answer("⚠️ Session expired! Please send the link again.", show_alert=True); return
    if e.get("user_id") and query.from_user.id != e["user_id"]:
        await query.answer("❌ This is not your session!", show_alert=True); return
    await query.answer()
    tl = max(0, e["timeout"] - (time.time() - e["created"]))
    fmts = e["fmts"]; is_pl = e["is_pl"]

    if action == "cancel":
        URL_SESSIONS.pop(uid, None)
        with suppress(Exception): await query.message.delete()
        return
    if action == "back":
        msg, kb = _kb_main(fmts, uid, is_pl, tl)
        await query.message.edit_text(msg, reply_markup=kb); return
    if action == "mp3":
        msg, kb = _kb_mp3(uid, tl)
        await query.message.edit_text(msg, reply_markup=kb); return
    if action == "audiofmt":
        msg, kb = _kb_audiofmt(uid, tl)
        await query.message.edit_text(msg, reply_markup=kb); return
    if action == "audioq":
        prefix = rest[0] if rest else "ba/b-mp3-"
        msg, kb = _kb_audioq(prefix, uid, tl)
        await query.message.edit_text(msg, reply_markup=kb); return
    if action == "dict":
        b_name = rest[0]; tbr_dict = fmts.get(b_name, {})
        msg, kb = _kb_sub(b_name, tbr_dict, uid, tl)
        await query.message.edit_text(msg, reply_markup=kb); return
    if action == "sub":
        b_name = rest[0]; tbr = rest[1]
        vl = fmts.get(b_name, {}).get(tbr)
        if not vl: await query.answer("Format not found!"); return
        await _start_dl(uid, vl[1], e, query, client); return
    if action == "fmt":
        qual = rest[0] if rest else "bv*+ba/b"
        if "|" in qual and qual in fmts: qual = fmts[qual]
        await _start_dl(uid, qual, e, query, client); return

@app.on_callback_query(filters.regex(r"^pl\|"))
async def playlist_cb(client, query: CallbackQuery):
    parts = query.data.split("|"); uid = parts[1]; sub = parts[2]
    e = PL_SESSIONS.get(uid)
    if sub == "cancel":
        PL_SESSIONS.pop(uid, None); WAITING_SEL.pop(query.from_user.id, None)
        with suppress(Exception): await query.message.delete()
        await query.answer(); return
    if not e: await query.answer("⚠️ Session expired!", show_alert=True); return
    await query.answer()

    if sub == "thumbs":
        sm = await query.message.reply_text("🖼️ Fetching thumbnails..."); sent = 0
        for i, en in enumerate(e["entries"], 1):
            tu = en.get("thumbnail") or (en.get("thumbnails") or [{}])[-1].get("url","")
            if not tu: continue
            try:
                r = requests.get(tu, timeout=10)
                if r.status_code != 200: continue
                p = os.path.join(DOWNLOADS_DIR, f"th_{uid}_{i}.jpg")
                with open(p,"wb") as f: f.write(r.content)
                await client.send_document(query.message.chat.id, p, caption=f"🖼️ `{i}.` {(en.get('title') or '')[:40]}")
                with suppress(Exception): os.remove(p)
                sent += 1; await asyncio.sleep(0.5)
            except Exception: pass
            if i % 5 == 0: await _safe_edit(sm, f"🖼️ `{i}/{len(e['entries'])}`...")
        await _safe_edit(sm, f"✅ `{sent}` thumbnails sent!"); return

    if sub == "select":
        WAITING_SEL[query.from_user.id] = uid; total = len(e["entries"]); vl = ""
        for i, en in enumerate(e["entries"][:20], 1):
            vl += f"`{i:02d}.` {(en.get('title') or f'Video {i}')[:35]} `[{time_fmt(en.get('duration',0))}]`\n"
        if total > 20: vl += f"_...{total-20} more_"
        await query.message.reply_text(f"🎯 **Select videos:**\nTotal: `{total}`\n\n{vl}\nFormat: `1,3,5` or `1-10`"); return

    if sub == "all":
        sm = await query.message.reply_text("🔍 Fetching quality options...")
        loop         = asyncio.get_event_loop()
        cookies_path = await get_cookies_path()
        fu = e["entries"][0].get("url") or e["entries"][0].get("webpage_url")
        try:
            info = await loop.run_in_executor(None, _blocking_info, fu, cookies_path)
            if not info: raise ValueError()
            fmts2, _ = parse_formats(info)
            s_uid = _new_uid()
            URL_SESSIONS[s_uid] = {
                "url": e["url"], "info": info, "fmts": fmts2,
                "is_pl": True, "created": time.time(), "timeout": 120,
                "user_id": query.from_user.id, "pl_uid": uid,
                "pl_indices": list(range(len(e["entries"]))),
            }
            msg_t, kb = _kb_main(fmts2, s_uid, True, 120)
            await sm.edit_text(f"🎨 **Select Quality:**\n📋 `{len(e['entries'])}` videos:\n\n{msg_t}", reply_markup=kb)
        except Exception as ex:
            await _safe_edit(sm, f"❌ {ex}")

# ═══════════════════════════════════════════
#    DOWNLOAD TASKS
# ═══════════════════════════════════════════
async def _start_dl(uid, qual, e, query, client):
    URL_SESSIONS.pop(uid, None)
    chat_id    = query.message.chat.id
    url        = e["url"]
    info       = e["info"]
    pl_uid     = e.get("pl_uid")
    pl_indices = e.get("pl_indices")

    title = (info.get("title","") or "")[:55]
    smsg = await query.message.reply_text(
        f"⚙️ **Starting download...**\n"
        f"🎬 `{title}`\n"
        f"🎞️ `{qual[:50]}`"
    )

    if pl_uid and pl_indices is not None:
        pl_e    = PL_SESSIONS.get(pl_uid, {})
        entries = [pl_e["entries"][i] for i in pl_indices if i < len(pl_e.get("entries",[]))]
        asyncio.create_task(_dl_playlist(entries, qual, info, smsg, client, chat_id, pl_uid))
    else:
        asyncio.create_task(_dl_single(url, qual, info, smsg, client, chat_id))

async def _dl_single(url, qual, info, smsg, client, chat_id):
    loop         = asyncio.get_event_loop()
    cookies_path = await get_cookies_path()          # ← always fresh
    uid          = _new_uid()
    dl_dir       = os.path.join(DOWNLOADS_DIR, uid)
    os.makedirs(dl_dir, exist_ok=True)
    out_tmpl = os.path.join(dl_dir, "%(title,fulltitle,alt_title)s %(height)sp%(fps)s.fps %(tbr)d.%(ext)s")

    title = (info.get("title","") or "")[:55]
    await _safe_edit(smsg, f"🎬 `{title}`\n⬇️ **Downloading...**")
    result = await loop.run_in_executor(
        None, _blocking_download, url, qual, out_tmpl, smsg, loop, title, False, cookies_path
    )

    if not result:
        await _safe_edit(smsg,
            "❌ **Download failed!**\n\n"
            "• Try updating cookies: `/setcookies`\n"
            "• Is this a private video?")
        with suppress(Exception): shutil.rmtree(dl_dir)
        return

    sz = os.path.getsize(result["filepath"])
    await _safe_edit(smsg, f"🎬 `{title}`\n📤 **Uploading...**\n📦 `{humanbytes(sz)}`")
    ok = await upload_file(client, chat_id, result, qual, smsg)
    if ok:
        with suppress(Exception): await smsg.delete()
    with suppress(Exception): shutil.rmtree(dl_dir)

async def _dl_playlist(entries, qual, base_info, smsg, client, chat_id, pl_uid):
    loop         = asyncio.get_event_loop()
    cookies_path = await get_cookies_path()          # ← fresh once per playlist run
    total        = len(entries); failed = []
    for idx, en in enumerate(entries, 1):
        vid_id = en.get("id","")
        ie_key = en.get("ie_key","") or en.get("extractor","")

        if vid_id and ("youtube" in ie_key.lower() or not en.get("url")):
            vurl = f"https://www.youtube.com/watch?v={vid_id}"
        else:
            vurl = en.get("url") or en.get("webpage_url") or ""
            if vurl and not vurl.startswith("http"):
                vurl = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""

        if not vurl:
            failed.append(en.get("title") or f"Video {idx}"); continue

        vtitle = (en.get("title") or f"Video {idx}")[:50]
        prefix = f"📋 **{idx}/{total}** — `{vtitle}`\n\n"
        await _safe_edit(smsg, f"{prefix}⬇️ **Downloading...**")

        uid     = _new_uid()
        dl_dir  = os.path.join(DOWNLOADS_DIR, uid)
        os.makedirs(dl_dir, exist_ok=True)
        out_tmpl = os.path.join(dl_dir, "%(title,fulltitle,alt_title)s %(height)sp%(fps)s.fps %(tbr)d.%(ext)s")

        result = await loop.run_in_executor(
            None, _blocking_download, vurl, qual, out_tmpl, smsg, loop, vtitle, False, cookies_path
        )

        if not result:
            failed.append(vtitle)
            with suppress(Exception): shutil.rmtree(dl_dir)
            continue

        sz = os.path.getsize(result["filepath"])
        await _safe_edit(smsg, f"{prefix}📤 **Uploading...** `{humanbytes(sz)}`")
        ok = await upload_file(client, chat_id, result, qual, smsg)
        with suppress(Exception): shutil.rmtree(dl_dir)
        if not ok: failed.append(vtitle)

    PL_SESSIONS.pop(pl_uid, None)
    if failed:
        await _safe_edit(smsg, f"✅ `{total-len(failed)}/{total}` uploaded.\n❌ Failed:\n" + "\n".join(f"• `{t}`" for t in failed[:10]))
    else:
        await _safe_edit(smsg, f"✅ **Playlist complete!** `{total}` videos uploaded! 🎉")

# ═══════════════════════════════════════════
#    UTILS
# ═══════════════════════════════════════════
def _parse_sel(text, total):
    idx = set()
    try:
        for p in text.strip().split(","):
            p = p.strip()
            if "-" in p:
                a, b = map(int, p.split("-",1))
                if a<1 or b>total or a>b: return None
                idx.update(range(a-1, b))
            else:
                n = int(p)
                if n<1 or n>total: return None
                idx.add(n-1)
        return sorted(idx)
    except: return None

# ═══════════════════════════════════════════
#    HEALTH CHECK + RUNNER
# ═══════════════════════════════════════════
async def health_check(request):
    uptime = time_fmt(time.time() - _BOT_START)
    return web.Response(
        text=f"✅ Bot is running | Uptime: {uptime} | Sessions: {len(URL_SESSIONS)+len(PL_SESSIONS)}",
        content_type="text/plain",
    )

async def main():
    await app.start()
    logger.info("✅ Bot running with Pyrofork (Namespace: pyrogram)...")

    # ── 1. MongoDB ──────────────────────────────────────────────────────────
    await config.init_mongodb()

    # ── 2. Cookies: auto-import local → DB, or restore DB → local ──────────
    await auto_import_local_cookies(app)

    # ── 3. Cookies admin handlers ───────────────────────────────────────────
    await setup_cookies_handlers(app)

    # ── 4. Web health-check ─────────────────────────────────────────────────
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"✅ Health check server running on port {PORT}")

    # ── 5. Render keep-alive ────────────────────────────────────────────────
    asyncio.create_task(keep_alive())

    await idle()
    await app.stop()
    await config.close_mongodb()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
