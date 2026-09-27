# Telegram Downloader Bot: Enhanced Version
# Multi-platform downloader (SoundCloud, Spotify, YouTube, Pinterest,
# Instagram, TikTok, Twitter).
# Reorganized into 9 PARTs for maintainability.


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 0 — CONFIG & STARTUP                                         ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 0.1  Imports (all import/from statements consolidated at top) ───────
import os
import re
import shutil
import sqlite3
import tempfile
import requests
import yt_dlp
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot import apihelper
import threading
from flask import Flask
import time
import logging
from datetime import datetime
import random
import json
import queue
from contextlib import contextmanager
import hashlib as _hashlib     # was at line 1181 (mid-file)
import uuid as _uuid           # was at line 1294 (mid-file)
import threading as _threading_mod  # was at line 4919 (mid-file)

# ── 0.2  Config (BOT_TOKEN, paths, limits, cookies) ─────────────────────
BOT_TOKEN = "8382981392:AAF8XVGAl4SLqrewcZWy6Joa2JuJlwDIzfA"
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
CHANNEL_USERNAME = "@TheDarkestNest"
DB_PATH = "sc_bot.db"
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
FORCE_MP3 = False
COMPANION_ID = "@Theirodentv"
PORT = int(os.environ.get('PORT', 5000))
COOKIES_PATH = "cookies.txt"

# Check if cookies file exists
COOKIES_AVAILABLE = os.path.exists(COOKIES_PATH)

# ===== Media Archive Config =====
# Private/public archive channel. The bot (current and future) must be an
# administrator with permission to post messages. Survives bot-token changes:
# a new bot only needs to be added as admin of the same channel.
ARCHIVE_CHANNEL_ID = -1004465114858
ARCHIVE_CHANNEL_USERNAME = "@NestMusicGreatArchive"
ENABLE_MEDIA_ARCHIVE = True
ARCHIVE_CAPTION = "<blockquote>@NestMusicGreatArchive</blockquote>"

# ── 0.3  Deno runtime setup (_ensure_spotdl_local_deno, _get_deno_ytdlp_opts, _deno_opts, _DENO_YTDL_OPTS) ────
# ===== Deno JavaScript runtime (REQUIRED for YouTube extraction) =====
# yt-dlp (latest) needs a JavaScript runtime to solve YouTube's signature/n-
# challenge. Without deno, downloads fail with "The page needs to be reloaded"
# or "Sign in to confirm you're not a bot" — this was the root cause of the
# OSNOVA artist tracks (Celebration, Geared, Burning Time, Thanks a Lot, etc.)
# failing to download.
#
# We ensure deno is available at startup (downloading it via spotdl's helper
# if missing) and expose a ready-to-merge yt-dlp options dict to every direct
def _ensure_spotdl_local_deno(deno_path):
    """Make sure spotdl's LOCAL deno (~/.config/spotdl/deno) exists.

    Why this matters: spotdl's AudioProvider wires deno into its internal
    yt-dlp ONLY through the local-path helper (get_local_deno_yt_dlp_options).
    When deno is installed globally (e.g. ~/.deno/bin/deno via the official
    installer), that helper returns {} and spotdl's yt-dlp then depends on
    PATH auto-detection — which BREAKS under systemd services (minimal PATH:
    /usr/bin:/bin, no ~/.deno/bin). We fix that by COPYING the global deno
    into spotdl's local path, so spotdl gets an explicit path that works in
    ANY environment. (A copy is used instead of a symlink because symlink
    creation is blocked/killed in some restricted environments — Docker with
    seccomp, hardened sandboxes.) Falls back to downloading if copying fails.
    All failures are non-fatal (the bot keeps its own explicit wiring).
    """
    try:
        from spotdl.utils.deno import get_local_deno
        if get_local_deno() is not None:
            return True  # local deno already present — spotdl is wired
        from spotdl.utils.config import get_spotdl_path
        spotdl_dir = str(get_spotdl_path())
        os.makedirs(spotdl_dir, exist_ok=True)
        local_path = os.path.join(spotdl_dir, "deno")
        # Copy the found deno into spotdl's local path (~95MB, works everywhere)
        try:
            shutil.copy2(str(deno_path), local_path)
            os.chmod(local_path, 0o755)
            size_mb = os.path.getsize(local_path) // (1024 * 1024)
            print(f"[startup] copied deno into spotdl path: {local_path} ({size_mb} MB)")
            return True
        except Exception as e:
            print(f"[startup] could not copy deno ({e}) — downloading local copy…")
        from spotdl.utils.deno import download_deno
        download_deno()
        print("[startup] spotdl-local deno downloaded")
        return True
    except Exception as e:
        print(f"[startup] spotdl-local deno setup failed (non-fatal): {type(e).__name__}: {e}")
        return False

def _get_deno_ytdlp_opts():
    """Return yt-dlp options dict that enables the deno JS runtime.

    Returns {} if deno cannot be made available (the bot will still work for
    SoundCloud / non-YouTube sources, but YouTube downloads will fail).
    """
    try:
        from spotdl.utils.deno import (
            get_deno_path, is_deno_installed, download_deno,
        )
        if not is_deno_installed():
            try:
                print("[startup] deno not found — downloading via spotdl helper...")
                download_deno()
                p = get_deno_path()
                if p:
                    print(f"[startup] deno installed at {p}")
                else:
                    print("[startup] deno download returned no path — YouTube may fail")
                    return {}
            except Exception as e:
                print(f"[startup] deno auto-download failed: {type(e).__name__}: {e}")
                print("[startup] ! YouTube downloads will likely fail without deno.")
                return {}
        path = get_deno_path()
        if path:
            # ALSO wire spotdl's LOCAL deno so spotdl's internal yt-dlp gets an
            # explicit deno path (works even under systemd's minimal PATH).
            _ensure_spotdl_local_deno(path)
            return {"js_runtimes": {"deno": {"path": str(path)}}}
    except Exception as e:
        print(f"[startup] deno check failed: {type(e).__name__}: {e}")
    return {}
# Computed once at module load (eager), so the first download isn't delayed.
_DENO_YTDL_OPTS = _get_deno_ytdlp_opts()

def _deno_opts():
    """Accessor for the cached deno yt-dlp options (so we can re-check lazily)."""
    global _DENO_YTDL_OPTS
    if not _DENO_YTDL_OPTS:
        _DENO_YTDL_OPTS = _get_deno_ytdlp_opts()
    return _DENO_YTDL_OPTS

# ── 0.4  Spotify config + Telegram bot init + lazy _ensure_bot_me + apihelper timeouts ────
# ===== Spotify Config =====
# Audio is downloaded with spotdl (which uses yt-dlp internally with multiple
# audio providers). Metadata comes from spotapi.
#
# spotdl provider order (tried in sequence until one succeeds):
#   1. soundcloud     (reliable, no bot-check, good coverage for popular songs)
#   2. youtube-music  (best global coverage, may bot-check on datacenter IPs)
#   3. youtube        (fallback)
#   4. bandcamp       (last resort, artist-uploaded)
SPOTDL_AUDIO_PROVIDERS = ["youtube-music", "soundcloud", "youtube", "bandcamp"]
SPOTIFY_AUDIO_BITRATE = "192"     # default mp3 bitrate (overridden by user setting)
SPOTIFY_ENABLED = True            # set False at runtime if spotapi/spotdl import fails

# Proxy capability has been intentionally removed — the bot now downloads
# directly. (Previously: free proxy lists were used to try to bypass
# SoundCloud geo-restrictions, but this was unreliable and is no longer
# needed since yt-dlp + deno can reach YouTube directly.)

# Optimized settings for Replit
os.environ['PYTHONUNBUFFERED'] = '1'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
# Lazy fetch of bot identity — avoids failing at import if the token is
# temporarily invalid (e.g. during local testing). Fetched on first use.
BOT_ME = None
BOT_USERNAME = "nest_agent_bot"
BOT_NICKNAME = "NEST Agent"
def _ensure_bot_me():
    global BOT_ME, BOT_USERNAME, BOT_NICKNAME
    if BOT_ME is not None:
        return
    try:
        BOT_ME = bot.get_me()
        BOT_USERNAME = BOT_ME.username or "nest_agent_bot"
        BOT_NICKNAME = BOT_ME.first_name or BOT_USERNAME
    except Exception as e:
        print(f"[Bot] get_me warning: {e} (using fallback identity)")
apihelper.SESSION_TIMEOUT = 60
apihelper.READ_TIMEOUT = 60
apihelper.CONNECT_TIMEOUT = 60

# ── 0.5  Per-platform quality constants + styled buttons (btn, _resolve_style, _STYLE_MAP) ────
# ===== Per-platform quality options =====
# Each platform exposes a list of (value, label-fa, label-en, sort_rank) tuples.
# "best" means: use the best available, but apply the smart 50 MB fallback.
PLATFORM_QUALITIES = {
    # SoundCloud: high = best audio; low = <=128 kbps
    "sc": [
        ("high", "کیفیت بالا 🎧", "High quality 🎧", 1),
        ("low",  "کیفیت سبک 🔉", "Light quality 🔉", 2),
    ],
    # Spotify: mp3 bitrate (yt-dlp postprocessor)
    "spotify": [
        ("320", "MP3 320 kbps ⭐️", "MP3 320 kbps ⭐️", 1),
        ("192", "MP3 192 kbps",    "MP3 192 kbps",    2),
        ("128", "MP3 128 kbps 🔉", "MP3 128 kbps 🔉", 3),
    ],
    # Instagram / TikTok / Pinterest: vertical-video quality ceiling
    "ig": [
        ("best",  "بهترین کیفیت ⭐️", "Best quality ⭐️", 1),
        ("720",   "720p",            "720p",            2),
        ("480",   "480p 🔉",         "480p 🔉",         3),
    ],
    "tt": [
        ("best",  "بهترین کیفیت ⭐️", "Best quality ⭐️", 1),
        ("720",   "720p",            "720p",            2),
        ("480",   "480p 🔉",         "480p 🔉",         3),
    ],
    "pin": [
        ("best",   "بهترین کیفیت ⭐️", "Best quality ⭐️", 1),
        ("1080",   "1080p",           "1080p",           2),
        ("720",    "720p 🔉",        "720p 🔉",         3),
    ],
    # YouTube Shorts: vertical, max 1080p
    "yt_shorts": [
        ("best",  "بهترین (تا 1080p) ⭐️", "Best (up to 1080p) ⭐️", 1),
        ("720",   "720p",                  "720p",                  2),
        ("480",   "480p 🔉",               "480p 🔉",               3),
    ],
}
DEFAULT_QUALITIES = {
    "sc": "high", "spotify": "320",
    "ig": "best", "tt": "best", "pin": "best",
    "yt_shorts": "best",
}
# ===== Styled Buttons (native Telegram button colors) =====
# pyTelegramBotAPI >= 4.26 supports a `style` parameter on InlineKeyboardButton
# and KeyboardButton. Telegram renders this as a colored button background.
# Supported styles: "primary" (blue), "success" (green), "danger" (red).
# Any other value (None / "normal") → default appearance.
#
# We expose the same API the docs show: btn(text, callback_data=..., style="primary")
# Non-standard styles ("warning", "info") are mapped to the closest supported one.
_STYLE_MAP = {
    "primary": "primary",   # blue
    "success": "success",   # green
    "danger":  "danger",    # red
    "warning": "primary",   # no yellow → blue
    "info":    "primary",   # no purple → blue
    "normal":  None,
    None:      None,
}
def _resolve_style(style):
    """Map any style name to a Telegram-supported style (or None)."""
    if style is None:
        return None
    if style in ("primary", "success", "danger"):
        return style
    return _STYLE_MAP.get(style, None)
def btn(text, callback_data=None, style=None, url=None, **kw):
    """Create a styled InlineKeyboardButton with native Telegram colors.

    Mirrors the documented API: btn(text, callback_data=..., style="primary").
    Supported: "primary" (blue), "success" (green), "danger" (red).
    """
    actual_style = _resolve_style(style)
    label = text[:64] if text else text  # Telegram button text limit
    if url is not None:
        return InlineKeyboardButton(text=label, url=url, style=actual_style, **kw)
    return InlineKeyboardButton(text=label, callback_data=callback_data, style=actual_style, **kw)
def rkb(text, style=None, **kw):
    """Create a styled ReplyKeyboardButton with native Telegram colors."""
    from telebot.types import KeyboardButton
    actual_style = _resolve_style(style)
    label = text[:64] if text else text
    return KeyboardButton(text=label, style=actual_style, **kw)


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 1 — DATABASE & PERSISTENT STATE                              ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 1.1  Connection pool (ConnectionPool class, db_pool instance, @contextmanager) ────
# ===== Connection Pool Implementation =====
class ConnectionPool:
    """Thread-safe connection pool for SQLite"""
    
    def __init__(self, db_path, max_connections=10):
        self.db_path = db_path
        self.max_connections = max_connections
        self.pool = queue.Queue(maxsize=max_connections)
        self.lock = threading.Lock()
        self.created_connections = 0
        
        # Pre-create some connections
        for _ in range(min(3, max_connections)):
            self._create_connection()
    
    def _create_connection(self):
        """Create a new connection and add it to the pool"""
        if self.created_connections >= self.max_connections:
            return None
            
        try:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
                isolation_level=None  # Autocommit mode
            )
            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=10000")
            conn.execute("PRAGMA temp_store=MEMORY")
            
            self.pool.put(conn)
            self.created_connections += 1
            return conn
        except Exception as e:
            print(f"Error creating connection: {e}")
            return None
    
    @contextmanager
    def get_connection(self):
        """Get a connection from the pool"""
        conn = None
        try:
            # Try to get a connection from the pool
            try:
                conn = self.pool.get(timeout=5.0)
            except queue.Empty:
                # Pool is empty, try to create a new connection
                conn = self._create_connection()
                if conn is None:
                    # Still couldn't get a connection, wait and try again
                    time.sleep(0.1)
                    conn = self.pool.get(timeout=10.0)
            
            yield conn
        except Exception as e:
            print(f"Error getting connection: {e}")
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            raise
        finally:
            # Return the connection to the pool
            if conn:
                try:
                    self.pool.put(conn, timeout=1.0)
                except queue.Full:
                    # Pool is full, close this connection
                    try:
                        conn.close()
                        self.created_connections -= 1
                    except:
                        pass
# Initialize connection pool
db_pool = ConnectionPool(DB_PATH, max_connections=20)

# ── 1.2  db_init (all CREATE TABLE statements + migrations together) ────
def db_init():
    """Initialize database with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        # Existing tables
        c.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, lang TEXT, quality TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS stats (chat_id INTEGER, count INTEGER, bytes INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS totals (id INTEGER PRIMARY KEY, count INTEGER, bytes INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS search_cache (chat_id INTEGER, idx INTEGER, url TEXT, title TEXT, artist TEXT, duration INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS playlist_cache (chat_id INTEGER, idx INTEGER, url TEXT, title TEXT, artist TEXT, duration INTEGER)")

        # New tables for advanced statistics
        c.execute("""
            CREATE TABLE IF NOT EXISTS detailed_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                platform TEXT,
                file_type TEXT,
                file_size INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users (chat_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS uptime_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                total_downloads INTEGER DEFAULT 0,
                total_processed INTEGER DEFAULT 0
            )
        """)

        # New table for YouTube quality cache
        c.execute("""
            CREATE TABLE IF NOT EXISTS youtube_quality_cache (
                chat_id INTEGER PRIMARY KEY,
                url TEXT,
                qualities TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # New table for YouTube Shorts cache
        c.execute("""
            CREATE TABLE IF NOT EXISTS youtube_shorts_cache (
                chat_id INTEGER PRIMARY KEY,
                url TEXT,
                is_short BOOLEAN,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Spotify track selection cache (album/playlist tracks for pick + batch)
        c.execute("""
            CREATE TABLE IF NOT EXISTS spotify_cache (
                chat_id INTEGER, idx INTEGER, track_id TEXT, title TEXT, artist TEXT,
                album TEXT, duration INTEGER, cover TEXT, track_number INTEGER, url TEXT
            )
        """)

        # Batch meta cache (album/playlist title + cover) for "Download All"
        c.execute("""
            CREATE TABLE IF NOT EXISTS batch_meta_cache (
                chat_id INTEGER PRIMARY KEY,
                kind TEXT,
                title TEXT,
                artist TEXT,
                cover TEXT,
                url TEXT,
                count INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Per-platform quality settings
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER PRIMARY KEY,
                sc TEXT,
                spotify TEXT,
                ig TEXT,
                tt TEXT,
                pin TEXT,
                yt_shorts TEXT
            )
        """)

        # Migration: add search_engine column if not present
        try:
            c.execute("ALTER TABLE user_settings ADD COLUMN search_engine TEXT")
        except Exception:
            pass  # column already exists

        # Migration: dedupe_discography (skip duplicate tracks in discography batch)
        try:
            c.execute("ALTER TABLE user_settings ADD COLUMN dedupe_discography INTEGER DEFAULT 0")
        except Exception:
            pass

        # === Media archive (Telegram channel message pointers; NO audio files) ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS media_archive (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                platform        TEXT NOT NULL,
                track_key       TEXT NOT NULL,
                quality         TEXT NOT NULL DEFAULT '',
                archive_chat_id INTEGER NOT NULL,
                archive_msg_id  INTEGER NOT NULL,
                file_type       TEXT NOT NULL,
                title           TEXT,
                artist          TEXT,
                duration        INTEGER,
                size            INTEGER,
                collection_key  TEXT,
                collection_kind TEXT,
                artist_key      TEXT,
                position        INTEGER,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_used       DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, track_key, quality)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_archive_lookup
            ON media_archive(platform, track_key, quality)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_archive_collection
            ON media_archive(collection_key, quality)
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS media_archive_headers (
                collection_key  TEXT NOT NULL,
                quality         TEXT NOT NULL DEFAULT '',
                header_msg_id   INTEGER NOT NULL,
                kind            TEXT,
                title           TEXT,
                artist          TEXT,
                artist_key      TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (collection_key, quality)
            )
        """)

        # === Batch resume state ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS batch_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                kind TEXT,
                panel_id TEXT,
                title TEXT,
                artist TEXT,
                cover TEXT,
                url TEXT,
                total INTEGER,
                done_json TEXT,
                failed_json TEXT,
                tracks_json TEXT,
                status TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_batch_state_chat ON batch_state(chat_id, status)")

        # === Panels (per-panel context) ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS panels (
                chat_id INTEGER,
                panel_id TEXT,
                kind TEXT,
                tracks_json TEXT,
                meta_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, panel_id)
            )
        """)

        c.execute("INSERT OR IGNORE INTO totals (id, count, bytes) VALUES (1, 0, 0)")
        c.execute("INSERT OR IGNORE INTO uptime_stats (id, total_downloads, total_processed) VALUES (1, 0, 0)")

        # Create indexes for better performance
        c.execute("CREATE INDEX IF NOT EXISTS idx_detailed_stats_chat_id ON detailed_stats(chat_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_detailed_stats_timestamp ON detailed_stats(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_detailed_stats_platform ON detailed_stats(platform)")

        conn.commit()

# ── 1.3  User basics: lang, quality, search engine, dedupe, platform quality, stats ────
def get_user_lang(chat_id):
    """Get user language with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT lang FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        return row[0] if row and row[0] in LANGS else None
def set_user_lang(chat_id, lang):
    """Set user language with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO users (chat_id, lang, quality) VALUES (?, ?, COALESCE((SELECT quality FROM users WHERE chat_id=?),'high'))", (chat_id, lang, chat_id))
        conn.commit()
def get_user_quality(chat_id):
    """Get user's SoundCloud quality preference (reads from user_settings first, falls back to users table)."""
    val = get_platform_quality(chat_id, "sc")
    if val in ("high", "low"):
        return val
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT quality FROM users WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if row and row[0] in ("high", "low"):
            set_platform_quality(chat_id, "sc", row[0])
            return row[0]
    return "high"
def set_user_quality(chat_id, q):
    """Set user's SoundCloud quality (writes to both tables for backward compat)."""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO users (chat_id, lang, quality) VALUES (?, COALESCE((SELECT lang FROM users WHERE chat_id=?),'en'), ?)", (chat_id, chat_id, q))
        conn.commit()
    set_platform_quality(chat_id, "sc", q)
# ===== Per-platform quality settings =====
_SETTINGS_COLUMNS = ("sc", "spotify", "ig", "tt", "pin", "yt_shorts")
def get_platform_quality(chat_id, platform_key):
    """Get a user's preferred quality for a given platform key.
    Returns the DEFAULT_QUALITIES value if the user has not set one.
    """
    col = platform_key if platform_key in _SETTINGS_COLUMNS else None
    if col is None:
        return DEFAULT_QUALITIES.get(platform_key, "best")
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute(f"SELECT {col} FROM user_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if row and row[0]:
            return row[0]
    return DEFAULT_QUALITIES.get(platform_key, "best")
def set_platform_quality(chat_id, platform_key, value):
    """Set a user's preferred quality for a given platform key."""
    if platform_key not in _SETTINGS_COLUMNS:
        return
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_settings (chat_id) VALUES (?)", (chat_id,))
        c.execute(f"UPDATE user_settings SET {platform_key}=? WHERE chat_id=?", (value, chat_id))
        conn.commit()
def get_all_platform_qualities(chat_id):
    """Return a dict of {platform_key: quality} for the user (with defaults)."""
    out = {}
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT sc, spotify, ig, tt, pin, yt_shorts FROM user_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    row = row or (None, None, None, None, None, None)
    for col, val in zip(_SETTINGS_COLUMNS, row):
        out[col] = val if val else DEFAULT_QUALITIES.get(col, "best")
    return out
def get_platform_quality_label(chat_id, platform_key, lang=None):
    """Human-readable label for the user's current quality on a platform."""
    lang = lang or get_user_lang(chat_id) or "en"
    val = get_platform_quality(chat_id, platform_key)
    options = PLATFORM_QUALITIES.get(platform_key, [])
    for v, fa, en, _ in options:
        if v == val:
            return fa if lang == "fa" else en
    return val
# ===== Search engine preference =====
DEFAULT_SEARCH_ENGINE = "spotify"  # default search engine
def get_search_engine(chat_id):
    """Get user's preferred search engine: 'spotify' or 'soundcloud'."""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT search_engine FROM user_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if row and row[0] in ("spotify", "soundcloud"):
            return row[0]
    return DEFAULT_SEARCH_ENGINE
def set_search_engine(chat_id, engine):
    """Set user's preferred search engine."""
    if engine not in ("spotify", "soundcloud"):
        return
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_settings (chat_id) VALUES (?)", (chat_id,))
        c.execute("UPDATE user_settings SET search_engine=? WHERE chat_id=?", (engine, chat_id))
        conn.commit()
# ===== Dedupe discography setting =====
def get_dedupe_setting(chat_id):
    """Whether to skip duplicate tracks in discography batches. Returns bool."""
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT dedupe_discography FROM user_settings WHERE chat_id=?", (chat_id,))
            row = c.fetchone()
            return bool(row and row[0])
    except Exception:
        return False
def set_dedupe_setting(chat_id, on):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_settings (chat_id) VALUES (?)", (chat_id,))
        c.execute("UPDATE user_settings SET dedupe_discography=? WHERE chat_id=?", (1 if on else 0, chat_id))
        conn.commit()
def add_detailed_stats(chat_id, platform, file_type, file_size):
    """Add detailed statistics to database with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        # Add to detailed stats table
        c.execute("INSERT INTO detailed_stats (chat_id, platform, file_type, file_size) VALUES (?, ?, ?, ?)", (chat_id, platform, file_type, file_size))

        # Update general stats
        c.execute("SELECT count, bytes FROM stats WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE stats SET count=?, bytes=? WHERE chat_id=?", (row[0] + 1, row[1] + file_size, chat_id))
        else:
            c.execute("INSERT INTO stats (chat_id, count, bytes) VALUES (?, ?, ?)", (chat_id, 1, file_size))

        # Update global stats
        c.execute("SELECT count, bytes FROM totals WHERE id=1")
        t = c.fetchone()
        c.execute("UPDATE totals SET count=?, bytes=? WHERE id=1", (t[0] + 1, t[1] + file_size))

        # Update uptime stats
        c.execute("UPDATE uptime_stats SET total_downloads = total_downloads + 1 WHERE id=1")

        conn.commit()
def add_stats_with_platform(chat_id, platform, file_type, file_size):
    """Register stats with platform and file type"""
    add_detailed_stats(chat_id, platform, file_type, file_size)
# Cache functions for YouTube qualities with connection pooling
def save_youtube_qualities(chat_id, url, qualities):
    """Save YouTube qualities for a URL with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO youtube_quality_cache (chat_id, url, qualities) VALUES (?, ?, ?)", 
                  (chat_id, url, json.dumps(qualities)))
        conn.commit()
def get_youtube_qualities(chat_id, url):
    """Get cached YouTube qualities with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT qualities FROM youtube_quality_cache WHERE chat_id=? AND url=?", (chat_id, url))
        row = c.fetchone()
        
        if row:
            try:
                return json.loads(row[0])
            except:
                return None
        return None
def clear_youtube_quality_cache(chat_id):
    """Clear YouTube quality cache for a user with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM youtube_quality_cache WHERE chat_id=?", (chat_id,))
        conn.commit()
# Cache functions for YouTube Shorts detection with connection pooling
def save_youtube_shorts_info(chat_id, url, is_short):
    """Save YouTube Shorts detection info with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO youtube_shorts_cache (chat_id, url, is_short) VALUES (?, ?, ?)", 
                  (chat_id, url, is_short))
        conn.commit()
def get_youtube_shorts_info(chat_id, url):
    """Get cached YouTube Shorts info with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT is_short FROM youtube_shorts_cache WHERE chat_id=? AND url=?", (chat_id, url))
        row = c.fetchone()
        
        if row:
            return row[0]
        return None
def clear_youtube_shorts_cache(chat_id):
    """Clear YouTube Shorts cache for a user with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM youtube_shorts_cache WHERE chat_id=?", (chat_id,))
        conn.commit()
# ===== Helper Functions =====
def get_stats(chat_id):
    """Get user statistics from detailed_stats with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        # User stats from detailed_stats
        c.execute("SELECT COUNT(*) as count, SUM(file_size) as bytes FROM detailed_stats WHERE chat_id = ?", (chat_id,))
        user_row = c.fetchone()

        # Global stats from detailed_stats
        c.execute("SELECT COUNT(*) as count, SUM(file_size) as bytes FROM detailed_stats")
        global_row = c.fetchone()

        return {
            "user_count": user_row[0] or 0,
            "user_bytes": user_row[1] or 0,
            "total_count": global_row[0] or 0,
            "total_bytes": global_row[1] or 0
        }
def get_uptime_stats():
    """Get bot uptime statistics with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        c.execute("SELECT start_time, total_downloads, total_processed FROM uptime_stats WHERE id=1")
        row = c.fetchone()

        if row:
            start_time, total_downloads, total_processed = row
            # Calculate uptime
            if start_time:
                start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                now = datetime.now()
                uptime_seconds = (now - start_dt).total_seconds()

                days = int(uptime_seconds // 86400)
                hours = int((uptime_seconds % 86400) // 3600)
                minutes = int((uptime_seconds % 3600) // 60)

                uptime_str = f"{days}d {hours}h {minutes}m"
            else:
                uptime_str = "Unknown"

            return {
                "uptime": uptime_str,
                "total_downloads": total_downloads,
                "total_processed": total_processed
            }

        return {"uptime": "Unknown", "total_downloads": 0, "total_processed": 0}
def get_user_display_name(chat_id):
    """Get user display name (nickname or full name)"""
    try:
        user_info = bot.get_chat(chat_id)
        # Priority to nickname
        if user_info.username:
            return f"@{user_info.username}"
        elif user_info.first_name:
            if user_info.last_name:
                return f"{user_info.first_name} {user_info.last_name}"
            else:
                return user_info.first_name
        else:
            return f"ID:{chat_id}"
    except:
        return f"ID:{chat_id}"
def get_user_username(chat_id):
    """Get pure username (for profile link)"""
    try:
        user_info = bot.get_chat(chat_id)
        return user_info.username
    except:
        return None
def get_top_users_all_time(limit=3):
    """Get top users of all time with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        c.execute("""
            SELECT ds.chat_id, COUNT(*) as download_count, SUM(ds.file_size) as total_size
            FROM detailed_stats ds
            GROUP BY ds.chat_id
            ORDER BY download_count DESC
            LIMIT ?
        """, (limit,))

        results = []
        for row in c.fetchall():
            chat_id, count, size = row

            # Get user display name
            display_name = get_user_display_name(chat_id)

            # Get most used platform for this user correctly
            c.execute("""
                SELECT platform, COUNT(*) as platform_count
                FROM detailed_stats 
                WHERE chat_id = ?
                GROUP BY platform
                ORDER BY platform_count DESC
                LIMIT 1
            """, (chat_id,))
            platform_row = c.fetchone()
            
            if platform_row:
                most_used_platform = platform_row[0]
            else:
                most_used_platform = "Unknown"

            results.append({
                "chat_id": chat_id,
                "display_name": display_name,
                "download_count": count,
                "total_size": size,
                "most_used_platform": most_used_platform
            })

        return results
def get_top_users_daily(limit=3):
    """Get top daily users with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        c.execute("""
            SELECT ds.chat_id, COUNT(*) as download_count, SUM(ds.file_size) as total_size
            FROM detailed_stats ds
            WHERE DATE(ds.timestamp) = DATE('now')
            GROUP BY ds.chat_id
            ORDER BY download_count DESC
            LIMIT ?
        """, (limit,))

        results = []
        for row in c.fetchall():
            chat_id, count, size = row

            display_name = get_user_display_name(chat_id)

            # Get most used platform for this user correctly
            c.execute("""
                SELECT platform, COUNT(*) as platform_count
                FROM detailed_stats 
                WHERE chat_id = ? AND DATE(timestamp) = DATE('now')
                GROUP BY platform
                ORDER BY platform_count DESC
                LIMIT 1
            """, (chat_id,))
            platform_row = c.fetchone()
            
            if platform_row:
                most_used_platform = platform_row[0]
            else:
                most_used_platform = "Unknown"

            results.append({
                "chat_id": chat_id,
                "display_name": display_name,
                "download_count": count,
                "total_size": size,
                "most_used_platform": most_used_platform
            })

        return results
def get_top_users_weekly(limit=3):
    """Get top weekly users with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        c.execute("""
            SELECT ds.chat_id, COUNT(*) as download_count, SUM(ds.file_size) as total_size
            FROM detailed_stats ds
            WHERE ds.timestamp >= datetime('now', '-7 days')
            GROUP BY ds.chat_id
            ORDER BY download_count DESC
            LIMIT ?
        """, (limit,))

        results = []
        for row in c.fetchall():
            chat_id, count, size = row

            display_name = get_user_display_name(chat_id)

            # Get most used platform for this user correctly
            c.execute("""
                SELECT platform, COUNT(*) as platform_count
                FROM detailed_stats 
                WHERE chat_id = ? AND timestamp >= datetime('now', '-7 days')
                GROUP BY platform
                ORDER BY platform_count DESC
                LIMIT 1
            """, (chat_id,))
            platform_row = c.fetchone()
            
            if platform_row:
                most_used_platform = platform_row[0]
            else:
                most_used_platform = "Unknown"

            results.append({
                "chat_id": chat_id,
                "display_name": display_name,
                "download_count": count,
                "total_size": size,
                "most_used_platform": most_used_platform
            })

        return results
def get_platform_ranking_all_time():
    """Platform ranking all time with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        c.execute("SELECT platform, COUNT(*) as download_count, SUM(file_size) as total_size FROM detailed_stats GROUP BY platform ORDER BY download_count DESC")

        results = []
        for row in c.fetchall():
            platform, count, size = row
            results.append({
                "platform": platform,
                "download_count": count,
                "total_size": size
            })

        return results
def get_platform_ranking_daily():
    """Daily platform ranking with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        c.execute("SELECT platform, COUNT(*) as download_count, SUM(file_size) as total_size FROM detailed_stats WHERE DATE(timestamp) = DATE('now') GROUP BY platform ORDER BY download_count DESC")

        results = []
        for row in c.fetchall():
            platform, count, size = row
            results.append({
                "platform": platform,
                "download_count": count,
                "total_size": size
            })

        return results
def get_platform_ranking_weekly():
    """Weekly platform ranking with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        c.execute("SELECT platform, COUNT(*) as download_count, SUM(file_size) as total_size FROM detailed_stats WHERE timestamp >= datetime('now', '-7 days') GROUP BY platform ORDER BY download_count DESC")

        results = []
        for row in c.fetchall():
            platform, count, size = row
            results.append({
                "platform": platform,
                "download_count": count,
                "total_size": size
            })

        return results
def get_user_platform_stats(chat_id, period='all'):
    """User platform statistics with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        if period == 'daily':
            c.execute("SELECT platform, COUNT(*) as download_count, SUM(file_size) as total_size FROM detailed_stats WHERE chat_id = ? AND DATE(timestamp) = DATE('now') GROUP BY platform ORDER BY download_count DESC", (chat_id,))
        elif period == 'weekly':
            c.execute("SELECT platform, COUNT(*) as download_count, SUM(file_size) as total_size FROM detailed_stats WHERE chat_id = ? AND timestamp >= datetime('now', '-7 days') GROUP BY platform ORDER BY download_count DESC", (chat_id,))
        else:  # all time
            c.execute("SELECT platform, COUNT(*) as download_count, SUM(file_size) as total_size FROM detailed_stats WHERE chat_id = ? GROUP BY platform ORDER BY download_count DESC", (chat_id,))

        results = []
        for row in c.fetchall():
            platform, count, size = row
            results.append({
                "platform": platform,
                "download_count": count,
                "total_size": size
            })

        return results
def get_user_stats(chat_id, period='all'):
    """User statistics for specific period with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()

        if period == 'daily':
            c.execute("SELECT COUNT(*) as count, SUM(file_size) as bytes FROM detailed_stats WHERE chat_id = ? AND DATE(timestamp) = DATE('now')", (chat_id,))
        elif period == 'weekly':
            c.execute("SELECT COUNT(*) as count, SUM(file_size) as bytes FROM detailed_stats WHERE chat_id = ? AND timestamp >= datetime('now', '-7 days')", (chat_id,))
        else:  # all time
            c.execute("SELECT COUNT(*) as count, SUM(file_size) as bytes FROM detailed_stats WHERE chat_id = ?", (chat_id,))

        row = c.fetchone()
        if row:
            count, bytes = row
            return {"count": count or 0, "bytes": bytes or 0}

        return {"count": 0, "bytes": 0}

# ── 1.4  Media archive (channel copyMessage pointers; no files on disk) ────
# Archive posts are serialized through one worker so concurrent users never
# interleave albums/discographies in the channel. No audio is stored on disk.
_ARCHIVE_JOB_QUEUE = queue.Queue()
_ARCHIVE_WORKER_STARTED = False
_ARCHIVE_WORKER_LOCK = threading.Lock()
_USER_JOB_QUEUES = {}
_USER_QUEUE_LOCK = threading.Lock()
_USER_QUEUE_ACTIVE = set()

def _html_esc(text):
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def _normalize_release_date(raw):
    """Return (year_str, date_str) from mixed Spotify/SoundCloud/yt-dlp shapes."""
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, dict):
        year = raw.get("year")
        iso = raw.get("isoString") or raw.get("iso") or raw.get("precision") or raw.get("name")
        if year and not iso:
            return str(year), str(year)
        return _normalize_release_date(iso if iso else year)
    if isinstance(raw, (int, float)):
        s = str(int(raw))
        if len(s) == 4:
            return s, s
        if len(s) == 8:
            return s[:4], f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    if len(s) == 8 and s.isdigit():
        return s[:4], f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    m = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", s)
    if not m:
        return None, None
    year = m.group(1)
    if m.group(2) and m.group(3):
        return year, f"{year}-{m.group(2)}-{m.group(3)}"
    if m.group(2):
        return year, f"{year}-{m.group(2)}"
    return year, year

def extract_release_from_meta(meta):
    """Pick the best release year/date from a track or yt-dlp info dict."""
    if not meta:
        return None, None
    for key in ("release_date", "releaseDate", "date", "upload_date", "release_year",
                "year", "album_date", "album_year"):
        year, date = _normalize_release_date(meta.get(key))
        if year:
            return year, date
    for nested_key in ("albumOfTrack", "album", "date"):
        nested = meta.get(nested_key)
        if isinstance(nested, dict):
            year, date = extract_release_from_meta(nested)
            if year:
                return year, date
    return None, None

def extract_track_key(platform, item=None, url=None):
    """Stable cache key per platform (quality is stored separately)."""
    item = item or {}
    plat = (platform or "").strip().lower()
    if plat in ("spotify",):
        tid = item.get("id") or item.get("track_id") or item.get("tid")
        if tid and str(tid) not in ("", "_no_spotify_id_"):
            return str(tid)
        u = url or item.get("url") or ""
        m = re.search(r"(?:track/|spotify:track:)([a-zA-Z0-9]+)", str(u))
        if m:
            return m.group(1)
        raw = f"{item.get('artist','')}\x1f{item.get('title','')}\x1f{item.get('duration') or 0}"
        return _hashlib.sha1(raw.encode("utf-8")).hexdigest()
    u = url or item.get("url") or item.get("webpage_url") or item.get("original_url") or ""
    u = str(u).split("?")[0].split("#")[0].rstrip("/").replace("http://", "https://").lower()
    if "soundcloud.com" in u:
        return u
    if u:
        return u
    raw = f"{item.get('artist','')}\x1f{item.get('title','')}\x1f{item.get('duration') or 0}"
    return _hashlib.sha1(raw.encode("utf-8")).hexdigest()

def archive_quality_for(chat_id, platform):
    plat = (platform or "").strip().lower()
    if plat == "spotify":
        q = get_platform_quality(chat_id, "spotify") if chat_id else None
        return q if q in ("320", "192", "128") else "320"
    q = get_user_quality(chat_id) if chat_id else None
    return q if q in ("high", "low") else "high"

def archive_platform_name(platform):
    p = (platform or "").strip().lower()
    if p in ("spotify",):
        return "spotify"
    if p in ("soundcloud", "sc"):
        return "soundcloud"
    return p or "unknown"

def get_archive_record(platform, track_key, quality=""):
    """Return archive record dict or None."""
    if not track_key:
        return None
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT platform, track_key, quality, archive_chat_id, archive_msg_id, "
                "file_type, title, artist, duration, size, collection_key, collection_kind, artist_key, position "
                "FROM media_archive WHERE platform=? AND track_key=? AND quality=?",
                (archive_platform_name(platform), str(track_key), str(quality or "")),
            )
            row = c.fetchone()
            if not row:
                return None
            return {
                "platform": row[0], "track_key": row[1], "quality": row[2],
                "archive_chat_id": row[3], "archive_msg_id": row[4],
                "file_type": row[5], "title": row[6], "artist": row[7],
                "duration": row[8], "size": row[9],
                "collection_key": row[10], "collection_kind": row[11],
                "artist_key": row[12], "position": row[13],
            }
    except Exception as e:
        print(f"[archive] lookup error: {e}")
        return None

def save_archive_record(platform, track_key, quality, archive_chat_id, archive_msg_id,
                        file_type, title=None, artist=None, duration=None, size=None,
                        collection_key=None, collection_kind=None, artist_key=None, position=None):
    """INSERT OR REPLACE into media_archive."""
    if not track_key or not archive_msg_id:
        return
    existing = get_archive_record(platform, track_key, quality)
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO media_archive "
                "(platform, track_key, quality, archive_chat_id, archive_msg_id, file_type, "
                "title, artist, duration, size, collection_key, collection_kind, artist_key, position, last_used) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (archive_platform_name(platform), str(track_key), str(quality or ""),
                 int(archive_chat_id), int(archive_msg_id), file_type or "audio",
                 title, artist, int(duration or 0) if duration else None,
                 int(size or 0) if size else None,
                 collection_key, collection_kind, artist_key,
                 int(position) if position is not None else None),
            )
            conn.commit()
        print(f"[archive] saved plat={archive_platform_name(platform)} key={track_key} q={quality} msg={archive_msg_id}")
        if (existing and existing.get("archive_msg_id")
                and int(existing["archive_msg_id"]) != int(archive_msg_id)):
            _safe_delete_archive_message(existing.get("archive_chat_id") or archive_chat_id,
                                         existing["archive_msg_id"])
    except Exception as e:
        print(f"[archive] save error: {e}")

def update_archive_last_used(platform, track_key, quality):
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE media_archive SET last_used=CURRENT_TIMESTAMP "
                "WHERE platform=? AND track_key=? AND quality=?",
                (archive_platform_name(platform), str(track_key), str(quality or "")),
            )
            conn.commit()
    except Exception as e:
        print(f"[archive] last_used error: {e}")

def delete_archive_record(platform, track_key, quality):
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "DELETE FROM media_archive WHERE platform=? AND track_key=? AND quality=?",
                (archive_platform_name(platform), str(track_key), str(quality or "")),
            )
            conn.commit()
    except Exception as e:
        print(f"[archive] delete record error: {e}")

def _save_archive_header(collection_key, quality, header_msg_id, kind=None, title=None, artist=None, artist_key=None):
    if not collection_key or not header_msg_id:
        return
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO media_archive_headers "
                "(collection_key, quality, header_msg_id, kind, title, artist, artist_key) "
                "VALUES (?,?,?,?,?,?,?)",
                (collection_key, str(quality or ""), int(header_msg_id), kind, title, artist, artist_key),
            )
            conn.commit()
    except Exception as e:
        print(f"[archive] header save error: {e}")

def _list_artist_archive_rows(artist_key, quality, exclude_collection_key=None):
    if not artist_key:
        return []
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            if exclude_collection_key:
                c.execute(
                    "SELECT platform, track_key, quality, archive_chat_id, archive_msg_id, collection_key "
                    "FROM media_archive WHERE artist_key=? AND quality=? AND IFNULL(collection_key,'') != ?",
                    (artist_key, str(quality or ""), exclude_collection_key),
                )
            else:
                c.execute(
                    "SELECT platform, track_key, quality, archive_chat_id, archive_msg_id, collection_key "
                    "FROM media_archive WHERE artist_key=? AND quality=?",
                    (artist_key, str(quality or "")),
                )
            return c.fetchall() or []
    except Exception as e:
        print(f"[archive] artist list error: {e}")
        return []

def _safe_delete_archive_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
        return True
    except Exception as e:
        print(f"[archive] delete message {message_id} failed: {e}")
        return False

def purge_artist_previous_archives(artist_key, quality, keep_collection_key=None):
    """Remove older singles/albums of an artist before writing a full discography."""
    if not artist_key:
        return
    rows = _list_artist_archive_rows(artist_key, quality, exclude_collection_key=keep_collection_key)
    seen_headers = set()
    for platform, track_key, q, chat_id, msg_id, col_key in rows:
        _safe_delete_archive_message(chat_id, msg_id)
        delete_archive_record(platform, track_key, q)
        if col_key and col_key not in seen_headers:
            seen_headers.add(col_key)
            try:
                with db_pool.get_connection() as conn:
                    c = conn.cursor()
                    c.execute(
                        "SELECT header_msg_id FROM media_archive_headers WHERE collection_key=? AND quality=?",
                        (col_key, str(quality or "")),
                    )
                    h = c.fetchone()
                    if h:
                        _safe_delete_archive_message(chat_id, h[0])
                    c.execute(
                        "DELETE FROM media_archive_headers WHERE collection_key=? AND quality=?",
                        (col_key, str(quality or "")),
                    )
                    conn.commit()
            except Exception as e:
                print(f"[archive] header purge error: {e}")
        time.sleep(0.05)
    print(f"[archive] purged {len(rows)} previous items for artist={artist_key} q={quality}")

def _copy_to_archive_channel(from_chat_id, message_id):
    """Copy one user-facing media message into the archive with archive-only caption."""
    return bot.copy_message(
        chat_id=ARCHIVE_CHANNEL_ID,
        from_chat_id=from_chat_id,
        message_id=message_id,
        caption=ARCHIVE_CAPTION,
        parse_mode="HTML",
    )

def _send_archive_header(kind, title, artist, count, url=None):
    kind_label = {
        "spotify_artist": "Discography",
        "soundcloud_artist": "Discography",
        "discography": "Discography",
        "album": "Album",
        "spotify": "Album",
        "playlist": "Playlist",
        "soundcloud": "Playlist",
    }.get((kind or "").lower(), "Collection")
    lines = [f"<b>{_html_esc(kind_label)}</b>"]
    if title:
        lines.append(_html_esc(title))
    if artist and artist != title:
        lines.append(_html_esc(artist))
    if count:
        lines.append(f"{int(count)} tracks")
    if url:
        lines.append(f'<a href="{_html_esc(url)}">Source</a>')
    lines.append("")
    lines.append(ARCHIVE_CAPTION)
    return bot.send_message(
        ARCHIVE_CHANNEL_ID,
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

def _ensure_archive_worker():
    global _ARCHIVE_WORKER_STARTED
    with _ARCHIVE_WORKER_LOCK:
        if _ARCHIVE_WORKER_STARTED:
            return
        t = threading.Thread(target=_archive_worker_loop, daemon=True, name="archive-worker")
        t.start()
        _ARCHIVE_WORKER_STARTED = True

def enqueue_archive_job(job):
    if not ENABLE_MEDIA_ARCHIVE:
        return
    _ensure_archive_worker()
    _ARCHIVE_JOB_QUEUE.put(job)

def archive_message_in_background(from_chat_id, message_id, platform, track_key, quality,
                                  file_type, title=None, artist=None, duration=None, size=None,
                                  collection_key=None, collection_kind=None, artist_key=None, position=None):
    """Queue a single-track copy into the archive channel (serialized worker)."""
    enqueue_archive_job({
        "type": "copy_single",
        "from_chat_id": from_chat_id,
        "message_id": message_id,
        "platform": platform,
        "track_key": track_key,
        "quality": quality,
        "file_type": file_type or "audio",
        "title": title,
        "artist": artist,
        "duration": duration,
        "size": size,
        "collection_key": collection_key,
        "collection_kind": collection_kind,
        "artist_key": artist_key,
        "position": position,
    })

def _archive_worker_loop():
    """Single worker: collections are posted atomically so they never mix."""
    print("[archive] worker started")
    while True:
        job = _ARCHIVE_JOB_QUEUE.get()
        try:
            jtype = (job or {}).get("type")
            if jtype == "copy_single":
                _archive_run_copy_single(job)
            elif jtype == "collection":
                _archive_run_collection(job)
            else:
                print(f"[archive] unknown job type: {jtype}")
        except Exception as e:
            print(f"[archive] worker job error: {type(e).__name__}: {e}")
        finally:
            _ARCHIVE_JOB_QUEUE.task_done()

def _archive_msg_id(copied):
    if copied is None:
        return None
    if isinstance(copied, int):
        return copied
    mid = getattr(copied, "message_id", None)
    if mid:
        return int(mid)
    if isinstance(copied, dict):
        return int(copied.get("message_id") or 0) or None
    return None

def _archive_run_copy_single(job):
    try:
        copied = _copy_to_archive_channel(job["from_chat_id"], job["message_id"])
        mid = _archive_msg_id(copied)
        if not mid:
            print(f"[archive] copy_single got no message_id: {copied!r}")
            return
        save_archive_record(
            job.get("platform"), job.get("track_key"), job.get("quality"),
            ARCHIVE_CHANNEL_ID, mid,
            job.get("file_type") or "audio",
            title=job.get("title"), artist=job.get("artist"),
            duration=job.get("duration"), size=job.get("size"),
            collection_key=job.get("collection_key"),
            collection_kind=job.get("collection_kind"),
            artist_key=job.get("artist_key"),
            position=job.get("position"),
        )
    except Exception as e:
        print(f"[archive] copy_single failed: {e}")

def _dedupe_archive_remaining(tracks):
    seen = set()
    out = []
    for t in tracks or []:
        k = t.get("id") or t.get("url") or (t.get("artist"), t.get("title"))
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out

def _archive_run_collection(job):
    kind = job.get("kind") or ""
    quality = job.get("quality") or ""
    collection_key = job.get("collection_key")
    artist_key = job.get("artist_key")
    items = job.get("items") or []
    remaining = _dedupe_archive_remaining(job.get("remaining") or [])
    header_msg = None
    try:
        header_msg = _send_archive_header(
            kind, job.get("title"), job.get("artist"),
            count=(len(items) + len(remaining)),
            url=job.get("url"),
        )
        if collection_key and header_msg:
            _save_archive_header(
                collection_key, quality, header_msg.message_id,
                kind=kind, title=job.get("title"), artist=job.get("artist"),
                artist_key=artist_key,
            )
    except Exception as e:
        print(f"[archive] header send failed (continuing): {e}")
    pos = 0
    for it in items:
        pos += 1
        try:
            copied = _copy_to_archive_channel(it["from_chat_id"], it["message_id"])
            mid = _archive_msg_id(copied)
            if not mid:
                print(f"[archive] collection copy got no message_id pos={pos}: {copied!r}")
                continue
            save_archive_record(
                it.get("platform"), it.get("track_key"), quality,
                ARCHIVE_CHANNEL_ID, mid,
                it.get("file_type") or "audio",
                title=it.get("title"), artist=it.get("artist"),
                duration=it.get("duration"), size=it.get("size"),
                collection_key=collection_key, collection_kind=kind,
                artist_key=artist_key, position=pos,
            )
        except Exception as e:
            print(f"[archive] collection copy failed pos={pos}: {e}")
        time.sleep(0.08)
    for trk in remaining:
        pos += 1
        try:
            _archive_download_and_post_remaining(job, trk, pos)
        except Exception as e:
            print(f"[archive] remaining track failed pos={pos}: {e}")
        time.sleep(0.08)
    if kind in ("spotify_artist", "soundcloud_artist", "discography") and artist_key:
        purge_artist_previous_archives(artist_key, quality, keep_collection_key=collection_key)

def _archive_download_and_post_remaining(job, trk, position):
    """Download a leftover collection track and post it only to the archive channel."""
    kind = job.get("kind") or ""
    quality = job.get("quality") or ""
    plat = "spotify" if kind in ("spotify", "spotify_artist") else "soundcloud"
    track_key = extract_track_key(plat, trk, url=trk.get("url"))
    cached = get_archive_record(plat, track_key, quality) if track_key else None
    if cached:
        copied = _copy_to_archive_channel(cached["archive_chat_id"], cached["archive_msg_id"])
        mid = _archive_msg_id(copied)
        if not mid:
            print(f"[archive] remaining copy got no message_id pos={position}")
            return
        save_archive_record(
            plat, track_key, quality, ARCHIVE_CHANNEL_ID, mid, "audio",
            title=trk.get("title") or cached.get("title"),
            artist=trk.get("artist") or cached.get("artist"),
            duration=trk.get("duration") or cached.get("duration"),
            size=cached.get("size"),
            collection_key=job.get("collection_key"), collection_kind=kind,
            artist_key=job.get("artist_key"), position=position,
        )
        return
    tmpdir = tempfile.mkdtemp(prefix="archive_remain_")
    try:
        item, platform = _download_one_batch_track(kind, trk, job.get("chat_id"), tmpdir)
        if not item or not item.get("filepath"):
            return
        plat = archive_platform_name(platform)
        track_key = extract_track_key(plat, {**trk, **item}, url=trk.get("url") or item.get("url"))
        cached = get_archive_record(plat, track_key, quality)
        if cached:
            copied = _copy_to_archive_channel(cached["archive_chat_id"], cached["archive_msg_id"])
            mid = _archive_msg_id(copied)
            if not mid:
                print(f"[archive] remaining recopy got no message_id pos={position}")
                return
            save_archive_record(
                plat, track_key, quality, ARCHIVE_CHANNEL_ID, mid, "audio",
                title=item.get("title") or cached.get("title"),
                artist=item.get("artist") or cached.get("artist"),
                duration=item.get("duration") or cached.get("duration"),
                size=item.get("size") or cached.get("size"),
                collection_key=job.get("collection_key"), collection_kind=kind,
                artist_key=job.get("artist_key"), position=position,
            )
            return
        safe_fp = item["filepath"]
        thumb = None
        kwargs = {
            "caption": ARCHIVE_CAPTION,
            "parse_mode": "HTML",
            "performer": item.get("artist"),
            "title": item.get("title"),
            "duration": item.get("duration") or None,
        }
        with open(safe_fp, "rb") as f:
            if item.get("thumb_file") and os.path.exists(item["thumb_file"]):
                try:
                    thumb = open(item["thumb_file"], "rb")
                    kwargs["thumb"] = thumb
                    msg = bot.send_audio(ARCHIVE_CHANNEL_ID, f, **kwargs)
                except Exception:
                    msg = bot.send_audio(
                        ARCHIVE_CHANNEL_ID, open(safe_fp, "rb"),
                        caption=ARCHIVE_CAPTION, parse_mode="HTML",
                        performer=item.get("artist"), title=item.get("title"),
                        duration=item.get("duration") or None,
                    )
            else:
                msg = bot.send_audio(ARCHIVE_CHANNEL_ID, f, **kwargs)
        if thumb:
            try:
                thumb.close()
            except Exception:
                pass
        save_archive_record(
            archive_platform_name(platform), track_key, quality,
            ARCHIVE_CHANNEL_ID, msg.message_id, "audio",
            title=item.get("title"), artist=item.get("artist"),
            duration=item.get("duration"), size=item.get("size"),
            collection_key=job.get("collection_key"), collection_kind=kind,
            artist_key=job.get("artist_key"), position=position,
        )
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)

def _copy_archive_to_user(chat_id, archive_chat_id, archive_msg_id, caption=None):
    """Copy archive media to the user. Caption is best-effort (never blocks a hit)."""
    kwargs = {
        "chat_id": chat_id,
        "from_chat_id": archive_chat_id,
        "message_id": archive_msg_id,
    }
    if caption:
        try:
            return bot.copy_message(caption=caption, parse_mode="HTML", **kwargs)
        except Exception as e:
            print(f"[archive] copy with caption failed: {e}; retrying without caption")
    return bot.copy_message(**kwargs)

def try_send_from_archive(chat_id, platform, track_key, quality, caption, performer=None, title=None, duration=None):
    """Copy an archived track to the user. Returns True on success."""
    if not ENABLE_MEDIA_ARCHIVE or not track_key:
        return False
    cached = get_archive_record(platform, track_key, quality)
    if not cached:
        print(f"[archive] MISS plat={archive_platform_name(platform)} key={track_key} q={quality}")
        return False
    try:
        _copy_archive_to_user(chat_id, cached["archive_chat_id"], cached["archive_msg_id"], caption)
        update_archive_last_used(platform, track_key, quality)
        add_stats_with_platform(
            chat_id,
            "Spotify" if archive_platform_name(platform) == "spotify" else "SoundCloud",
            "audio",
            cached.get("size") or 0,
        )
        print(f"[archive] HIT plat={archive_platform_name(platform)} key={track_key} q={quality}")
        return True
    except Exception as e:
        err = str(e).lower()
        print(f"[archive] copy failed, falling back to download: {e}")
        if "message to copy not found" in err or "message can't be copied" in err:
            delete_archive_record(platform, track_key, quality)
        return False

def _caption_for_archive_track(chat_id, platform, trk, cached=None, original_url=None):
    plat_display = "Spotify" if archive_platform_name(platform) == "spotify" else "SoundCloud"
    src = cached or {}
    item = {
        "artist": src.get("artist") or trk.get("artist") or "Unknown",
        "title": src.get("title") or trk.get("title") or "Unknown",
        "album": trk.get("album"),
        "track_number": trk.get("track_number"),
        "duration": src.get("duration") or trk.get("duration") or 0,
        "size": src.get("size") or trk.get("size") or 0,
        "year": trk.get("year"),
        "release_date": trk.get("release_date"),
    }
    return caption_builder.build_caption(chat_id, plat_display, item, original_url or trk.get("url"))

def _try_archive_hit_for_track(chat_id, platform, trk, quality, original_url=None):
    """Serve from archive BEFORE any download. Returns send-info dict or None."""
    if not ENABLE_MEDIA_ARCHIVE:
        return None
    plat = archive_platform_name(platform)
    track_key = extract_track_key(plat, trk, original_url or trk.get("url"))
    if not track_key:
        return None
    cached = get_archive_record(plat, track_key, quality)
    if not cached:
        print(f"[archive] MISS plat={plat} key={track_key} q={quality}")
        return None
    caption = _caption_for_archive_track(chat_id, plat, trk, cached, original_url or trk.get("url"))
    if try_send_from_archive(chat_id, plat, track_key, quality, caption,
                             performer=cached.get("artist") or trk.get("artist"),
                             title=cached.get("title") or trk.get("title"),
                             duration=cached.get("duration") or trk.get("duration")):
        return {
            "from_archive": True, "already_archived": True,
            "track_key": track_key, "platform": plat,
            "title": cached.get("title") or trk.get("title"),
            "artist": cached.get("artist") or trk.get("artist"),
            "duration": cached.get("duration") or trk.get("duration"),
            "size": cached.get("size") or 0,
        }
    return None

def enqueue_user_job(chat_id, func, *args, notify=True, **kwargs):
    """Serialize downloads per user so a long collection does not overlap new requests."""
    with _USER_QUEUE_LOCK:
        q = _USER_JOB_QUEUES.setdefault(chat_id, queue.Queue())
        busy = (chat_id in _USER_QUEUE_ACTIVE) or (q.qsize() > 0)
        q.put((func, args, kwargs))
        need_start = chat_id not in _USER_QUEUE_ACTIVE
        if need_start:
            _USER_QUEUE_ACTIVE.add(chat_id)
    if busy and notify:
        try:
            bot.send_message(chat_id, tr(chat_id, "user_queue_added"))
        except Exception:
            pass
    if need_start:
        threading.Thread(target=_user_queue_worker, args=(chat_id,), daemon=True, name=f"user-q-{chat_id}").start()

def _user_queue_worker(chat_id):
    try:
        while True:
            with _USER_QUEUE_LOCK:
                q = _USER_JOB_QUEUES.get(chat_id)
            if q is None:
                break
            try:
                func, args, kwargs = q.get(timeout=1.0)
            except queue.Empty:
                with _USER_QUEUE_LOCK:
                    if q.empty():
                        _USER_QUEUE_ACTIVE.discard(chat_id)
                        break
                continue
            try:
                func(*args, **kwargs)
            except Exception as e:
                print(f"[user-queue] job error chat={chat_id}: {type(e).__name__}: {e}")
                try:
                    bot.send_message(chat_id, tr(chat_id, "error", err=str(e)[:180]))
                except Exception:
                    pass
            finally:
                q.task_done()
    finally:
        with _USER_QUEUE_LOCK:
            q = _USER_JOB_QUEUES.get(chat_id)
            if q is None or q.empty():
                _USER_QUEUE_ACTIVE.discard(chat_id)

# ── 1.5  Batch resume state (batch_state_save/get_running/get_all_running/mark/clear) ────
# ===== Batch resume state =====
def batch_state_save(chat_id, kind, panel_id, title, artist, cover, url, total, tracks, done_indices, failed, status="running"):
    """One row per chat (active batch): UPDATE-or-INSERT."""
    try:
        tracks_json = json.dumps(tracks, ensure_ascii=False)
        done_json = json.dumps(list(done_indices))
        failed_json = json.dumps(failed, ensure_ascii=False)
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE batch_state SET kind=?, panel_id=?, title=?, artist=?, cover=?, url=?, "
                "total=?, done_json=?, failed_json=?, tracks_json=?, status=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=(SELECT id FROM batch_state WHERE chat_id=? AND status IN ('running','paused') ORDER BY id DESC LIMIT 1)",
                (kind, panel_id or "", title or "", artist or "", cover or "", url or "", total,
                 done_json, failed_json, tracks_json, status, chat_id)
            )
            if c.rowcount == 0:
                c.execute(
                    "INSERT INTO batch_state (chat_id, kind, panel_id, title, artist, cover, url, total, done_json, failed_json, tracks_json, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (chat_id, kind, panel_id or "", title or "", artist or "", cover or "", url or "", total,
                     done_json, failed_json, tracks_json, status)
                )
            conn.commit()
    except Exception as e:
        print(f"[batch_state] save error: {e}")
def batch_state_get_running(chat_id):
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id, kind, panel_id, title, artist, cover, url, total, done_json, failed_json, tracks_json, status "
                "FROM batch_state WHERE chat_id=? AND status IN ('running','paused') ORDER BY id DESC LIMIT 1", (chat_id,))
            row = c.fetchone()
            if not row:
                return None
            return {"id": row[0], "kind": row[1], "panel_id": row[2], "title": row[3], "artist": row[4],
                    "cover": row[5], "url": row[6], "total": row[7],
                    "done": json.loads(row[8] or "[]"), "failed": json.loads(row[9] or "[]"),
                    "tracks": json.loads(row[10] or "[]"), "status": row[11]}
    except Exception as e:
        print(f"[batch_state] get error: {e}")
        return None
def batch_state_get_all_running():
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, chat_id, kind, panel_id, title, artist, cover, url, total, done_json, failed_json, tracks_json, status FROM batch_state WHERE status='running' ORDER BY id DESC")
            rows = c.fetchall()
            out = []
            for r in rows:
                out.append({"id": r[0], "chat_id": r[1], "kind": r[2], "panel_id": r[3], "title": r[4],
                            "artist": r[5], "cover": r[6], "url": r[7], "total": r[8],
                            "done": json.loads(r[9] or "[]"), "failed": json.loads(r[10] or "[]"),
                            "tracks": json.loads(r[11] or "[]"), "status": r[12]})
            return out
    except Exception as e:
        print(f"[batch_state] get_all error: {e}")
        return []
def batch_state_mark(chat_id, status):
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE batch_state SET status=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=(SELECT id FROM batch_state WHERE chat_id=? AND status IN ('running','paused') ORDER BY id DESC LIMIT 1)",
                (status, chat_id))
            conn.commit()
    except Exception as e:
        print(f"[batch_state] mark error: {e}")
def batch_state_clear(chat_id):
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM batch_state WHERE chat_id=? AND status IN ('done','cancelled','failed')", (chat_id,))
            conn.commit()
    except Exception:
        pass

# ── 1.6  Panels (new_panel_id, panel_save/get/cleanup) ──────────────────
# ===== Panels (per-panel context) =====
def new_panel_id():
    return _uuid.uuid4().hex[:12]
def panel_save(chat_id, kind, tracks, meta=None):
    """Store a panel's tracks + meta under a fresh panel_id. Returns panel_id."""
    panel_id = new_panel_id()
    try:
        tracks_json = json.dumps(tracks, ensure_ascii=False)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO panels (chat_id, panel_id, kind, tracks_json, meta_json) VALUES (?,?,?,?,?)",
                (chat_id, panel_id, kind, tracks_json, meta_json)
            )
            conn.commit()
    except Exception as e:
        print(f"[panel] save error: {e}")
    return panel_id
def panel_get(chat_id, panel_id):
    """Return (kind, tracks, meta) for a panel_id, or (None, [], {})."""
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT kind, tracks_json, meta_json FROM panels WHERE chat_id=? AND panel_id=?", (chat_id, panel_id))
            row = c.fetchone()
            if not row:
                return (None, [], {})
            return (row[0], json.loads(row[1] or "[]"), json.loads(row[2] or "{}"))
    except Exception as e:
        print(f"[panel] get error: {e}")
        return (None, [], {})
def panel_cleanup(chat_id, keep=8):
    """Keep only the most recent `keep` panels for a chat."""
    try:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT panel_id FROM panels WHERE chat_id=? ORDER BY created_at DESC", (chat_id,))
            rows = c.fetchall()
            if len(rows) > keep:
                old = [r[0] for r in rows[keep:]]
                for pid in old:
                    c.execute("DELETE FROM panels WHERE chat_id=? AND panel_id=?", (chat_id, pid))
            conn.commit()
    except Exception:
        pass

# ── 1.7  Spotify choice caches + batch meta ─────────────────────────────
# ===== Spotify + Batch cache functions =====
def save_spotify_choices(chat_id, tracks):
    """Save Spotify album/playlist tracks for selection + batch."""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM spotify_cache WHERE chat_id=?", (chat_id,))
        for idx, t in enumerate(tracks):
            c.execute(
                "INSERT INTO spotify_cache (chat_id, idx, track_id, title, artist, album, duration, cover, track_number, url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (chat_id, idx, t.get("id") or "", t.get("title", "Unknown"), t.get("artist", "Unknown"),
                 t.get("album") or "", int(t.get("duration") or 0), t.get("cover") or "",
                 t.get("track_number") or 0, t.get("url") or "")
            )
        conn.commit()
def get_spotify_choice(chat_id, idx):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT track_id, title, artist, album, duration, cover, track_number, url FROM spotify_cache WHERE chat_id=? AND idx=?", (chat_id, idx))
        row = c.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "title": row[1], "artist": row[2], "album": row[3],
            "duration": row[4], "cover": row[5], "track_number": row[6], "url": row[7],
        }
def get_all_spotify_choices(chat_id):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT idx, track_id, title, artist, album, duration, cover, track_number, url FROM spotify_cache WHERE chat_id=? ORDER BY idx", (chat_id,))
        rows = c.fetchall()
        out = []
        for r in rows:
            out.append({
                "idx": r[0], "id": r[1], "title": r[2], "artist": r[3], "album": r[4],
                "duration": r[5], "cover": r[6], "track_number": r[7], "url": r[8],
            })
        return out
def clear_spotify_cache(chat_id):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM spotify_cache WHERE chat_id=?", (chat_id,))
        conn.commit()
def save_batch_meta(chat_id, kind, title, artist, cover, url, count):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO batch_meta_cache (chat_id, kind, title, artist, cover, url, count) VALUES (?,?,?,?,?,?,?)",
            (chat_id, kind, title or "", artist or "", cover or "", url or "", int(count or 0))
        )
        conn.commit()
def get_batch_meta(chat_id):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT kind, title, artist, cover, url, count FROM batch_meta_cache WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if not row:
            return None
        return {"kind": row[0], "title": row[1], "artist": row[2], "cover": row[3], "url": row[4], "count": row[5]}
def clear_batch_meta(chat_id):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM batch_meta_cache WHERE chat_id=?", (chat_id,))
        conn.commit()

# ── 1.8  SoundCloud choice caches (search_cache, playlist_cache) ────────
def save_search_choices(chat_id, choices):
    """Save search choices with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM search_cache WHERE chat_id=?", (chat_id,))
        for idx, ch in enumerate(choices):
            c.execute("INSERT INTO search_cache (chat_id, idx, url, title, artist, duration) VALUES (?, ?, ?, ?, ?, ?)", (chat_id, idx, ch["url"], ch["title"], ch["artist"], ch.get("duration", 0)))
        conn.commit()
def save_playlist_choices(chat_id, choices):
    """Save playlist songs for selection with connection pooling"""
    if not choices:
        return

    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM playlist_cache WHERE chat_id=?", (chat_id,))
        for idx, ch in enumerate(choices):
            title = ch.get("title", "Unknown Title")
            artist = ch.get("artist", "Unknown Artist")
            url = ch.get("url", "")
            duration = ch.get("duration", 0)

            c.execute("INSERT INTO playlist_cache (chat_id, idx, url, title, artist, duration) VALUES (?, ?, ?, ?, ?, ?)", (chat_id, idx, url, title, artist, duration))
        conn.commit()
def get_search_choice(chat_id, idx):
    """Get search choice with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT url, title, artist, duration FROM search_cache WHERE chat_id=? AND idx=?", (chat_id, idx))
        row = c.fetchone()
        if not row:
            return None
        return {"url": row[0], "title": row[1], "artist": row[2], "duration": row[3]}
def get_playlist_choice(chat_id, idx):
    """Get selected song from playlist with connection pooling"""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT url, title, artist, duration FROM playlist_cache WHERE chat_id=? AND idx=?", (chat_id, idx))
        row = c.fetchone()
        if not row:
            return None
        return {"url": row[0], "title": row[1], "artist": row[2], "duration": row[3]}
def get_all_playlist_choices(chat_id):
    """Get all cached playlist tracks (for SoundCloud batch download)."""
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT idx, url, title, artist, duration FROM playlist_cache WHERE chat_id=? ORDER BY idx", (chat_id,))
        rows = c.fetchall()
        return [{"idx": r[0], "url": r[1], "title": r[2], "artist": r[3], "duration": r[4]} for r in rows]


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 2 — i18n & UI HELPERS                                        ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 2.1  i18n dicts (T = {fa, en}, LANGS, tr()) ─────────────────────────
# ===== Enhanced i18n =====
LANGS = {"fa", "en"}
T = {
    "fa": {
        "start": "🌐 به ربات دانلودر چند پلتفرمی خوش آمدید!\n\nاین ربات قابلیت دانلود از پلتفرم‌های مختلف را با بهترین کیفیت ممکن فراهم می‌کند. برای استفاده از ربات، لطفاً عضو کانال شوید.",
        "fa_btn": "فارسی 🇮🇷",
        "en_btn": "English 🇬🇧",
        "lang_set": "زبان تنظیم شد: {lang}",
        "send_link": "🔗 لینک پلتفرم‌های زیر رو بفرست تا برات دانلود کنم:\n\n🎵 SoundCloud  •  🟢 Spotify  •  📷 Pinterest  •  📸 Instagram\n🎬 YouTube & Shorts  •  🎵 TikTok  •  🐦 Twitter/X\n\nیا از /search برای جستجوی آهنگ در ساندکلاد استفاده کن.",
        "quality_prompt": "کیفیت صوتی SoundCloud را انتخاب کن:",
        "quality_high": "کیفیت بالا 🎧",
        "quality_low": "کیفیت سبک 🔉",
        "quality_set": "کیفیت تنظیم شد: {q}",
        "downloading": "در حال دانلود... ⏳",
        "progress": "در حال دانلود... {pct}% ({done}/{total})",
        "invalid_link": "لطفاً لینک معتبر بده یا از /search استفاده کن.",
        "error": "❗️خطا: {err}",
        "stats_title": "آمار دانلود",
        "stats_body": "کاربر: {user_count} مورد، {user_bytes}\nکل ربات: {total_count} مورد، {total_bytes}",
        "search_prompt": "برای جستجو بنویس: /search کلمه‌کلیدی",
        "searching": "در حال جستجو در SoundCloud... 🔎",
        "searching_spotify": "در حال جستجو در اسپاتیفای... 🔎",
        "searching_with_count": "در حال جستجو در SoundCloud... 🔎 ({count} نتیجه یافت شد)",
        "search_results_found": "✅ {count} نتیجه پیدا شد",
        "no_results_found": "نتیجه‌ای پیدا نشد",
        "search_complete": "جستجو کامل شد - {count} نتیجه",
        "processing_results": "در حال پردازش نتایج...",
        "loading_results": "در حال بارگذاری نتایج...",
        "pick_from_results": "از نتایج زیر انتخاب کنید:",
        "previous_page": "⬅️ قبلی",
        "next_page": "بعدی ➡️",
        "page_number": "📄 {page}/{total_pages}",
        "playlist_song_selection": "🎵 انتخاب آهنگ از پلی‌لیست:",
        "downloading_playlist": "در حال دانلود پلی‌لیست...",
        "processing_playlist": "در حال پردازش آهنگ‌های پلی‌لیست...",
        "playlist_detected": "پلی‌لیست شناسایی شد. {count} آهنگ یافت شد",
        "select_song": "انتخاب آهنگ",
        "song_number": "آهنگ {num}",
        "downloading_single": "در حال دانلود تک آهنگ...",
        "preview": "پیش‌نمایش",
        "video_preview": "پیش‌نمایش ویدیو",
        "tiktok_preview": "پیش‌نمایش TikTok",
        "instagram_preview": "پیش‌نمایش اینستاگرام",
        "youtube_preview": "پیش‌نمایش یوتیوب",
        "pinterest_preview": "پیش‌نمایش پینترست",
        "twitter_preview": "پیش‌نمایش توییتر",
        "search_none": "نتیجه‌ای پیدا نشد.",
        "search_pick": "یکی را انتخاب کن:",
        "playlist_note": "پلی‌لیست شناسایی شد. در حال ارسال ترک‌ها... 📂",
        "cover_sent": "اینم از کاور🖼️",
        "must_join": "برای استفاده از ربات، لطفاً عضو کانال {chan} شو.",
        "must_join_start": "برای استفاده از ربات، لطفاً عضو کانال {chan} شوید.\n\nبعد از عضویت روی /start بزنید:",
        "join_btn": "عضویت در کانال",
        "join_came_btn": "بشم، اومدم",
        "signature": "دانلود شده با 💝",
        "features_header": "🌟 قابلیت‌های ربات:",
        "features_lines": [
            "<blockquote>🎵 <b>پلتفرم‌های صوتی:</b></blockquote>",
            "<blockquote expandable>  • <b>SoundCloud:</b> ترک تکی و پلی‌لیست، جستجو با /search، انتخاب کیفیت صوتی، ارسال کاور و اطلاعات آهنگ\n  • <b>Spotify:</b> دانلود ترک، آلبوم و پلی‌لیست با کاور و متادیتای کامل</blockquote>",
            "<blockquote>🎬 <b>پلتفرم‌های ویدیویی:</b></blockquote>",
            "<blockquote expandable>  • <b>YouTube:</b> دانلود ویدیوهای عادی و شورتس با انتخاب کیفیت و گزینه صرفاً صدا\n  • <b>TikTok:</b> دانلود ویدیوهای تیک‌تاک بدون واترمارک و اطلاعات کامل\n  • <b>Instagram:</b> دانلود عکس، ویدیو و ریلز با بالاترین کیفیت و کپشن کامل\n  • <b>Pinterest:</b> دانلود عکس و ویدیو با کپشن و بالاترین کیفیت\n  • <b>Twitter (X):</b> دانلود توییت‌ها، ویدیوها و تصاویر با بالاترین کیفیت</blockquote>",
            "<blockquote>✨ <b>امکانات ویژه:</b></blockquote>",
            "<blockquote expandable>  • <b>دانلود گروهی:</b> برای آلبوم/پلی‌لیست (ساندکلاد و اسپاتیفای) کاور یک‌بار ارسال میشه و همه ترک‌ها با پیشرفت زنده دانلود میشن\n  • <b>نمایش پیشرفت:</b> نمایش درصد دانلود به‌صورت زنده\n  • <b>آمار:</b> آمار کاربر و کل ربات با /stats (روزانه/هفتگی/کلی)\n  • <b>دکمه‌های رنگی:</b> دکمه‌های رنگی و کاربرپسند در همه بخش‌های ربات\n  • <b>بخش تنظیمات:</b> کیفیت هر پلتفرم و زبان قابل تنظیم با /settings\n  • <b>دانلود پایدار:</b> موتور yt-dlp با پشتیبانی deno برای دانلود مطمئن از یوتیوب</blockquote>",
            "<blockquote>✨ <b>خوشحال میشم که عضو خونواده‌ی ما بشی!</b></blockquote>"
        ],
        "companion_label": "🤝 همراه شما: {id}",
        "close_menu": "❌ بستن منو",
        "content_link": "لینک محتوا",
        "top_users_all_time": "برترین کاربران (همه زمان)",
        "top_platforms_all_time": "پلتفرم‌های برتر (همه زمان)",
        "top_users_daily": "برترین کاربران (امروز)",
        "top_platforms_daily": "پلتفرم‌های برتر (امروز)",
        "top_users_weekly": "برترین کاربران (هفته)",
        "top_platforms_weekly": "پلتفرم‌های برتر (هفته)",
        "view_profile": "مشاهده پروفایل",
        "back_to_stats": "بازگشت به آمار",
        "no_data": "داده‌ای برای نمایش وجود ندارد",
        "rank": "رتبه",
        "user": "کاربر",
        "downloads": "دانلودها",
        "volume": "حجم",
        "platform": "پلتفرم",
        "most_used": "پراستفاده‌ترین",
        "your_stats": "آمار شما",
        "total_processed": "کل پردازش‌ها",
        "uptime": "آپتایم",
        "no_user_data": "شما هنوز فعالیتی نداشته‌اید",
        "top_user_stats": "👑 برترین کاربران (همه زمان)",
        "daily_top_user_stats": "📅 برترین کاربران (امروز)",
        "weekly_top_user_stats": "📆 برترین کاربران (هفته)",
        "top_platform_stats": "🏆 پلتفرم‌های برتر (همه زمان)",
        "daily_top_platform_stats": "📊 پلتفرم‌های برتر (امروز)",
        "weekly_top_platform_stats": "📈 پلتفرم‌های برتر (هفته)",
        "your_daily_stats": "آمار روزانه شما",
        "your_weekly_stats": "آمار هفتگی شما",
        "choose_category": "دسته مورد نظر را انتخاب کنید:",
        "global_stats": "آمار کل ربات",
        # (proxy i18n strings removed — proxy capability was removed from the bot)
        # New translations for YouTube quality selection
        "youtube_quality_prompt": "🎬 کیفیت ویدیو را انتخاب کنید:",
        "youtube_audio_only": "فقط صدا",
        "youtube_video_quality": "🎬 {quality}",
        "youtube_size_info": "{size} مگابایت",
        "youtube_processing": "در حال پردازش کیفیت‌های ممکن...",
        "youtube_no_qualities": "هیچ کیفیت مناسب زیر ۵۰ مگابایت یافت نشد",
        "youtube_selected_quality": "✅ کیفیت انتخاب شد: {quality}",
        "youtube_downloading": "در حال دانلود با کیفیت {quality}...",
        # YouTube Shorts specific
        "youtube_shorts_detected": "🎬 این یه ویدیوی کوتاه (Short) از یوتیوبه!",
        "youtube_shorts_prompt": "نوع دانلود رو انتخاب کن:",
        "youtube_shorts_video": "📹 ویدیو",
        "youtube_shorts_audio": "🎵 فقط صدا",
        "youtube_shorts_downloading": "⏳ در حال دانلود ویدیوی کوتاه...",
        # Spotify
        "spotify_disabled": "❗️ ماژول اسپاتیفای در دسترس نیست (spotapi نصب نیست).",
        "spotify_invalid": "❗️ لینک اسپاتیفای معتبر نیست. لینک ترک، آلبوم یا پلی‌لیست بفرست.",
        "spotify_fetching_track": "🔎 در حال گرفتن اطلاعات ترک از اسپاتیفای...",
        "spotify_fetching_album": "🔎 در حال گرفتن اطلاعات آلبوم از اسپاتیفای...",
        "spotify_fetching_playlist": "🔎 در حال گرفتن اطلاعات پلی‌لیست از اسپاتیفای...",
        "spotify_track_downloading": "⏳ در حال دانلود ترک...",
        "spotify_searching_audio": "🎵 در حال آماده‌سازی صدا...",
        "spotify_single_done": "✅ ترک آماده شد",
        "track_done": "✅ ترک آماده شد",
        "spotify_album_found": "💿 آلبوم پیدا شد: <b>{name}</b>\n👤 {artist}\n🎵 {count} ترک",
        "spotify_playlist_found": "📂 پلی‌لیست پیدا شد: <b>{name}</b>\n👤 {owner}\n🎵 {count} ترک",
        "spotify_select_track": "🎵 یک ترک انتخاب کن یا همه رو دانلود کن:",
        "spotify_pick": "انتخاب ترک",
        "spotify_no_tracks": "❗️ هیچ ترکی پیدا نشد.",
        "spotify_audio_failed": "❗️ صدا برای این ترک پیدا نشد.",
        # Batch (Download All) - works for SoundCloud & Spotify
        "download_all": "⬇️ دانلود همه ({count})",
        "batch_cover_sent": "🖼️ کاور {kind}\n<b>{title}</b>\n🎵 {count} ترک\n🔗 <a href=\"{url}\">مشاهده در {kind}</a>",
        "batch_starting": "⏳ شروع دانلود گروهی {count} ترک...",
        "batch_progress": "⏬ دانلود گروهی\n✅ {done}/{total} ارسال شد\n🎵 در حال: {current}",
        "batch_track_sent": "📨 {i}/{total}: {artist} - {title}",
        "batch_failed_track": "⚠️ خطا در ترک {i}/{total}: {title}\n{err}",
        "batch_done": "✅ دانلود گروهی کامل شد!\n📦 {done}/{total} ترک ارسال شد.",
        "batch_no_data": "❗️ داده‌ای برای دانلود گروهی پیدا نشد. دوباره لینک رو بفرست.",
        # Settings menu
        "settings_title": "⚙️ تنظیمات",
        "settings_intro": "یکی از بخش‌ها رو برای تغییر انتخاب کن:",
        "settings_language": "🌐 زبان",
        "settings_sc_quality": "🎵 کیفیت SoundCloud",
        "settings_spotify_quality": "🟢 کیفیت Spotify",
        "settings_ig_quality": "📸 کیفیت Instagram",
        "settings_tt_quality": "🎵 کیفیت TikTok",
        "settings_pin_quality": "📷 کیفیت Pinterest",
        "settings_shorts_quality": "🎬 کیفیت YouTube Shorts",
        "settings_back": "🔙 بازگشت",
        "settings_current": "کنونی: {value}",
        "settings_quality_prompt": "کیفیت {platform} رو انتخاب کن:",
        "settings_quality_set": "✅ کیفیت {platform} تنظیم شد: {value}",
        "settings_lang_prompt": "زبان خودت رو انتخاب کن:",
        # Main menu
        "menu_title": "📋 منوی اصلی",
        "menu_send_link": "🔗 لینک محتوا رو بفرست تا دانلود کنم",
        "menu_settings": "⚙️ تنظیمات",
        "menu_features": "🌟 قابلیت‌ها",
        "menu_stats": "📊 آمار",
        "menu_search": "🔍 جستجوی آهنگ",
        "search_engine_title": "🔍 موتور جستجو",
        "search_engine_prompt": "موتور جستجو رو انتخاب کن:",
        "search_engine_spotify": "🟢 Spotify (پیش‌فرض)",
        "search_engine_soundcloud": "🎵 SoundCloud",
        "search_engine_set": "✅ موتور جستجو تنظیم شد: {engine}",
        "dedupe_toggle_label": "🔁 رد کردن ترک‌های تکراری (آلبوم/پلی‌لیست/دیسکوگرافی)",
        "dedupe_set": "✅ تنظیم شد: {state}",
        "redl_btn": "⬇️ دانلود مجدد از منبع",
        "redl_downloading": "⬇️ در حال دانلود مجدد: {title}",
        "search_prompt_spotify": "🔍 برای جستجو در Spotify بنویس:\nنام آهنگ یا آرتیست",
        "search_no_query": "❗️ عبارت جستجو خالیه. بعد از /search کلمه‌کلیدی بنویس.",
        "search_engine_current": "کنونی: {engine}",
        "search_no_results_both": "❗️ نتیجه‌ای در Spotify و SoundCloud پیدا نشد.",
        "search_try_other": "💡 میتونی موتور جستجو رو عوض کنی:",
        "menu_main": "🏠 منوی اصلی",
        "menu_back": "🔙 بازگشت",
        # Album / playlist keyboard
        "album_track_count": "📊 تعداد ترک‌ها: {count}",
        "album_download_all": "⬇️ دانلود همه",
        "album_select_track": "🎵 انتخاب ترک",
        "album_cancel": "❌ لغو",
        # Artist discography
        "artist_found": "🎤 آرتیست پیدا شد: <b>{name}</b>\n🎵 {count} اثر",
        "artist_no_tracks": "❗️ هیچ اثری برای این آرتیست پیدا نشد.",
        "artist_fetch_error": "❗️ خطا در گرفتن آثار آرتیست: {err}",
        "artist_fetching": "🔎 در حال گرفتن آثار آرتیست... این ممکنه چند ثانیه طول بکشه.",
        "artist_fetching_progress": "🔎 در حال گرفتن آثار آرتیست... ({done}/{total} آلبوم)",
        "artist_track_count": "📊 تعداد آثار: {count}",
        "artist_download_all": "⬇️ دانلود همه ({count})",
        "artist_download_first": "⬇️ دانلود {count} ترک اول",
        "artist_custom_count": "🔢 تعداد دلخواه",
        "artist_custom_prompt": "تعداد ترک‌هایی که میخوای دانلود کنی رو بفرست (۱ تا {max}):",
        "artist_custom_invalid": "❗️ عدد معتبر نیست. بین ۱ و {max} بفرست.",
        "artist_select_track": "🎵 انتخاب ترک",
        "artist_cancel": "❌ لغو",
        "artist_large_notice": "⚠️ دیسکوگرافی بزرگ! میتونی همه رو دانلود کنی یا تعداد دلخواه انتخاب کنی.",
        # Batch cancel
        "batch_cancel": "❌ لغو دانلود",
        "batch_cancelled": "⏹️ دانلود لغو شد\n📦 {done}/{total} ارسال شد{errors}",
        "batch_cancelled_short": "⏹️ لغو شد",
        "batch_progress_cancelable": "📦 دانلود گروهی\n{bar} {pct}% [{done}/{total}]\n🎵 در حال: {current}{failed}",
        "batch_failed_list": "\n\n⚠️ ناموفق‌ها ({count}):\n{list}",
        "batch_failed_more": "\n... و {count} مورد دیگر",
        # Common UI strings
        "ui_ok": "✅",
        "ui_invalid": "❗️ نامعتبر",
        "ui_invalid_page": "❗️ صفحه نامعتبر",
        "ui_invalid_selection": "❗️ انتخاب نامعتبر",
        "ui_url_not_found": "❗️ لینک پیدا نشد",
        "ui_choice_expired": "❗️ انتخاب منقضی شده. دوباره تلاش کن.",
        # Error messages (localized)
        "err_track_not_found": "ترک پیدا نشد",
        "err_no_quality": "کیفیت مناسبی پیدا نشد",
        "err_process_failed": "پردازش فایل ناموفق بود",
        "err_file_too_large": "فایل خیلی بزرگه: {size}",
        "err_unsupported_link": "لینک پشتیبانی نمی‌شه",
        "err_drm_fallback": "محافظت DRM، fallback ناموفق بود",
        "err_download_failed": "دانلود ناموفق بود",
        "err_send_failed": "ارسال فایل ناموفق بود: {err}",
        "drm_fallback_msg": "🔄 در حال تلاش برای دانلود از منبع جایگزین...",
        "search_ready_spotify": "✅ موتور جستجو: Spotify\n\nحالا کافیه اسم آهنگ یا آرتیست رو بفرستی تا برات بگردم.",
        "search_ready_soundcloud": "✅ موتور جستجو: SoundCloud\n\nحالا کافیه اسم آهنگ یا آرتیست رو بفرستی تا برات بگردم.",
        "user_queue_added": "📥 درخواستت رفت توی صف شخصی‌ت. بعد از تموم شدن دانلود فعلی برات انجام می‌شه.",
        "user_queue_busy": "⏳ یک دانلود مجموعه در جریانه. درخواست جدید صف شد.",
    },
    "en": {
        "start": "🌐 Welcome to the Multi-Platform Downloader Bot!\n\nThis bot provides downloading capabilities from various platforms with the best possible quality. Please join the channel to use the bot.",
        "fa_btn": "فارسی 🇮🇷",
        "en_btn": "English 🇬🇧",
        "lang_set": "Language set: {lang}",
        "send_link": "🔗 Send a link from any supported platform and I'll download it for you:\n\n🎵 SoundCloud  •  🟢 Spotify  •  📷 Pinterest  •  📸 Instagram\n🎬 YouTube & Shorts  •  🎵 TikTok  •  🐦 Twitter/X\n\nOr use /search to search songs on SoundCloud.",
        "quality_prompt": "Choose SoundCloud audio quality:",
        "quality_high": "High quality 🎧",
        "quality_low": "Light quality 🔉",
        "quality_set": "Quality set: {q}",
        "downloading": "Downloading... ⏳",
        "progress": "Downloading... {pct}% ({done}/{total})",
        "invalid_link": "Please send a valid link or use /search.",
        "error": "❗️Error: {err}",
        "stats_title": "Download stats",
        "stats_body": "You: {user_count} items, {user_bytes}\nGlobal: {total_count} items, {total_bytes}",
        "search_prompt": "To search, type: /search keyword",
        "searching": "Searching SoundCloud... 🔎",
        "searching_spotify": "Searching Spotify... 🔎",
        "searching_with_count": "Searching SoundCloud... 🔎 ({count} results found)",
        "search_results_found": "✅ {count} results found",
        "no_results_found": "No results found",
        "search_complete": "Search complete - {count} results",
        "processing_results": "Processing results...",
        "loading_results": "Loading results...",
        "pick_from_results": "Pick from the results below:",
        "previous_page": "⬅️ Previous",
        "next_page": "Next ➡️",
        "page_number": "📄 {page}/{total_pages}",
        "playlist_song_selection": "🎵 Select song from playlist:",
        "downloading_playlist": "Downloading playlist...",
        "processing_playlist": "Processing playlist songs...",
        "playlist_detected": "Playlist detected. {count} songs found",
        "select_song": "Select song",
        "song_number": "Song {num}",
        "downloading_single": "Downloading single track...",
        "preview": "Preview",
        "video_preview": "Video Preview",
        "tiktok_preview": "TikTok Preview",
        "instagram_preview": "Instagram Preview",
        "youtube_preview": "YouTube Preview",
        "pinterest_preview": "Pinterest Preview",
        "twitter_preview": "Twitter Preview",
        "search_none": "No results found.",
        "search_pick": "Pick one:",
        "playlist_note": "Playlist detected. Sending tracks... 📂",
        "cover_sent": "Cover art sent 🖼️",
        "must_join": "To use the bot, please join {chan}.",
        "must_join_start": "To use the bot, please join {chan}.\n\nAfter joining, press /start:",
        "join_btn": "Join channel",
        "join_came_btn": "I'll join, I'm in",
        "signature": "Downloaded With 💝",
        "features_header": "🌟 Bot Features:",
        "features_lines": [
            "<blockquote>🎵 <b>Audio Platforms:</b></blockquote>",
            "<blockquote expandable>  • <b>SoundCloud:</b> Single tracks & playlists, search via /search, audio quality selection, cover + metadata\n  • <b>Spotify:</b> Download tracks, albums and playlists with full cover and metadata</blockquote>",
            "<blockquote>🎬 <b>Video Platforms:</b></blockquote>",
            "<blockquote expandable>  • <b>YouTube:</b> Regular videos and Shorts with quality selection and audio-only option\n  • <b>TikTok:</b> Download TikTok videos without watermark with complete info\n  • <b>Instagram:</b> Download photos, videos and reels with highest quality and full captions\n  • <b>Pinterest:</b> Download images and videos with captions in highest quality\n  • <b>Twitter (X):</b> Download tweets, videos and images in highest quality</blockquote>",
            "<blockquote>✨ <b>Special Features:</b></blockquote>",
            "<blockquote expandable>  • <b>Batch Download:</b> For albums/playlists (SoundCloud & Spotify) the cover is sent once and all tracks are downloaded with live progress\n  • <b>Progress Display:</b> Live download percentage display\n  • <b>Statistics:</b> User and global stats via /stats (daily/weekly/all-time)\n  • <b>Colored Buttons:</b> Friendly colored buttons across the whole bot\n  • <b>Settings Menu:</b> Per-platform quality and language via /settings\n  • <b>Stable Downloads:</b> yt-dlp engine with deno support for reliable YouTube downloads</blockquote>",
            "<blockquote>✨ <b>I'll Be Happy To Have You In Our Family!</b></blockquote>"
        ],
        "companion_label": "🤝 Your companion: {id}",
        "close_menu": "❌ Close Menu",
        "content_link": "Content Link",
        "top_users_all_time": "Top Users (All Time)",
        "top_platforms_all_time": "Top Platforms (All Time)",
        "top_users_daily": "Top Users (Daily)",
        "top_platforms_daily": "Top Platforms (Daily)",
        "top_users_weekly": "Top Users (Weekly)",
        "top_platforms_weekly": "Top Platforms (Weekly)",
        "view_profile": "View Profile",
        "back_to_stats": "Back to Stats",
        "no_data": "No data available",
        "rank": "Rank",
        "user": "User",
        "downloads": "Downloads",
        "volume": "Volume",
        "platform": "Platform",
        "most_used": "Most Used",
        "your_stats": "Your Stats",
        "total_processed": "Total Processed",
        "uptime": "Uptime",
        "no_user_data": "You have no activity yet",
        "top_user_stats": "👑 Top Users (All Time)",
        "daily_top_user_stats": "📅 Top Users (Daily)",
        "weekly_top_user_stats": "📆 Top Users (Weekly)",
        "top_platform_stats": "🏆 Top Platforms (All Time)",
        "daily_top_platform_stats": "📊 Top Platforms (Daily)",
        "weekly_top_platform_stats": "📈 Top Platforms (Weekly)",
        "your_daily_stats": "Your Daily Stats",
        "your_weekly_stats": "Your Weekly Stats",
        "choose_category": "Choose a category:",
        "global_stats": "Global Bot Stats",
        # (proxy i18n strings removed — proxy capability was removed from the bot)
        # New translations for YouTube quality selection
        "youtube_quality_prompt": "🎬 Choose video quality:",
        "youtube_audio_only": "Audio Only",
        "youtube_video_quality": "🎬 {quality}",
        "youtube_size_info": "{size} MB",
        "youtube_processing": "Processing available qualities...",
        "youtube_no_qualities": "No suitable qualities under 50MB found",
        "youtube_selected_quality": "✅ Quality selected: {quality}",
        "youtube_downloading": "Downloading with {quality} quality...",
        # YouTube Shorts specific
        "youtube_shorts_detected": "🎬 YouTube Short detected!",
        "youtube_shorts_prompt": "Choose download option:",
        "youtube_shorts_video": "📹 Video",
        "youtube_shorts_audio": "🎵 Audio only",
        "youtube_shorts_downloading": "Downloading YouTube Short...",
        # Spotify
        "spotify_disabled": "❗️ Spotify module unavailable (spotapi not installed).",
        "spotify_invalid": "❗️ Invalid Spotify link. Send a track, album or playlist link.",
        "spotify_fetching_track": "🔎 Fetching track info from Spotify...",
        "spotify_fetching_album": "🔎 Fetching album info from Spotify...",
        "spotify_fetching_playlist": "🔎 Fetching playlist info from Spotify...",
        "spotify_track_downloading": "⏳ Downloading track...",
        "spotify_searching_audio": "🎵 Preparing audio...",
        "spotify_single_done": "✅ Track ready",
        "track_done": "✅ Track ready",
        "spotify_album_found": "💿 Album found: <b>{name}</b>\n👤 {artist}\n🎵 {count} tracks",
        "spotify_playlist_found": "📂 Playlist found: <b>{name}</b>\n👤 {owner}\n🎵 {count} tracks",
        "spotify_select_track": "🎵 Pick a track or download all:",
        "spotify_pick": "Pick track",
        "spotify_no_tracks": "❗️ No tracks found.",
        "spotify_audio_failed": "❗️ Audio for this track could not be found.",
        # Batch (Download All) - works for SoundCloud & Spotify
        "download_all": "⬇️ Download All ({count})",
        "batch_cover_sent": "🖼️ {kind} cover\n<b>{title}</b>\n🎵 {count} tracks\n🔗 <a href=\"{url}\">View on {kind}</a>",
        "batch_starting": "⏳ Starting batch download of {count} tracks...",
        "batch_progress": "⬬ Batch download\n✅ {done}/{total} sent\n🎵 Now: {current}",
        "batch_track_sent": "📨 {i}/{total}: {artist} - {title}",
        "batch_failed_track": "⚠️ Failed track {i}/{total}: {title}\n{err}",
        "batch_done": "✅ Batch download complete!\n📦 {done}/{total} tracks sent.",
        "batch_no_data": "❗️ No batch data found. Please resend the link.",
        # Settings menu
        "settings_title": "⚙️ Settings",
        "settings_intro": "Choose a section to change:",
        "settings_language": "🌐 Language",
        "settings_sc_quality": "🎵 SoundCloud quality",
        "settings_spotify_quality": "🟢 Spotify quality",
        "settings_ig_quality": "📸 Instagram quality",
        "settings_tt_quality": "🎵 TikTok quality",
        "settings_pin_quality": "📷 Pinterest quality",
        "settings_shorts_quality": "🎬 YouTube Shorts quality",
        "settings_back": "🔙 Back",
        "settings_current": "Current: {value}",
        "settings_quality_prompt": "Choose {platform} quality:",
        "settings_quality_set": "✅ {platform} quality set: {value}",
        "settings_lang_prompt": "Choose your language:",
        # Main menu
        "menu_title": "📋 Main Menu",
        "menu_send_link": "🔗 Send a content link to download",
        "menu_settings": "⚙️ Settings",
        "menu_features": "🌟 Features",
        "menu_stats": "📊 Stats",
        "menu_search": "🔍 Search songs",
        "search_engine_title": "🔍 Search Engine",
        "search_engine_prompt": "Choose your search engine:",
        "search_engine_spotify": "🟢 Spotify (default)",
        "search_engine_soundcloud": "🎵 SoundCloud",
        "search_engine_set": "✅ Search engine set: {engine}",
        "dedupe_toggle_label": "🔁 Skip duplicate tracks (album/playlist/discography)",
        "dedupe_set": "✅ Set: {state}",
        "redl_btn": "⬇️ Re-download from source",
        "redl_downloading": "⬇️ Re-downloading: {title}",
        "search_prompt_spotify": "🔍 To search on Spotify, type:\nSong name or artist",
        "search_no_query": "❗️ Search query is empty. Type a keyword after /search.",
        "search_engine_current": "Current: {engine}",
        "search_no_results_both": "❗️ No results found on Spotify or SoundCloud.",
        "search_try_other": "💡 You can switch search engine:",
        "menu_main": "🏠 Main Menu",
        "menu_back": "🔙 Back",
        # Album / playlist keyboard
        "album_track_count": "📊 Track count: {count}",
        "album_download_all": "⬇️ Download All",
        "album_select_track": "🎵 Select Track",
        "album_cancel": "❌ Cancel",
        # Artist discography
        "artist_found": "🎤 Artist found: <b>{name}</b>\n🎵 {count} tracks",
        "artist_no_tracks": "❗️ No tracks found for this artist.",
        "artist_fetch_error": "❗️ Error fetching artist tracks: {err}",
        "artist_fetching": "🔎 Fetching artist tracks... This may take a few seconds.",
        "artist_fetching_progress": "🔎 Fetching artist tracks... ({done}/{total} albums)",
        "artist_track_count": "📊 Track count: {count}",
        "artist_download_all": "⬇️ Download All ({count})",
        "artist_download_first": "⬇️ Download first {count}",
        "artist_custom_count": "🔢 Custom count",
        "artist_custom_prompt": "Send the number of tracks you want to download (1 to {max}):",
        "artist_custom_invalid": "❗️ Invalid number. Send a number between 1 and {max}.",
        "artist_select_track": "🎵 Select Track",
        "artist_cancel": "❌ Cancel",
        "artist_large_notice": "⚠️ Large discography! You can download all or choose a custom count.",
        # Batch cancel
        "batch_cancel": "❌ Cancel download",
        "batch_cancelled": "⏹️ Download cancelled\n📦 {done}/{total} sent{errors}",
        "batch_cancelled_short": "⏹️ Cancelled",
        "batch_progress_cancelable": "📦 Batch download\n{bar} {pct}% [{done}/{total}]\n🎵 Now: {current}{failed}",
        "batch_failed_list": "\n\n⚠️ Failed ({count}):\n{list}",
        "batch_failed_more": "\n... and {count} more",
        # Common UI strings
        "ui_ok": "✅",
        "ui_invalid": "❗️ Invalid",
        "ui_invalid_page": "❗️ Invalid page",
        "ui_invalid_selection": "❗️ Invalid selection",
        "ui_url_not_found": "❗️ URL not found",
        "ui_choice_expired": "❗️ Choice expired. Please try again.",
        # Error messages (localized)
        "err_track_not_found": "Track not found",
        "err_no_quality": "No suitable quality found",
        "err_process_failed": "Failed to process file",
        "err_file_too_large": "File too large: {size}",
        "err_unsupported_link": "Unsupported link",
        "err_drm_fallback": "DRM protected, fallback failed",
        "err_download_failed": "Download failed",
        "err_send_failed": "Failed to send file: {err}",
        "drm_fallback_msg": "🔄 Trying an alternative source...",
        "search_ready_spotify": "✅ Search engine: Spotify\n\nNow just send me a song name or artist to search.",
        "search_ready_soundcloud": "✅ Search engine: SoundCloud\n\nNow just send me a song name or artist to search.",
        "user_queue_added": "📥 Your request was added to your personal queue. It will start after the current download finishes.",
        "user_queue_busy": "⏳ A collection download is in progress. Your new request was queued.",
    },
}
def tr(chat_id, key, **kwargs):
    lang = get_user_lang(chat_id) or "en"
    text = T.get(lang, T["en"]).get(key, key)
    return text.format(**kwargs) if kwargs else text

# ── 2.2  ProgressBar class ──────────────────────────────────────────────
# ===== Enhanced Progress Bar Class =====
class ProgressBar:
    def __init__(self, chat_id, message_id, total_size=0):
        self.chat_id = chat_id
        self.message_id = message_id
        self.total_size = total_size
        self.last_update_time = 0
        self.last_percentage = -1
        self.update_interval = 3.0  # Update every 3 seconds minimum
        self.percentage_threshold = 5  # Update only every 5% minimum
        
    def create_progress_bar(self, percentage, width=20):
        """Create a visual progress bar"""
        filled = int(width * percentage / 100)
        bar = "█" * filled + "░" * (width - filled)
        return f"[{bar}]"
    
    def format_progress(self, done_bytes, total_bytes):
        """Format progress with visual bar, percentage and file sizes"""
        if total_bytes > 0:
            percentage = int(done_bytes * 100 / total_bytes)
        else:
            percentage = 0
            
        progress_bar = self.create_progress_bar(percentage)
        return f"{progress_bar} {percentage}%"
    
    def should_update(self, current_percentage):
        """Check if we should update the progress message"""
        current_time = time.time()
        
        # Update if it's been long enough OR significant percentage change
        time_passed = current_time - self.last_update_time
        percentage_change = abs(current_percentage - self.last_percentage)
        
        return (time_passed >= self.update_interval or 
                percentage_change >= self.percentage_threshold or
                current_percentage == 100)
    
    def update(self, done_bytes, total_bytes):
        """Update progress with rate limiting"""
        if total_bytes > 0:
            current_percentage = int(done_bytes * 100 / total_bytes)
        else:
            current_percentage = 0
            
        if self.should_update(current_percentage):
            progress_text = self.format_progress(done_bytes, total_bytes)
            
            try:
                safe_edit_message(progress_text, self.chat_id, self.message_id)
                self.last_update_time = time.time()
                self.last_percentage = current_percentage
                return True
            except Exception as e:
                print(f"Error updating progress: {e}")
                return False
        
        return False

# ── 2.3  safe_edit_message + _message_cache ─────────────────────────────
# ===== Safe message editing =====
_message_cache = {}
def safe_edit_message(text, chat_id, message_id, reply_markup=None):
    """Safely edit a message, avoiding duplicate edits"""
    cache_key = (chat_id, message_id)
    last_text = _message_cache.get(cache_key)

    if last_text == text:
        return

    try:
        if reply_markup:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup)
        else:
            bot.edit_message_text(text, chat_id, message_id)
        _message_cache[cache_key] = text
    except Exception as e:
        error_msg = str(e).lower()
        if "message is not modified" not in error_msg:
            raise
        _message_cache[cache_key] = text# Telegram Downloader Bot: Enhanced Version - Part 3


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 3 — FILE PROCESSING & HELPERS                                ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 3.1  FileProcessor class (file_processor instance) ──────────────────
# ===== Enhanced FileProcessor Class =====
class FileProcessor:
    """Unified file processing for all platforms"""
    
    def __init__(self):
        self.supported_audio_exts = ['.mp3', '.m4a', '.wav', '.ogg', '.opus']
        self.supported_video_exts = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm']
        self.supported_image_exts = ['.jpg', '.jpeg', '.png', '.webp']
    
    def sanitize_name(self, name: str) -> str:
        """Sanitize filename for all platforms"""
        return re.sub(r'[\\/:*?"<>|\n\r]+', ' ', name).strip()
    
    def extract_artist(self, info: dict) -> str:
        """Extract artist info from metadata"""
        candidates = [
            info.get("uploader"), info.get("creator"), info.get("artist"),
            info.get("uploader_id"), info.get("user"), info.get("username"),
            info.get("channel"), info.get("channel_name"), info.get("author"),
            info.get("post_author"),
        ]

        for c in candidates:
            if c and isinstance(c, str) and c.strip() and c.lower() != "unknown":
                cleaned = c.strip()
                if cleaned.endswith(" - topic"):
                    cleaned = cleaned[:-7].strip()
                if cleaned and len(cleaned) > 1:
                    return cleaned

        title = info.get("title") or ""
        if title and " - " in title:
            parts = title.split(" - ")
            if len(parts) >= 2:
                potential_artist = parts[0].strip()
                if len(potential_artist) > 1 and len(potential_artist) < 50:
                    return potential_artist

        url = info.get("webpage_url") or info.get("url") or ""
        if url:
            patterns = [r"soundcloud\.com/([^/]+)/", r"/user/([^/]+)/", r"/@([^/]+)/", r"/artist/([^/]+)/"]
            for pattern in patterns:
                m = re.search(pattern, url, re.IGNORECASE)
                if m:
                    artist_name = m.group(1).strip()
                    if artist_name and len(artist_name) > 1:
                        return artist_name

        filename = info.get("_filename") or ""
        if filename and " - " in filename:
            parts = filename.split(" - ")
            if len(parts) >= 2:
                potential_artist = parts[0].strip()
                potential_artist = re.sub(r'[\\/:*?"<>|]', '', potential_artist)
                if potential_artist and len(potential_artist) > 1:
                    return potential_artist

        return "unknown"
    
    def download_thumb(self, thumb_url: str, workdir: str) -> str:
        """Download thumbnail for all platforms"""
        try:
            if not thumb_url:
                return ""
            r = requests.get(thumb_url, timeout=10)
            if r.status_code == 200:
                path = os.path.join(workdir, "thumb.jpg")
                with open(path, "wb") as f:
                    f.write(r.content)
                return path
        except Exception:
            pass
        return ""
    
    def force_audio_extension(self, filepath: str) -> str:
        """Ensure audio files have .mp3 extension"""
        base, ext = os.path.splitext(filepath)
        if ext.lower() in [".ogg", ".opus"]:
            new_fp = base + ".mp3"
            try:
                os.rename(filepath, new_fp)
                return new_fp
            except Exception:
                return filepath
        return filepath
    
    def force_video_extension(self, filepath: str) -> str:
        """Ensure all video files have .mp4 extension"""
        base, ext = os.path.splitext(filepath)
        if ext.lower() not in [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"]:
            new_fp = base + ".mp4"
            try:
                os.rename(filepath, new_fp)
                return new_fp
            except Exception:
                return filepath
        elif ext.lower() != ".mp4":
            new_fp = base + ".mp4"
            try:
                os.rename(filepath, new_fp)
                return new_fp
            except Exception:
                return filepath
        return filepath
    
    def find_downloaded_file(self, workdir: str, preferred_exts=None) -> str:
        """Find the downloaded file in workdir"""
        if not os.path.exists(workdir):
            return None
        
        existing_files = [f for f in os.listdir(workdir) if os.path.isfile(os.path.join(workdir, f))]
        
        if preferred_exts:
            # Try to find file with preferred extension first
            for ext in preferred_exts:
                for file in existing_files:
                    if file.lower().endswith(ext.lower()):
                        return os.path.join(workdir, file)
        
        # Return any file found
        if existing_files:
            return os.path.join(workdir, existing_files[0])
        
        return None
    
    def process_soundcloud_file(self, info, workdir: str):
        """Process SoundCloud file with metadata tagging"""
        fp = info.get("_filename")
        
        # If no filename or file doesn't exist, try to find it in workdir
        if not fp or not os.path.exists(fp):
            print(f"Original file not found: {fp}")
            fp = self.find_downloaded_file(workdir, self.supported_audio_exts)
            print(f"Found file in workdir: {fp}")
        
        if not fp or not os.path.exists(fp):
            return None, "file not found"

        title = self.sanitize_name(info.get("title", "soundcloud_audio"))
        artist = self.sanitize_name(self.extract_artist(info) or "unknown")
        ext = os.path.splitext(fp)[1].lstrip(".")
        new_fp = os.path.join(workdir, f"{artist} - {title}.{ext}")
        
        try:
            os.rename(fp, new_fp)
            print(f"Renamed file: {fp} -> {new_fp}")
        except Exception as e:
            print(f"Rename failed: {e}")
            new_fp = fp

        year, release_date = extract_release_from_meta(info)
        
        # Apply SoundCloud specific tagging
        self._tag_audio_file(new_fp, artist, title, info.get("thumbnail"), extra={
            "year": year, "release_date": release_date, "album": info.get("album") or info.get("playlist"),
        })
        
        thumb_file = self.download_thumb(info.get("thumbnail"), workdir)
        size = os.path.getsize(new_fp)
        duration = info.get("duration", 0)
        
        print(f"Processed SoundCloud file: {new_fp}, size: {size}, duration: {duration}")
        
        return {
            "filepath": new_fp, "title": title, "artist": artist, "size": size,
            "duration": duration, "thumb_file": thumb_file, "ext": ext.lower(),
            "year": year, "release_date": release_date,
            "id": info.get("id") or info.get("display_id"),
            "url": info.get("webpage_url") or info.get("original_url") or info.get("url"),
        }, None
    
    def process_generic_file(self, info, workdir: str, platform: str = "generic"):
        """Process generic file for other platforms"""
        if not info:
            print("process_generic_file: info is None")
            return None

        print(f"process_generic_file: info keys = {list(info.keys())}")

        fp = info.get("_filename")

        if not fp:
            title = info.get("title") or info.get("fulltitle") or "media"
            title = self.sanitize_name(title)

            # Determine file extension based on platform
            if platform in ["YouTube", "TikTok", "Instagram", "Twitter"]:
                ext = ".mp4"  # Default to video for these platforms
            elif platform == "Pinterest":
                # Pinterest can be image or video
                if info.get("ext"):
                    ext = "." + info["ext"]
                elif info.get("video_ext"):
                    ext = "." + info["video_ext"]
                else:
                    ext = ".mp4"  # Default to video
            else:
                ext = ".mp4"  # Default for unknown platforms

            fp = os.path.join(workdir, f"{title}{ext}")
            print(f"process_generic_file: Generated filename: {fp}")

            if not os.path.exists(fp):
                print(f"process_generic_file: File does not exist at {fp}")
                
                # Try to find any file in workdir
                preferred_exts = self.supported_video_exts if platform != "Pinterest" else self.supported_video_exts + self.supported_image_exts
                fp = self.find_downloaded_file(workdir, preferred_exts)
                
                if fp:
                    print(f"process_generic_file: Found fallback file: {fp}")
                else:
                    print("process_generic_file: No files found in workdir")
                    return None

        # Double-check if file exists before proceeding
        if not os.path.exists(fp):
            print(f"process_generic_file: Final file does not exist at {fp}")
            # Try to find any file in workdir
            preferred_exts = self.supported_video_exts if platform != "Pinterest" else self.supported_video_exts + self.supported_image_exts
            fp = self.find_downloaded_file(workdir, preferred_exts)
            if not fp:
                print("process_generic_file: No files found in workdir")
                return None
            print(f"process_generic_file: Found fallback file: {fp}")

        title = info.get("title") or info.get("fulltitle") or "media"
        if not title:
            title = info.get("alt") or info.get("description") or "media"
        title = self.sanitize_name(title)

        print(f"process_generic_file: title = {title}")

        ext = os.path.splitext(fp)[1].lower()
        new_fp = os.path.join(workdir, f"{title}{ext}")

        print(f"process_generic_file: old_fp = {fp}")
        print(f"process_generic_file: new_fp = {new_fp}")

        try:
            if fp != new_fp:
                os.rename(fp, new_fp)
                print("process_generic_file: file renamed successfully")
        except Exception as e:
            print(f"process_generic_file: rename failed: {e}")
            new_fp = fp

        # Ensure video files have .mp4 extension
        final_fp = new_fp
        if ext in ['.webm', '.mkv', '.avi', '.mov', '.flv']:
            final_fp = self.force_video_extension(new_fp)
            ext = '.mp4'

        # Double-check final file exists
        if not os.path.exists(final_fp):
            print(f"process_generic_file: Final file after extension change does not exist at {final_fp}")
            return None

        size = os.path.getsize(final_fp)
        duration = int(info.get("duration") or 0)
        thumb_url = info.get("thumbnail")
        thumb_file = self.download_thumb(thumb_url, workdir)

        result = {
            "filepath": final_fp, "title": title, "size": size,
            "duration": duration, "thumb_file": thumb_file, "ext": ext.lstrip("."),
        }

        print(f"process_generic_file: result = {result}")
        return result
    
    def _tag_audio_file(self, filepath: str, artist: str, title: str, cover_url: str = None, extra=None):
        """Tag audio file with metadata"""
        try:
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TYER, TDRL, TRCK, APIC
            from mutagen.mp4 import MP4, MP4Cover
            from mutagen.oggvorbis import OggVorbis
            extra = extra or {}
            ext = os.path.splitext(filepath)[1].lower()
            if ext == ".mp3":
                try:
                    id3 = ID3(filepath)
                except Exception:
                    id3 = ID3()
                id3.add(TIT2(encoding=3, text=title))
                id3.add(TPE1(encoding=3, text=artist))
                if extra.get("album"):
                    id3.add(TALB(encoding=3, text=str(extra["album"])))
                if extra.get("track_number"):
                    id3.add(TRCK(encoding=3, text=str(extra["track_number"])))
                year = extra.get("year")
                date = extra.get("release_date") or extra.get("date") or year
                if date:
                    id3.add(TDRC(encoding=3, text=str(date)))
                    id3.add(TDRL(encoding=3, text=str(date)))
                if year:
                    id3.add(TYER(encoding=3, text=str(year)))
                if cover_url:
                    try:
                        img = requests.get(cover_url, timeout=10).content
                        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img))
                    except Exception:
                        pass
                id3.save(filepath)
            elif ext in [".m4a", ".mp4", ".aac"]:
                audio = MP4(filepath)
                audio["\xa9nam"] = title
                audio["\xa9ART"] = artist
                if extra.get("album"):
                    audio["\xa9alb"] = str(extra["album"])
                day = extra.get("release_date") or extra.get("date") or extra.get("year")
                if day:
                    audio["\xa9day"] = str(day)
                if extra.get("track_number"):
                    try:
                        audio["trkn"] = [(int(extra["track_number"]), 0)]
                    except Exception:
                        pass
                if cover_url:
                    try:
                        img = requests.get(cover_url, timeout=10).content
                        audio["covr"] = [MP4Cover(img, imageformat=MP4Cover.FORMAT_JPEG)]
                    except Exception:
                        pass
                audio.save()
            elif ext in [".ogg", ".oga", ".opus"]:
                audio = OggVorbis(filepath)
                audio["title"] = [title]
                audio["artist"] = [artist]
                if extra.get("album"):
                    audio["album"] = [str(extra["album"])]
                day = extra.get("release_date") or extra.get("date") or extra.get("year")
                if day:
                    audio["date"] = [str(day)]
                audio.save()
        except Exception:
            pass
# Initialize global file processor
file_processor = FileProcessor()

# ── 3.2  CaptionBuilder class (caption_builder instance) ────────────────
# ===== Enhanced CaptionBuilder Class =====
class CaptionBuilder:
    """Unified caption building for all platforms"""
    
    def __init__(self):
        self.platform_emojis = {
            "SoundCloud": "🎵",
            "Spotify": "🟢",
            "YouTube": "🎬",
            "Pinterest": "📷",
            "Instagram": "📸",
            "TikTok": "🎵",
            "Twitter": "🐦"
        }
        
        self.platform_names = {
            "SoundCloud": "SoundCloud",
            "Spotify": "Spotify",
            "YouTube": "YouTube",
            "Pinterest": "Pinterest",
            "Instagram": "Instagram",
            "TikTok": "TikTok",
            "Twitter": "Twitter"
        }
    
    def format_duration_for_lang(self, seconds: int, lang: str) -> str:
        """Format duration based on language"""
        seconds = int(seconds or 0)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if lang == "fa":
            return f"{h} ساعت {m} دقیقه {s} ثانیه" if h > 0 else f"{m} دقیقه {s} ثانیه"
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
    
    def human_size(self, n: int, chat_id=None) -> str:
        """Convert bytes to human readable format with Persian/English units"""
        if chat_id and get_user_lang(chat_id) == "fa":
            # Persian units
            for unit in ["بایت", "کیلوبایت", "مگابایت", "گیگابایت"]:
                if n < 1024.0:
                    return f"{n:.1f} {unit}"
                n /= 1024.0
            return f"{n:.1f} ترابایت"
        else:
            # English units
            for unit in ["B", "KB", "MB", "GB"]:
                if n < 1024.0:
                    return f"{n:.1f} {unit}"
                n /= 1024.0
            return f"{n:.1f} TB"
    
    def build_caption(self, chat_id, platform, item, original_url=None, **kwargs):
        """Build unified caption for any platform.

        Layout (using Telegram blockquote, parse_mode=HTML):
          {emoji} {Platform}
          <blockquote expandable>
            ...track metadata (artist, title, album, duration, size, link)...
          </blockquote>
          <blockquote>
            @{BOT_USERNAME} | {signature}
          </blockquote>
        The metadata block is expandable (collapsed by default on Telegram
        clients that support it) to keep captions compact; the signature is a
        normal blockquote so it's always visible.
        """
        lang = get_user_lang(chat_id) or "en"
        signature = T[lang]["signature"]
        link_text = tr(chat_id, "content_link")
        emoji = self.platform_emojis.get(platform, "📁")
        platform_name = self.platform_names.get(platform, platform)

        # Header line (always visible)
        header_line = f"{emoji} {platform_name}"
        meta_lines = [header_line]

        # Platform-specific formatting → collected into a block (for expandable quote)
        if platform == "SoundCloud":
            sc_lines = [f"🎵 {_html_esc(item.get('artist'))} - {_html_esc(item.get('title'))}"]
            year = item.get("year") or item.get("release_year")
            date = item.get("release_date") or item.get("date")
            if date and date != year:
                sc_lines.append(f"📅 {_html_esc(date)}")
            elif year:
                sc_lines.append(f"📅 {_html_esc(year)}")
            sc_lines.extend([
                f"⏱️ {self.format_duration_for_lang(item['duration'], lang)}",
                f"💾 {self.human_size(item['size'], chat_id)}"
            ])
            meta_lines.extend(sc_lines)
        elif platform == "Spotify":
            sp_lines = [f"🎵 {_html_esc(item.get('artist'))} - {_html_esc(item.get('title'))}"]
            if item.get("album"):
                sp_lines.append(f"💿 {_html_esc(item['album'])}")
            if item.get("track_number"):
                sp_lines.append(f"🔢 #{_html_esc(item['track_number'])}")
            year = item.get("year") or item.get("release_year")
            date = item.get("release_date") or item.get("date")
            if date and date != year:
                sp_lines.append(f"📅 {_html_esc(date)}")
            elif year:
                sp_lines.append(f"📅 {_html_esc(year)}")
            sp_lines.append(f"⏱️ {self.format_duration_for_lang(item['duration'], lang)}")
            sp_lines.append(f"💾 {self.human_size(item['size'], chat_id)}")
            meta_lines.extend(sp_lines)
        elif platform == "YouTube":
            audio_only = kwargs.get('audio_only', False)
            if audio_only:
                meta_lines.extend([
                    f"🎵 {_html_esc(item.get('title'))}",
                    f"⏱️ {self.format_duration_for_lang(item['duration'], lang)}",
                    f"💾 {self.human_size(item['size'], chat_id)}"
                ])
            else:
                meta_lines.extend([
                    f"🎬 {_html_esc(item.get('title'))}",
                    f"⏱️ {self.format_duration_for_lang(item['duration'], lang)}",
                    f"💾 {self.human_size(item['size'], chat_id)}"
                ])
        else:
            # Generic formatting for other platforms
            if item.get("title"):
                meta_lines.append(f"📝 {_html_esc(item['title'])}")
            if item.get("duration"):
                meta_lines.append(f"⏱️ {self.format_duration_for_lang(item['duration'], lang)}")
            meta_lines.append(f"💾 {self.human_size(item['size'], chat_id)}")

        # Add original link inside the expandable block (if provided)
        if original_url:
            safe_href = _html_esc(original_url).replace('"', "&quot;")
            meta_lines.append(f'🔗 <a href="{safe_href}">{_html_esc(link_text)}</a>')

        # Build the caption:
        # - header line first
        # - then expandable blockquote with the rest of the metadata
        # - then a normal blockquote with the signature
        header = meta_lines[0]
        body_lines = meta_lines[1:]
        if body_lines:
            body = "\n".join(body_lines)
            expandable_block = f"<blockquote expandable>{body}</blockquote>"
        else:
            expandable_block = ""
        sig_block = f"<blockquote>@{BOT_USERNAME} | {signature}</blockquote>"

        parts = [header]
        if expandable_block:
            parts.append(expandable_block)
        parts.append(sig_block)
        return "\n".join(parts)
# Initialize global caption builder
caption_builder = CaptionBuilder()

# ── 3.3  Misc helpers (human_size, sanitize_name, extract_artist, wrappers, detect_platform_from_url, resolve_url, estimate_file_size) ────
# ===== Other Helper Functions =====
def human_size(n: int, chat_id=None) -> str:
    """Wrapper for backward compatibility"""
    return caption_builder.human_size(n, chat_id)
def sanitize_name(name: str) -> str:
    """Wrapper for backward compatibility"""
    return file_processor.sanitize_name(name)
def extract_artist(info: dict) -> str:
    """Wrapper for backward compatibility"""
    return file_processor.extract_artist(info)
def format_duration_for_lang(seconds: int, lang: str) -> str:
    """Wrapper for backward compatibility"""
    return caption_builder.format_duration_for_lang(seconds, lang)
def download_thumb(thumb_url: str, workdir: str) -> str:
    """Wrapper for backward compatibility"""
    return file_processor.download_thumb(thumb_url, workdir)
def force_audio_extension(filepath: str) -> str:
    """Wrapper for backward compatibility"""
    return file_processor.force_audio_extension(filepath)
def force_video_extension(filepath: str) -> str:
    """Wrapper for backward compatibility"""
    return file_processor.force_video_extension(filepath)
def process_sc_info_to_file(info, workdir: str):
    """Wrapper for backward compatibility"""
    return file_processor.process_soundcloud_file(info, workdir)
def finalize_generic_item(info, workdir: str):
    """Wrapper for backward compatibility"""
    return file_processor.process_generic_file(info, workdir)
def detect_platform_from_url(url):
    """Detect platform from URL"""
    url = url.lower()
    if "soundcloud.com" in url:
        return "SoundCloud"
    elif "spotify.com" in url or "spotify:" in url:
        return "Spotify"
    elif "pinterest.com" in url or "pin.it" in url:
        return "Pinterest"
    elif "instagram.com" in url or "instagr.am" in url:
        return "Instagram"
    elif "youtube.com" in url or "youtu.be" in url:
        return "YouTube"
    elif "tiktok.com" in url:
        return "TikTok"
    elif "twitter.com" in url or "x.com" in url or "t.co" in url:
        return "Twitter"
    else:
        return "Unknown"
def resolve_url(url: str) -> str:
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return r.url or url
    except Exception:
        return url
def estimate_file_size(format_info, duration_seconds):
    """Estimate file size based on format info and duration"""
    try:
        # Get bitrate information
        if format_info.get("vcodec") != "none" and format_info.get("acodec") != "none":
            # Video + audio
            vbr = format_info.get("vbr", 0) or format_info.get("tbr", 0) or 1000  # Default to 1000 kbps
            abr = format_info.get("abr", 0) or 128  # Default to 128 kbps for audio
            total_bitrate = vbr + abr
        elif format_info.get("vcodec") == "none" and format_info.get("acodec") != "none":
            # Audio only
            total_bitrate = format_info.get("abr", 0) or format_info.get("tbr", 0) or 128
        else:
            # Video only or unknown
            total_bitrate = format_info.get("tbr", 0) or 1000
        
        # Calculate size (in bytes)
        size_bits = total_bitrate * 1000 * duration_seconds  # Convert kbps to bits
        size_bytes = size_bits / 8  # Convert bits to bytes
        
        # Add some buffer (10%)
        size_bytes *= 1.1
        
        return int(size_bytes)
        
    except Exception as e:
        print(f"Error estimating file size: {e}")
        # Return a conservative estimate
        return 10 * 1024 * 1024  # 10MB default# Telegram Downloader Bot: Enhanced Version - Part 4


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 4 — DOWNLOAD ENGINES                                         ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 4.1  yt-dlp options builders (_SilentLogger, _audio_actual_duration, _is_sc_premium_preview, make_youtube_opts, make_sc_opts, make_generic_opts) ────
# ===== Enhanced yt-dlp options builders =====
def _audio_actual_duration(filepath):
    """Return the actual playback duration (seconds) of an audio file via ffprobe.

    Used to detect SoundCloud Go+ preview tracks: yt-dlp downloads only the
    first ~30s of premium tracks without raising an error.
    """
    try:
        import subprocess
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, timeout=20
        )
        if r.returncode == 0:
            return float(r.stdout.decode("utf-8", "ignore").strip() or 0)
    except Exception:
        pass
    return 0.0
def _is_sc_premium_preview(item, expected_duration):
    """Detect whether a downloaded SoundCloud track is a Go+ premium preview.

    Premium tracks download only ~30s. We consider it a preview if:
      - actual audio duration ≤ 31s, AND
      - expected (SoundCloud metadata) duration > 40s
    """
    if not item or not item.get("filepath"):
        return False
    exp = int(expected_duration or 0)
    if exp <= 40:
        return False  # genuinely short track
    actual = _audio_actual_duration(item["filepath"])
    if actual <= 0:
        return False
    if actual <= 31.0 and exp > 40:
        print(f"[SC premium] preview detected: actual={actual:.1f}s vs expected={exp}s")
        return True
    return False
def make_sc_opts(workdir: str, quality: str, progress_hook=None, force_mp3=False):
    format_sel = "bestaudio/best" if quality == "high" else "bestaudio[abr<=128]/bestaudio/best"
    opts = {
        "format": format_sel,
        "noplaylist": False,
        "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "default_search": "auto",
        "socket_timeout": 60,
        "extractor_retries": 5,
        "fragment_retries": 5,
        "retry_sleep": 2,
        "file_access_retries": 3,
        "retries": 5,
    }
    # Enable deno JS runtime (required by modern yt-dlp for YouTube extraction)
    opts.update(_deno_opts())
    # Cookies help avoid YouTube bot-check when SoundCloud search falls back to YouTube
    if COOKIES_AVAILABLE:
        opts["cookiefile"] = COOKIES_PATH

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if force_mp3:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    return opts
def make_generic_opts(workdir: str, progress_hook=None):
    opts = {
        "format": "bestvideo+bestaudio/bestvideo/bestaudio/best",
        "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "nocheckcertificate": True,
        "no_check_certificate": True,
        "extractor_retries": 5,
        "socket_timeout": 30,
        "prefer_ffmpeg": True,
        "ignoreerrors": True,
    }
    
    if COOKIES_AVAILABLE:
        opts["extractor_args"] = {
            "instagram": {
                "cookies": [COOKIES_PATH]
            }
        }
        opts["cookiefile"] = COOKIES_PATH

    # Enable deno JS runtime for any YouTube / embedded YouTube content
    opts.update(_deno_opts())

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts
def make_youtube_opts(workdir: str, format_id: str, progress_hook=None, audio_only=False):
    """YouTube-specific options with quality selection, cookie support and deno.

    NOTE: deno (a JS runtime) is REQUIRED by modern yt-dlp to solve YouTube's
    signature/n-challenge. Without it, downloads fail with "The page needs to
    be reloaded". We merge _deno_opts() below.
    """
    if audio_only:
        # Audio-only: build a complete opts dict (previously this branch was
        # missing outtmpl/quiet/cookies/etc., which broke audio-only downloads).
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",  # proper fallback chain (was 'bestaudio[ext=m4a]/')
            "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "nocheckcertificate": True,
            "socket_timeout": 30,
            "extractor_retries": 3,
            "fragment_retries": 3,
            "retry_sleep": 2,
            "prefer_ffmpeg": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",  # m4a is faster than mp3
                    "preferredquality": "192"
                }
            ],
        }
    else:
        opts = {
            "format": format_id,
            "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "nocheckcertificate": True,
            "socket_timeout": 30,
            "extractor_retries": 3,
            "fragment_retries": 3,
            "retry_sleep": 2,
            "prefer_ffmpeg": True,
        }

    # Enable deno JS runtime (required by modern yt-dlp for YouTube signature/n-challenge)
    opts.update(_deno_opts())

    # Add cookies if available
    if COOKIES_AVAILABLE:
        opts["cookiefile"] = COOKIES_PATH
        print(f"Using cookies file: {COOKIES_PATH}")
    else:
        print("No cookies file available, proceeding without cookies")

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts
class _SilentLogger:
    """Suppresses yt-dlp noise errors (format-not-available, nsig, bot-check)."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg):
        msg_str = str(msg)
        if any(x in msg_str for x in ["Requested format is not available", "Sign in to confirm", "nsig"]):
            return
        print(f"[yt-dlp] {msg_str[:120]}")

# ── 4.2  YouTube detection + quality + handlers (is_youtube_short, qualities, selection, download) ────
# ===== YouTube Detection and Shorts =====
def is_youtube_short(url: str) -> bool:
    """Detect if URL is a YouTube Short"""
    url = url.lower()
    
    # Check for shorts indicators in URL
    if "youtube.com/shorts/" in url:
        return True
    elif "youtu.be/" in url:
        # youtu.be links can be shorts, need to check video info
        return None  # Unknown, need further checking
    elif "youtube.com/watch" in url:
        # Check for shorts parameters
        if "shorts" in url:
            return True
    
    return False
def get_youtube_video_info(url: str) -> dict:
    """Get YouTube video information to detect shorts and get details"""
    tmpdir = tempfile.mkdtemp(prefix="yt_info_")
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "simulate": True,
            "skip_download": True,
            "cookiefile": COOKIES_PATH if COOKIES_AVAILABLE else None,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        print(f"Error getting YouTube video info: {e}")
        return {}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
def confirm_youtube_short(url: str) -> bool:
    """Confirm if a YouTube video is actually a Short by checking video info"""
    info = get_youtube_video_info(url)
    if not info:
        return False
    
    # Check duration (shorts are typically < 60 seconds)
    duration = info.get("duration", 0)
    if duration and duration <= 60:
        return True
    
    # Check for shorts-specific metadata
    title = info.get("title", "").lower()
    description = info.get("description", "").lower()
    
    # Sometimes shorts have specific indicators
    if "#shorts" in title or "#shorts" in description:
        return True
    
    return False
# ===== Enhanced YouTube Quality Selection with Merging and New Button Format =====
def get_youtube_qualities_with_merging(url: str, chat_id):
    """Get available YouTube qualities with video+audio merging and new button format"""
    print(f"Getting YouTube qualities for: {url}")
    tmpdir = tempfile.mkdtemp(prefix="yt_qualities_")
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "simulate": True,
            "skip_download": True,
            "listformats": True,
            "cookiefile": COOKIES_PATH if COOKIES_AVAILABLE else None,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return []
            
            formats = info.get("formats", [])
            qualities = []
            
            # Get audio format for merging
            audio_formats = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"]
            best_audio = None
            if audio_formats:
                best_audio = max(audio_formats, key=lambda x: x.get("abr", 0) or 0)
            
            # Get video-only formats and merge with audio
            video_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none"]
            video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
            
            # Define quality priorities for regular YouTube videos
            quality_priorities = [1080, 720, 480, 360, 240, 144]
            
            for height in quality_priorities:
                # Find video format with this height
                video_fmt = None
                for fmt in video_formats:
                    if fmt.get("height") == height:
                        video_fmt = fmt
                        break
                
                if video_fmt and best_audio:
                    # Create merged format ID
                    merged_format_id = f"{video_fmt['format_id']}+{best_audio['format_id']}"
                    
                    # Calculate actual file size by combining video and audio
                    video_size = estimate_file_size(video_fmt, info.get("duration", 0))
                    audio_size = estimate_file_size(best_audio, info.get("duration", 0))
                    total_size = video_size + audio_size
                    
                    if total_size <= TELEGRAM_UPLOAD_LIMIT:
                        # Quality label based on height
                        if height >= 1080:
                            quality_label = "1080p"
                        elif height >= 720:
                            quality_label = "720p"
                        elif height >= 480:
                            quality_label = "480p"
                        elif height >= 360:
                            quality_label = "360p"
                        else:
                            quality_label = f"{height}p"
                        
                        qualities.append({
                            "format_id": merged_format_id,
                            "quality": quality_label,
                            "size": total_size,
                            "ext": "mp4",
                            "type": "video",
                            "height": height,
                            "video_format": video_fmt['format_id'],
                            "audio_format": best_audio['format_id']
                        })
            
            # Add audio-only option
            if best_audio:
                audio_size = estimate_file_size(best_audio, info.get("duration", 0))
                if audio_size <= TELEGRAM_UPLOAD_LIMIT:
                    qualities.append({
                        "format_id": best_audio["format_id"],
                        "quality": "Audio Only",
                        "size": audio_size,
                        "ext": best_audio.get("ext", "mp3"),
                        "type": "audio"
                    })
            
            # Remove duplicates and sort
            unique_qualities = []
            seen_qualities = set()
            for q in qualities:
                key = (q["quality"], q["type"])
                if key not in seen_qualities:
                    unique_qualities.append(q)
                    seen_qualities.add(key)
            
            # Sort: audio first, then video by quality (highest to lowest)
            unique_qualities.sort(key=lambda x: (x["type"] != "audio", -x.get("height", 0)))
            
            print(f"Found {len(unique_qualities)} suitable qualities")
            for q in unique_qualities:
                print(f"  - {q['quality']} ({q['type']}): {human_size(q['size'])}")
            
            return unique_qualities
            
    except Exception as e:
        print(f"Error getting YouTube qualities: {e}")
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
def get_best_youtube_short_quality(url: str, chat_id):
    """Get best quality for YouTube Shorts (max 1080p, video+audio) under 50MB"""
    print(f"Getting best YouTube Short quality for: {url}")
    tmpdir = tempfile.mkdtemp(prefix="yt_shorts_")
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "simulate": True,
            "skip_download": True,
            "listformats": True,
            "cookiefile": COOKIES_PATH if COOKIES_AVAILABLE else None,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            
            formats = info.get("formats", [])
            
            # Get best audio
            audio_formats = [f for f in formats if f.get("acodec") != "none"]
            best_audio = None
            if audio_formats:
                best_audio = max(audio_formats, key=lambda x: x.get("abr", 0) or 0)
            
            # For Shorts, limit to 1080p maximum
            max_height = 1080
            quality_priorities = [1080, 720, 480, 360, 240, 144]
            
            # Get best video with audio (prefer pre-merged)
            video_with_audio = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
            video_with_audio.sort(key=lambda x: x.get("height", 0), reverse=True)
            
            # Try pre-merged formats first (limited to 1080p)
            for fmt in video_with_audio:
                height = fmt.get("height", 0)
                if height <= max_height:  # Limit to 1080p for Shorts
                    size = estimate_file_size(fmt, info.get("duration", 0))
                    if size <= TELEGRAM_UPLOAD_LIMIT:
                        return {
                            "format_id": fmt["format_id"],
                            "quality": f"{height}p",
                            "size": size,
                            "ext": fmt.get("ext", "mp4"),
                            "type": "video"
                        }
            
            # If no suitable pre-merged format, try to merge video-only + audio (limited to 1080p)
            if best_audio:
                video_only = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none"]
                video_only.sort(key=lambda x: x.get("height", 0), reverse=True)
                
                for video_fmt in video_only:
                    height = video_fmt.get("height", 0)
                    if height <= max_height:  # Limit to 1080p for Shorts
                        merged_format_id = f"{video_fmt['format_id']}+{best_audio['format_id']}"
                        video_size = estimate_file_size(video_fmt, info.get("duration", 0))
                        audio_size = estimate_file_size(best_audio, info.get("duration", 0))
                        total_size = video_size + audio_size
                        
                        if total_size <= TELEGRAM_UPLOAD_LIMIT:
                            return {
                                "format_id": merged_format_id,
                                "quality": f"{height}p",
                                "size": total_size,
                                "ext": "mp4",
                                "type": "video",
                                "video_format": video_fmt['format_id'],
                                "audio_format": best_audio['format_id']
                            }
            
            # Fallback to audio only
            if best_audio:
                audio_size = estimate_file_size(best_audio, info.get("duration", 0))
                if audio_size <= TELEGRAM_UPLOAD_LIMIT:
                    return {
                        "format_id": best_audio["format_id"],
                        "quality": "Audio Only",
                        "size": audio_size,
                        "ext": best_audio.get("ext", "mp3"),
                        "type": "audio"
                    }
            
            return None
            
    except Exception as e:
        print(f"Error getting YouTube Short quality: {e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
# ===== Enhanced Button Formatting =====
def create_youtube_quality_keyboard(qualities, chat_id):
    """Create keyboard for YouTube quality selection with colored buttons."""
    kb = InlineKeyboardMarkup()
    
    if not qualities:
        return kb

    current_row = []
    for i, quality in enumerate(qualities):
        quality_type = quality.get("type", "video")
        quality_label = quality.get("quality", "Unknown")
        size_mb = quality["size"] / (1024 * 1024)
        size_text = f"{size_mb:.1f} مگابایت" if (get_user_lang(chat_id) or "en") == "fa" else f"{size_mb:.1f} MB"

        if quality_type == "audio":
            label = "🎵 " + ("فقط صدا" if (get_user_lang(chat_id) or "en") == "fa" else "Audio Only") + " • " + size_text
            style = "success"
        else:
            label = f"🎬 {quality_label} • {size_text}"
            style = "primary"

        format_id = quality.get("format_id", "unknown")
        callback_data = f"yt_quality:{format_id}:{quality_type}"
        current_row.append(btn(label, callback_data=callback_data, style=style))
        if len(current_row) == 2:
            kb.row(*current_row)
            current_row = []
    if current_row:
        kb.row(*current_row)
    # Cancel button
    kb.row(btn(tr(chat_id, "close_menu"), callback_data="yt_cancel", style="danger"))
    return kb
def create_youtube_shorts_keyboard(chat_id):
    """Create keyboard for YouTube Shorts selection with colored buttons."""
    kb = InlineKeyboardMarkup()
    if (get_user_lang(chat_id) or "en") == "fa":
        video_btn = "🎬 ویدیو"
        audio_btn = "🎵 فقط صدا"
    else:
        video_btn = "🎬 Video"
        audio_btn = "🎵 Audio Only"
    kb.row(
        btn(video_btn, callback_data="yt_shorts:video", style="primary"),
        btn(audio_btn, callback_data="yt_shorts:audio", style="success"),
    )
    # Cancel button
    kb.row(btn(tr(chat_id, "close_menu"), callback_data="yt_cancel", style="danger"))
    return kb
# ===== YouTube Quality Selection Handler =====
def handle_youtube_quality_selection(call):
    """Handle YouTube quality selection callback with merging support"""
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    try:
        # Parse callback data
        parts = call.data.split(":")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Invalid selection")
            return
        
        format_id = parts[1]
        media_type = parts[2]  # "audio" or "video"
        
        # Get the URL and qualities from cache
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT url FROM youtube_quality_cache WHERE chat_id=?", (chat_id,))
            row = c.fetchone()
        
        if not row:
            bot.answer_callback_query(call.id, "URL not found")
            return
        
        url = row[0]
        
        # Get the quality text for display
        quality_text = "Audio Only" if media_type == "audio" else "Video"
        
        # For video, try to get the actual quality from the format_id
        if media_type == "video":
            # Extract quality from format_id if it's a merged format
            if "+" in format_id:
                # This is a merged format, we need to get the quality from our cached data
                qualities = get_youtube_qualities(chat_id, url)
                if qualities:
                    for q in qualities:
                        if q.get("format_id") == format_id:
                            quality_text = f"Video ({q.get('quality', 'Unknown')})"
                            break
                else:
                    # Fallback: try to extract from format_id (less reliable)
                    quality_text = f"Video ({format_id})"
            else:
                quality_text = f"Video ({format_id})"
        else:
            # For Persian users, show "فقط صدا"
            if get_user_lang(chat_id) == "fa":
                quality_text = "فقط صدا"
            else:
                quality_text = "Audio Only"
        
        # Answer callback immediately to avoid timeout
        bot.answer_callback_query(call.id, tr(chat_id, "youtube_selected_quality", quality=quality_text))
        
        # Delete the quality selection message
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as e:
            print(f"Error deleting quality selection message: {e}")
        
        # Show downloading message with correct quality text
        msg = bot.send_message(chat_id, tr(chat_id, "youtube_downloading", quality=quality_text))
        
        # Download with selected quality
        download_youtube_with_quality(chat_id, url, format_id, media_type, msg.message_id)
        
    except Exception as e:
        print(f"Error in YouTube quality selection: {e}")
        # Only answer callback if not already answered
        try:
            bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)
        except:
            pass
def handle_youtube_shorts_selection(call):
    """Handle YouTube Shorts selection callback with new logic"""
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    try:
        # Parse callback data
        parts = call.data.split(":")
        if len(parts) < 2:
            bot.answer_callback_query(call.id, "Invalid selection")
            return
        
        choice = parts[1]  # "video" or "audio"
        
        # Get the URL from cache
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT url FROM youtube_shorts_cache WHERE chat_id=?", (chat_id,))
            row = c.fetchone()
        
        if not row:
            bot.answer_callback_query(call.id, "URL not found")
            return
        
        url = row[0]
        
        # Delete the selection message
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as e:
            print(f"Error deleting selection message: {e}")
        
        # Get best quality for the short to show actual quality
        quality_info = get_best_youtube_short_quality(url, chat_id)
        
        # Get the quality text for display
        if choice == "audio":
            # For Persian users, show "فقط صدا"
            if get_user_lang(chat_id) == "fa":
                choice_text = "فقط صدا"
            else:
                choice_text = "Audio Only"
        else:
            # For video, show the actual quality
            if quality_info:
                choice_text = f"Video ({quality_info.get('quality', 'Unknown')})"
            else:
                choice_text = "Video"
        
        # Answer callback immediately to avoid timeout
        bot.answer_callback_query(call.id, f"Downloading {choice_text}")
        
        # Delete the selection message
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as e:
            print(f"Error deleting selection message: {e}")
        
        msg = bot.send_message(chat_id, tr(chat_id, "youtube_shorts_downloading"))
        
        # Download the short with best quality
        download_youtube_short_with_choice(chat_id, url, choice, msg.message_id)
        
    except Exception as e:
        print(f"Error in YouTube Shorts selection: {e}")
        # Only answer callback if not already answered
        try:
            bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)
        except:
            pass
def download_youtube_with_quality(chat_id, url, format_id, media_type, message_id):
    """Download YouTube with specific quality with merging support"""
    tmpdir = tempfile.mkdtemp(prefix="youtube_dl_")
    
    try:
        # Create progress bar instance
        progress_bar = ProgressBar(chat_id, message_id)
        
        def progress_hook(d):
            try:
                if d.get("status") == "downloading":
                    done = d.get("downloaded_bytes", 0)
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    progress_bar.update(done, total)
            except Exception:
                pass
        
        # Download with selected format
        audio_only = (media_type == "audio")
        ydl_opts = make_youtube_opts(tmpdir, format_id, progress_hook=progress_hook, audio_only=audio_only)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            info["_filename"] = ydl.prepare_filename(info)
            item = finalize_generic_item(info, tmpdir)
            
            if item:
                # Ensure correct file extension
                if not audio_only:
                    item["filepath"] = force_video_extension(item["filepath"])
                
                # Send the file
                send_youtube_item(chat_id, item, url, audio_only)
            else:
                safe_edit_message(tr(chat_id, "error", err="Failed to process downloaded file"), chat_id, message_id)
                
    except Exception as e:
        safe_edit_message(tr(chat_id, "error", err=str(e)), chat_id, message_id)
    finally:
        # Clean up AFTER sending
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        
        # Clear cache
        clear_youtube_quality_cache(chat_id)
def download_youtube_short_with_choice(chat_id, url, choice, message_id):
    """Download YouTube Short with user choice and no thumbnail"""
    tmpdir = tempfile.mkdtemp(prefix="youtube_short_dl_")
    
    try:
        # Create progress bar instance
        progress_bar = ProgressBar(chat_id, message_id)
        
        def progress_hook(d):
            try:
                if d.get("status") == "downloading":
                    done = d.get("downloaded_bytes", 0)
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    progress_bar.update(done, total)
            except Exception:
                pass
        
        # Get best quality for the short (max 1080p)
        quality_info = get_best_youtube_short_quality(url, chat_id)
        
        if not quality_info:
            safe_edit_message(tr(chat_id, "error", err="No suitable quality found"), chat_id, message_id)
            return
        
        # Determine format based on user choice
        if choice == "audio":
            # Force audio-only
            format_id = None  # Will use best audio
            audio_only = True
            quality_text = "فقط صدا" if get_user_lang(chat_id) == "fa" else "Audio Only"
        else:
            # Use best video quality found (max 1080p)
            format_id = quality_info["format_id"]
            audio_only = False
            quality_text = quality_info.get('quality', 'Video')
        
        # Update downloading message with actual quality
        safe_edit_message(tr(chat_id, "youtube_downloading", quality=quality_text), chat_id, message_id)
        
        # Download
        ydl_opts = make_youtube_opts(tmpdir, format_id, progress_hook=progress_hook, audio_only=audio_only)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            info["_filename"] = ydl.prepare_filename(info)
            item = finalize_generic_item(info, tmpdir)
            
            if item:
                # Ensure correct file extension
                if not audio_only:
                    item["filepath"] = force_video_extension(item["filepath"])
                
                # Send the file WITHOUT thumbnail for Shorts
                send_youtube_short_item(chat_id, item, url, audio_only)
            else:
                safe_edit_message(tr(chat_id, "error", err="Failed to process downloaded file"), chat_id, message_id)
                
    except Exception as e:
        safe_edit_message(tr(chat_id, "error", err=str(e)), chat_id, message_id)
    finally:
        # Clean up AFTER sending
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        
        # Clear cache
        clear_youtube_shorts_cache(chat_id)

# ── 4.3  SoundCloud core (detect_content_type, download_soundcloud_with_retry, download_soundcloud, _sc_youtube_fallback) ────
# ===== SoundCloud core =====
def detect_content_type(url):
    """Smart content type detection from link"""
    url = resolve_url(url)

    if 'soundcloud.com' in url:
        if any(indicator in url.lower() for indicator in ['/sets/', '/albums/', '/playlist/']):
            return "playlist"
        if any(pattern in url for pattern in ['/you/', '/stations/']):
            return "playlist"

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; SoundCloudBot/1.0)'}
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
        content_type = response.headers.get('content-type', '')
        final_url = response.url

        if any(indicator in final_url.lower() for indicator in ['/sets/', '/albums/', '/playlist/']):
            return "playlist"
        if any(pattern in final_url for pattern in ['/you/', '/stations/']):
            return "playlist"

    except Exception as e:
        print(f"Error in URL detection: {e}")

    try:
        ydl_opts = {
            "quiet": True, "no_warnings": True, "extract_flat": False,
            "simulate": True, "skip_download": True,
            "cookiefile": COOKIES_PATH if COOKIES_AVAILABLE else None,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if "entries" in info and info["entries"]:
                if len(info["entries"]) > 1:
                    return "playlist"
                elif len(info["entries"]) == 1:
                    return "single"

            if info.get("_type") == "playlist":
                return "playlist"
            elif info.get("ie_key") == "soundcloud:set":
                return "playlist"
            elif info.get("ie_key") == "soundcloud:track":
                return "single"

    except Exception as e:
        print(f"Error in yt-dlp detection: {e}")

    return "single"
def download_soundcloud_with_retry(url_or_query: str, workdir: str, quality: str, is_search=False, search_limit=15, progress_hook=None, max_retries=3):
    """Download SoundCloud content with simple retry logic (proxy support removed).

    Retries now only help for transient network errors (timeouts, 429s).
    DRM-protected and geo-restricted tracks fail immediately — DRM is sent to
    the YouTube fallback path, geo-restriction is reported as a hard failure.
    """
    last_error = "All attempts failed"
    for attempt in range(max_retries):
        try:
            ydl_opts = make_sc_opts(workdir, quality, progress_hook=progress_hook, force_mp3=FORCE_MP3)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                if is_search:
                    info = ydl.extract_info(f"scsearch{search_limit}:{url_or_query}", download=False)
                    entries = info.get("entries") or []
                    choices = []
                    for e in entries:
                        choices.append({
                            "title": e.get("title"), "artist": extract_artist(e),
                            "url": e.get("webpage_url"), "duration": e.get("duration", 0),
                            "thumb": e.get("thumbnail"),
                        })
                    return {"choices": choices, "ok": True}
                else:
                    content_type = detect_content_type(url_or_query)
                    print(f"Detected content type: {content_type} for URL: {url_or_query}")

                    if content_type == "playlist":
                        ydl_opts_playlist = {
                            "quiet": True, "no_warnings": True, "extract_flat": False,
                            "simulate": True, "skip_download": True,
                        }

                        with yt_dlp.YoutubeDL(ydl_opts_playlist) as ydl_playlist:
                            info = ydl_playlist.extract_info(url_or_query, download=False)

                            if "entries" in info and info["entries"]:
                                playlist_items = []
                                for e in info["entries"]:
                                    if e:
                                        playlist_items.append({
                                            "title": e.get("title", "Unknown Title"),
                                            "artist": extract_artist(e) or "Unknown Artist",
                                            "url": e.get("webpage_url", ""),
                                            "duration": e.get("duration", 0),
                                            "thumb": e.get("thumbnail"),
                                        })

                                return {"playlist": playlist_items, "ok": True, "content_type": "playlist"}
                            else:
                                return {"error": "No playlist items found", "ok": False}
                    else:
                        info = ydl.extract_info(url_or_query, download=True)
                        info["_filename"] = ydl.prepare_filename(info)
                        item, err = process_sc_info_to_file(info, workdir)
                        if not item:
                            return {"error": err or "failed", "ok": False}
                        return {"item": item, "ok": True, "content_type": "single"}

        except Exception as e:
            error_str = str(e).lower()
            last_error = str(e)
            print(f"Attempt {attempt + 1} failed: {error_str[:200]}")

            # DRM-protected track → fail immediately and signal the YouTube fallback path
            if "drm" in error_str or "drm protected" in error_str:
                print(f"[SoundCloud] DRM-protected track, skipping to YouTube fallback")
                return {"error": f"DRM protected: {str(e)}", "ok": False, "drm": True}

            # Geo-restriction is now a hard failure (no proxy to retry with)
            if "geo restriction" in error_str or "not available from your location" in error_str:
                print(f"[SoundCloud] Geo-restriction detected — no proxy available, failing")
                return {"error": str(e), "ok": False}

            # Transient errors (timeouts, 429): retry up to max_retries
            if attempt == max_retries - 1:
                return {"error": str(e), "ok": False}
            # brief backoff before next attempt
            time.sleep(1.5)
            continue

    return {"error": last_error, "ok": False}
def download_soundcloud(url_or_query: str, workdir: str, quality: str, is_search=False, search_limit=15, progress_hook=None):
    """Wrapper for backward compatibility"""
    return download_soundcloud_with_retry(url_or_query, workdir, quality, is_search, search_limit, progress_hook)
def _sc_youtube_fallback(track_meta, workdir, progress_hook=None):
    """YouTube fallback for DRM-protected SoundCloud tracks.

    Uses spotdl's Song.from_search_term() to search Spotify for the track,
    then downloads via spotdl's audio providers (youtube-music, etc.).
    This is more reliable than raw ytsearch because spotdl uses the YouTube
    Music API which doesn't get bot-checked like yt-dlp's YouTube extractor.

    Returns an item dict or None.
    """
    query = f"{track_meta.get('artist','')} - {track_meta.get('title','')}".strip(" -")
    if not query:
        return None

    if not _init_spotdl():
        print(f"[SC→YT fallback] spotdl not available")
        return None

    print(f"[SC→YT fallback] spotdl search: {query}")

    try:
        from spotdl.types.song import Song as _SpotdlSong
        from spotdl.download.downloader import Downloader as _SpotdlDownloader
        from spotdl.types.options import DownloaderOptions as _SpotdlOpts

        song = _SpotdlSong.from_search_term(query)

        settings = {
            "format": "mp3", "bitrate": "192k",
            "output": os.path.join(workdir, "{artists} - {title}.{output-ext}"),
            "cookie_file": COOKIES_PATH if COOKIES_AVAILABLE else None,
            "ffmpeg": "ffmpeg", "log_level": "ERROR", "simple_tui": True,
            "audio_providers": SPOTDL_AUDIO_PROVIDERS,
            "yt_dlp_args": "--retries 1 --fragment-retries 1 --extractor-retries 1 --socket-timeout 20",
        }
        opts = _SpotdlOpts(settings)
        dl = _SpotdlDownloader(opts)
        result_song, path = dl.search_and_download(song)

        if not path or not os.path.exists(str(path)):
            print(f"[SC→YT fallback] spotdl returned no path")
            return None

        fp = str(path)
        file_size = os.path.getsize(fp)
        print(f"[SC→YT fallback] OK: {human_size(file_size)}")

        # Size guard
        if file_size > 45 * 1024 * 1024:
            for lower_br in ["192", "128", "96"]:
                if _sp_reencode_mp3(fp, bitrate=lower_br):
                    file_size = os.path.getsize(fp)
                    if file_size <= 45 * 1024 * 1024:
                        break

        # Tag with SoundCloud metadata + cover
        cover_url = track_meta.get("thumb") or track_meta.get("cover")
        _sp_tag_mp3(fp, track_meta, cover_url=cover_url)

        # Rename
        safe_artist = sanitize_name(track_meta.get("artist") or "Unknown")
        safe_title = sanitize_name(track_meta.get("title") or "Unknown")
        new_fp = os.path.join(workdir, f"{safe_artist} - {safe_title}.mp3")
        try:
            if os.path.abspath(fp) != os.path.abspath(new_fp):
                if os.path.exists(new_fp):
                    os.remove(new_fp)
                os.rename(fp, new_fp)
            fp = new_fp
        except Exception:
            pass

        thumb_file = ""
        if cover_url:
            thumb_file = file_processor.download_thumb(cover_url, workdir)

        return {
            "filepath": fp,
            "title": track_meta.get("title") or "Unknown",
            "artist": track_meta.get("artist") or "Unknown",
            "album": track_meta.get("album"),
            "size": os.path.getsize(fp),
            "duration": track_meta.get("duration") or 0,
            "thumb_file": thumb_file,
            "ext": "mp3",
            "cover": cover_url,
        }
    except Exception as e:
        print(f"[SC→YT fallback] failed: {type(e).__name__}: {str(e)[:150]}")
        return None

# ── 4.4  Spotify metadata module (spotapi: _SpPublic, _SpPublicAlbum, _SpPublicPlaylist imports, helpers, track/album/playlist info) ────
# ===== Spotify Module (spotapi metadata + audio via YouTube/SoundCloud search) =====
# spotapi returns public metadata only (no audio). We get the real audio by
# searching "artist - title" on YouTube (best coverage) and, if that fails,
# falling back to SoundCloud search. The result is converted to mp3 and tagged
# with the Spotify cover art + metadata.
try:
    from spotapi import Public as _SpPublic, PublicAlbum as _SpPublicAlbum, PublicPlaylist as _SpPublicPlaylist
except Exception as _sp_err:
    print(f"[Spotify] spotapi import failed, Spotify disabled: {_sp_err}")
    SPOTIFY_ENABLED = False
def _sp_largest_image(sources):
    """Pick the highest-resolution image url from a Spotify sources list."""
    if not sources:
        return None
    try:
        best = max(sources, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
        return best.get("url")
    except Exception:
        return sources[0].get("url") if sources else None
def _sp_artist_names(artists_obj):
    items = (artists_obj or {}).get("items") or []
    names = []
    for a in items:
        n = (a.get("profile") or {}).get("name")
        if n:
            names.append(n)
    return names
def _sp_dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out
def extract_spotify_id(url):
    """Return (kind, id) where kind is track/album/playlist, or (None, None)."""
    for kind, pat in [
        ("track", r"spotify\.com/track/([a-zA-Z0-9]+)"),
        ("album", r"spotify\.com/album/([a-zA-Z0-9]+)"),
        ("playlist", r"spotify\.com/playlist/([a-zA-Z0-9]+)"),
    ]:
        m = re.search(pat, url)
        if m:
            return kind, m.group(1)
    m = re.search(r"spotify:(track|album|playlist):([a-zA-Z0-9]+)", url)
    if m:
        return m.group(1), m.group(2)
    return None, None
def spotify_search_tracks(query, limit=15):
    """Search Spotify for tracks using spotapi Public.song_search().

    Returns a list of dicts: {title, artist, id, url, duration, cover, thumb, album}.
    """
    try:
        results = []
        for batch in _SpPublic.song_search(query):
            if isinstance(batch, list):
                for item_wrapper in batch[:limit]:
                    item = (item_wrapper.get("item") or {}).get("data") or {}
                    if not item.get("name"):
                        continue
                    uri = item.get("uri") or ""
                    tid = uri.split(":")[-1] if uri.startswith("spotify:track:") else None
                    if not tid:
                        continue
                    artists_obj = item.get("artists") or {}
                    artist_items = artists_obj.get("items") or []
                    artist_names = [a.get("profile", {}).get("name") for a in artist_items if a.get("profile", {}).get("name")]
                    artist = ", ".join(artist_names) if artist_names else "Unknown"
                    album_obj = item.get("albumOfTrack") or {}
                    cover_sources = (album_obj.get("coverArt") or {}).get("sources") or []
                    cover = _sp_largest_image(cover_sources)
                    duration_ms = (item.get("duration") or {}).get("totalMilliseconds", 0)
                    results.append({
                        "title": item.get("name") or "Unknown",
                        "artist": artist,
                        "id": tid,
                        "url": f"https://open.spotify.com/track/{tid}",
                        "duration": int(duration_ms // 1000),
                        "cover": cover,
                        "thumb": cover,
                        "album": album_obj.get("name"),
                    })
                    if len(results) >= limit:
                        break
            break  # only first batch
        return results
    except Exception as e:
        print(f"[Spotify search] error: {e}")
        return []
def _sp_extract_date_fields(obj):
    year, date = extract_release_from_meta(obj or {})
    if year:
        return year, date
    nested = (obj or {}).get("date") or (obj or {}).get("releaseDate") or {}
    if isinstance(nested, dict):
        return extract_release_from_meta(nested)
    return None, None

def spotify_track_info(track_id):
    """Fetch single track metadata via spotapi."""
    data = _SpPublic.song_info(track_id)
    tu = data["data"]["trackUnion"]
    artists = _sp_dedupe(_sp_artist_names(tu.get("firstArtist")) + _sp_artist_names(tu.get("otherArtists")))
    aot = tu.get("albumOfTrack") or {}
    cover = _sp_largest_image((aot.get("coverArt") or {}).get("sources"))
    year, date = _sp_extract_date_fields(aot)
    if not year:
        year, date = _sp_extract_date_fields(tu)
    return {
        "title": tu.get("name") or "Unknown",
        "artist": ", ".join(artists) if artists else "Unknown",
        "artists": artists,
        "album": aot.get("name"),
        "album_id": aot.get("id") or aot.get("uri", "").split(":")[-1] if aot.get("uri") else None,
        "duration": int((tu.get("duration") or {}).get("totalMilliseconds", 0) // 1000),
        "cover": cover,
        "track_number": tu.get("trackNumber") or tu.get("track_number"),
        "disc_number": tu.get("discNumber") or tu.get("disc_number"),
        "year": year,
        "release_date": date,
        "id": track_id,
        "url": f"https://open.spotify.com/track/{track_id}",
        "kind": "track",
    }
def _sp_album_item_to_track(item, fallback_cover, album_name=None, album_year=None, album_date=None):
    t = item.get("track") or item
    arts = _sp_dedupe(_sp_artist_names(t.get("artists")))
    uri = t.get("uri") or ""
    tid = uri.split(":")[-1] if uri.startswith("spotify:track:") else None
    year, date = _sp_extract_date_fields(t.get("albumOfTrack") or {})
    if not year:
        year, date = album_year, album_date
    return {
        "title": t.get("name") or "Unknown",
        "artist": ", ".join(arts) if arts else "Unknown",
        "artists": arts,
        "duration": int((t.get("duration") or {}).get("totalMilliseconds", 0) // 1000),
        "track_number": t.get("trackNumber"),
        "disc_number": t.get("discNumber") or t.get("disc_number"),
        "uri": uri,
        "id": tid,
        "url": f"https://open.spotify.com/track/{tid}" if tid else None,
        "cover": _sp_largest_image(((t.get("albumOfTrack") or {}).get("coverArt") or {}).get("sources")) or fallback_cover,
        "album": album_name,
        "year": year,
        "release_date": date,
    }
def spotify_album_info(album_id):
    """Fetch album metadata + full track list via spotapi."""
    album = _SpPublicAlbum(album_id)
    info = album.get_album_info(limit=50)
    au = info["data"]["albumUnion"]
    cover = _sp_largest_image((au.get("coverArt") or {}).get("sources"))
    artists = _sp_dedupe(_sp_artist_names(au.get("artists")))
    album_name = au.get("name") or "Unknown Album"
    year, date = _sp_extract_date_fields(au)
    tracks = []
    for chunk in album.paginate_album():
        if isinstance(chunk, list):
            for it in chunk:
                tracks.append(_sp_album_item_to_track(it, cover, album_name, year, date))
        else:
            tracks.append(_sp_album_item_to_track(chunk, cover, album_name, year, date))
    return {
        "title": album_name,
        "artist": ", ".join(artists) if artists else "Unknown",
        "artists": artists,
        "cover": cover,
        "label": au.get("label"),
        "year": year,
        "release_date": date,
        "tracks": tracks,
        "id": album_id,
        "url": f"https://open.spotify.com/album/{album_id}",
        "kind": "album",
    }
def _sp_playlist_item_to_track(item, fallback_cover):
    data = (item.get("itemV2") or {}).get("data") or {}
    if not data or data.get("__typename") != "Track":
        return None
    arts = _sp_dedupe(_sp_artist_names(data.get("artists")))
    aot = data.get("albumOfTrack") or {}
    uri = data.get("uri") or ""
    tid = uri.split(":")[-1] if uri.startswith("spotify:track:") else None
    year, date = _sp_extract_date_fields(aot)
    if not year:
        year, date = _sp_extract_date_fields(data)
    return {
        "title": data.get("name") or "Unknown",
        "artist": ", ".join(arts) if arts else "Unknown",
        "artists": arts,
        "album": aot.get("name"),
        "duration": int((data.get("trackDuration") or {}).get("totalMilliseconds", 0) // 1000),
        "track_number": data.get("trackNumber"),
        "disc_number": data.get("discNumber") or data.get("disc_number"),
        "uri": uri,
        "id": tid,
        "url": f"https://open.spotify.com/track/{tid}" if tid else None,
        "cover": _sp_largest_image((aot.get("coverArt") or {}).get("sources")) or fallback_cover,
        "year": year,
        "release_date": date,
    }
def spotify_playlist_info(playlist_id):
    """Fetch playlist metadata + full track list via spotapi."""
    pl = _SpPublicPlaylist(playlist_id)
    info = pl.get_playlist_info(limit=50)
    pv = info["data"]["playlistV2"]
    cover = None
    img_items = (pv.get("images") or {}).get("items") or []
    if img_items:
        cover = _sp_largest_image(img_items[0].get("sources"))
    owner = ((pv.get("ownerV2") or {}).get("data") or {}).get("name")
    desc = pv.get("description")
    if isinstance(desc, dict):
        desc = desc.get("text")
    tracks = []
    for chunk in pl.paginate_playlist():
        items = chunk.get("items") if isinstance(chunk, dict) else chunk
        if not items:
            continue
        for it in items:
            tr = _sp_playlist_item_to_track(it, cover)
            if tr:
                tracks.append(tr)
    return {
        "title": pv.get("name") or "Unknown Playlist",
        "owner": owner,
        "cover": cover,
        "description": desc,
        "tracks": tracks,
        "id": playlist_id,
        "url": f"https://open.spotify.com/playlist/{playlist_id}",
        "kind": "playlist",
    }
def _sp_tag_mp3(filepath, track_meta, cover_url=None):
    """Embed Spotify metadata + cover into an mp3 file."""
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, TYER, TDRL, TPOS, APIC
        try:
            id3 = ID3(filepath)
        except Exception:
            id3 = ID3()
        id3.add(TIT2(encoding=3, text=track_meta.get("title") or "Unknown"))
        id3.add(TPE1(encoding=3, text=track_meta.get("artist") or "Unknown"))
        if track_meta.get("album"):
            id3.add(TALB(encoding=3, text=track_meta["album"]))
        if track_meta.get("track_number"):
            disc = track_meta.get("disc_number")
            trck = str(track_meta["track_number"])
            if disc:
                trck = f"{trck}"
            id3.add(TRCK(encoding=3, text=trck))
        if track_meta.get("disc_number"):
            id3.add(TPOS(encoding=3, text=str(track_meta["disc_number"])))
        year, date = extract_release_from_meta(track_meta)
        if not year:
            year = track_meta.get("year")
            date = track_meta.get("release_date") or track_meta.get("date") or year
        if date:
            id3.add(TDRC(encoding=3, text=str(date)))
            id3.add(TDRL(encoding=3, text=str(date)))
        if year:
            id3.add(TYER(encoding=3, text=str(year)))
        if cover_url:
            try:
                img = requests.get(cover_url, timeout=15).content
                if img:
                    id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img))
            except Exception:
                pass
        id3.save(filepath)
    except Exception as e:
        print(f"[Spotify] tag error: {e}")

# ── 4.5  spotdl init + download_spotify_track_audio (cache fast-path + YT Music primary + spotdl + verified SC search + last resort) ────
# ===== spotdl initialization (lazy, once) =====
_SPOTDL_READY = False
def _init_spotdl():
    """Initialize spotdl's SpotifyClient once. Returns True on success."""
    global _SPOTDL_READY
    if _SPOTDL_READY:
        return True
    try:
        from spotdl.utils.spotify import SpotifyClient
        from spotdl.utils.config import DEFAULT_CONFIG as _SPOTDL_CFG
        try:
            SpotifyClient.init(
                client_id=_SPOTDL_CFG["client_id"],
                client_secret=_SPOTDL_CFG["client_secret"],
                user_auth=False, no_cache=True, headless=True,
            )
        except Exception as init_err:
            if "already" in str(init_err).lower():
                # Already initialized from a previous call — that's fine
                pass
            else:
                raise
        _SPOTDL_READY = True
        return True
    except Exception as e:
        print(f"[Spotify] spotdl init failed: {e}")
        return False
def _sp_reencode_mp3(filepath, bitrate="128"):
    """Re-encode an mp3 at a lower bitrate to shrink it under Telegram's limit."""
    try:
        import subprocess
        tmp = filepath + ".reenc.mp3"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-b:a", f"{bitrate}k", "-map", "a", tmp],
            capture_output=True, timeout=180
        )
        if result.returncode == 0 and os.path.exists(tmp):
            new_size = os.path.getsize(tmp)
            old_size = os.path.getsize(filepath)
            if new_size < old_size:
                os.replace(tmp, filepath)
                return filepath
        if os.path.exists(tmp):
            os.remove(tmp)
        return None
    except Exception as e:
        print(f"[Spotify] reencode error: {e}")
        return None
def download_spotify_track_audio(track_meta, workdir, progress_hook=None, chat_id=None):
    """Download a Spotify track's audio.

    Download priority chain:
      1. YouTube Music (ytmusicapi search + yt-dlp + deno) — PRIMARY, most
         reliable. Title+artist+duration scoring rejects wrong matches.
      2. Verified SoundCloud search (yt-dlp scsearch + rapidfuzz scoring +
         premium-preview guard) — PRIORITY 2. Same strict scoring as step 1.
      3. spotdl YouTube-only (Song.from_url → Song.from_search_term) —
         PRIORITY 3 / LAST RESORT. Only the "youtube" provider is used, so
         spotdl's internal loop stays tight and can't pick SoundCloud/bandcamp
         mismatches. Track-id verification prevents spotdl's search-term mode
         from picking a different song that shares the name.

    Returns an item dict or None on failure.
    """
    if not _init_spotdl():
        print("[Spotify] spotdl not initialized, cannot download")
        return None

    track_id = track_meta.get("id")
    # track_id may be absent (e.g. when re-downloading via cached metadata only).
    # In that case we skip spotdl's Song.from_url step (which needs the Spotify
    # track URL) and rely on YT Music + SoundCloud direct search. We use a
    # sentinel so the track_url construction below doesn't crash.
    if not track_id:
        print("[Spotify] no track id in metadata — will skip spotdl Song.from_url")
        track_id = "_no_spotify_id_"

    bitrate = SPOTIFY_AUDIO_BITRATE
    if chat_id is not None:
        user_q = get_platform_quality(chat_id, "spotify")
        if user_q in ("320", "192", "128"):
            bitrate = user_q
    bitrate_k = f"{bitrate}k"

    SIZE_GUARD = 45 * 1024 * 1024
    track_url = f"https://open.spotify.com/track/{track_id}"
    query_label = f"{track_meta.get('artist','?')} - {track_meta.get('title','?')}"

    def _process_spotdl_result(fp, source_type=None, source_url=None):
        """Shared post-download processing: size guard, tag, rename, build item dict."""
        fp = str(fp)
        file_size = os.path.getsize(fp)
        print(f"[Spotify] spotdl OK: {human_size(file_size)}")

        # Size guard — re-encode at lower bitrate if file is too big for Telegram
        if file_size > SIZE_GUARD:
            chain = []
            if bitrate == "320": chain = ["192", "128", "96"]
            elif bitrate == "192": chain = ["128", "96"]
            elif bitrate == "128": chain = ["96"]
            else: chain = ["96"]
            for lower_br in chain:
                if _sp_reencode_mp3(fp, bitrate=lower_br):
                    file_size = os.path.getsize(fp)
                    if file_size <= SIZE_GUARD:
                        break

        # Re-tag with spotapi metadata (so the file carries the correct Spotify tags)
        _sp_tag_mp3(fp, track_meta, cover_url=track_meta.get("cover"))

        # Rename to a clean "Artist - Title.mp3"
        safe_artist = sanitize_name(track_meta.get("artist") or "Unknown")
        safe_title = sanitize_name(track_meta.get("title") or "Unknown")
        new_fp = os.path.join(workdir, f"{safe_artist} - {safe_title}.mp3")
        try:
            if os.path.abspath(fp) != os.path.abspath(new_fp):
                if os.path.exists(new_fp):
                    os.remove(new_fp)
                os.rename(fp, new_fp)
            fp = new_fp
        except Exception:
            pass

        thumb_file = ""
        if track_meta.get("cover"):
            thumb_file = file_processor.download_thumb(track_meta["cover"], workdir)

        year, date = extract_release_from_meta(track_meta)
        return {
            "filepath": fp,
            "title": track_meta.get("title") or "Unknown",
            "artist": track_meta.get("artist") or "Unknown",
            "album": track_meta.get("album"),
            "size": os.path.getsize(fp),
            "duration": track_meta.get("duration") or 0,
            "thumb_file": thumb_file,
            "ext": "mp3",
            "cover": track_meta.get("cover"),
            "id": track_meta.get("id"),
            "url": track_meta.get("url"),
            "track_number": track_meta.get("track_number"),
            "year": year or track_meta.get("year"),
            "release_date": date or track_meta.get("release_date"),
        }

    def _try_spotdl(song_obj, providers, label=""):
        """Try to download a song with given providers. Returns item dict or None.

        Robust to provider init failures: spotdl's Downloader.__init__ eagerly
        creates ALL audio providers. If one fails to initialize (e.g. SoundCloud
        returns HTTP 403 on client_id generation), the whole downloader dies —
        even though youtube-music would have worked. To avoid that, we try
        progressively smaller subsets of providers (largest first) until init
        succeeds. The actual download is then attempted once.
        """
        from spotdl.download.downloader import Downloader as _SpotdlDownloader
        from spotdl.types.options import DownloaderOptions as _SpotdlOpts

        # Build subsets: [all], [all but last], [all but last two], ...
        # The flakier providers (soundcloud, bandcamp) sit at the end of the
        # default list, so we trim from the end first.
        subsets = []
        cur = list(providers)
        while cur:
            subsets.append(list(cur))
            cur = cur[:-1]

        dl = None
        used_ps = None
        for ps in subsets:
            try:
                settings = {
                    "format": "mp3", "bitrate": bitrate_k,
                    "output": os.path.join(workdir, "{artists} - {title}.{output-ext}"),
                    "cookie_file": COOKIES_PATH if COOKIES_AVAILABLE else None,
                    "ffmpeg": "ffmpeg", "log_level": "ERROR", "simple_tui": True,
                    "audio_providers": ps,
                    "yt_dlp_args": "--retries 1 --fragment-retries 1 --extractor-retries 1 --socket-timeout 20",
                }
                opts = _SpotdlOpts(settings)
                dl = _SpotdlDownloader(opts)
                used_ps = ps
                break  # init succeeded
            except Exception as e:
                if len(ps) > 1:
                    print(f"[Spotify] spotdl{label} init failed with {ps} "
                          f"({type(e).__name__}: {str(e)[:80]}), retrying with fewer providers")
                    continue
                print(f"[Spotify] spotdl{label} init failed (last subset {ps}): "
                      f"{type(e).__name__}: {str(e)[:150]}")
                return None

        if dl is None:
            return None

        # Init succeeded — now attempt the actual download (single try)
        try:
            print(f"[Spotify] spotdl{label} (providers={used_ps}): {query_label}")
            result_song, path = dl.search_and_download(song_obj)
            if path and os.path.exists(str(path)):
                return _process_spotdl_result(path)
            else:
                print(f"[Spotify] spotdl{label} returned no path for '{query_label}'")
                return None
        except Exception as e:
            print(f"[Spotify] spotdl{label} failed for '{query_label}': "
                  f"{type(e).__name__}: {str(e)[:150]}")
            return None


    # === Step 1: Direct YouTube Music (PRIMARY — most reliable) ===
    # ytmusicapi search bypasses bot-check; yt-dlp + deno solves n-challenge.
    # We score each result on title + artist similarity + duration match, and
    # ONLY accept it if it's clearly the SAME song. If YT Music returns
    # unrelated songs (e.g. the track isn't on YT Music), we fail honestly
    # and fall through to spotdl / SoundCloud — instead of downloading a
    # wrong song and tagging it as the wanted one (the bug the user reported).
    try:
        from ytmusicapi import YTMusic
        from rapidfuzz import fuzz as _fuzz
        ytm_query = f"{track_meta.get('artist','')} - {track_meta.get('title','')}".strip(" -")
        if ytm_query:
            print(f"[Spotify] YouTube Music search: {ytm_query}")
            yt_api = YTMusic()
            results = yt_api.search(ytm_query, filter="songs", limit=5)
            if results:
                sp_title = (track_meta.get("title") or "").lower().strip()
                sp_artist = (track_meta.get("artist") or "").lower().strip()
                # split multi-artist "A, B" → take first for matching
                sp_artist_first = sp_artist.split(",")[0].strip() if sp_artist else ""
                target_dur = track_meta.get("duration") or 0

                scored = []
                for r in results:
                    r_title = (r.get("title") or "").lower().strip()
                    # artists is a list of {name, id}; join names
                    r_artists_list = [a.get("name", "") for a in (r.get("artists") or []) if a.get("name")]
                    r_artist = " ".join(r_artists_list).lower().strip()
                    r_dur = r.get("duration_seconds", 0) or 0

                    title_score = _fuzz.token_set_ratio(sp_title, r_title) if sp_title and r_title else 0
                    # artist match: prefer matching the primary artist
                    if sp_artist_first and r_artist:
                        artist_score = _fuzz.token_set_ratio(sp_artist_first, r_artist)
                    else:
                        artist_score = 50.0
                    # duration: 100 if exact, 0 if off by >30s, linear in between
                    if target_dur > 0 and r_dur > 0:
                        diff = abs(target_dur - r_dur)
                        dur_score = max(0.0, 100 - (diff * 100.0 / 30)) if diff < 30 else 0.0
                    else:
                        dur_score = 50.0
                    # weighted: title 50% / artist 35% / duration 15%
                    total = title_score * 0.5 + artist_score * 0.35 + dur_score * 0.15
                    scored.append((total, title_score, artist_score, dur_score, r))

                scored.sort(key=lambda x: x[0], reverse=True)

                # Thresholds — reject obvious mismatches (different songs sharing
                # a word, or YT Music returning other songs by the same artist
                # when the wanted track isn't there).
                MIN_TITLE_SCORE = 80.0
                MIN_ARTIST_SCORE = 60.0
                MIN_TOTAL_SCORE = 75.0
                accepted = [(t, r) for (t, ts, as_, ds, r) in scored
                            if ts >= MIN_TITLE_SCORE and as_ >= MIN_ARTIST_SCORE and t >= MIN_TOTAL_SCORE]

                if not accepted:
                    if scored:
                        b = scored[0]
                        print(f"[Spotify] YT Music: no result matched closely enough "
                              f"(best: title={b[1]:.0f} artist={b[2]:.0f} dur={b[3]:.0f} "
                              f"total={b[0]:.0f}) — failing honestly, will try spotdl/SoundCloud")
                    else:
                        print(f"[Spotify] YT Music: no scored candidates")
                else:
                    best_total, best_entry = accepted[0]
                    video_id = best_entry.get("videoId", "")
                    if video_id:
                        yt_url = f"https://music.youtube.com/watch?v={video_id}"
                        r_arts = ", ".join(a.get("name","") for a in (best_entry.get("artists") or []))
                        print(f"[Spotify] YT Music picked: {best_entry.get('title','?')[:40]} "
                              f"by {r_arts[:30]} (dur={best_entry.get('duration_seconds',0)}s vs target {target_dur}s, "
                              f"score={best_total:.0f})")
                        yt_opts = {
                            "format": "bestaudio/best",
                            "outtmpl": os.path.join(workdir, "%(title).80s.%(ext)s"),
                            "quiet": True, "no_warnings": True,
                            "socket_timeout": 30, "retries": 1,
                            "ignoreerrors": True,
                            "cookiefile": COOKIES_PATH if COOKIES_AVAILABLE else None,
                            "postprocessors": [
                                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": bitrate},
                            ],
                        }
                        yt_opts.update(_deno_opts())
                        with yt_dlp.YoutubeDL(yt_opts) as ydl:
                            ydl.extract_info(yt_url, download=True)
                        mp3_files = [f for f in os.listdir(workdir) if f.lower().endswith(".mp3")]
                        if mp3_files:
                            fp = os.path.join(workdir, sorted(mp3_files, key=lambda f: os.path.getmtime(os.path.join(workdir, f)))[-1])
                            file_size = os.path.getsize(fp)
                            print(f"[Spotify] YT Music OK: {human_size(file_size)}")
                            return _process_spotdl_result(fp)
                        else:
                            print(f"[Spotify] YT Music: no mp3 produced")
            else:
                print(f"[Spotify] YT Music: no results")
    except Exception as e:
        print(f"[Spotify] YT Music direct failed: {type(e).__name__}: {str(e)[:150]}")

    # === Step 2: Verified SoundCloud search via yt-dlp (PRIORITY 2) ===
    # Strong as our YT Music step: title + artist similarity scoring with
    # rapidfuzz + duration match + premium-preview guard. We probe up to 5
    # candidates, reject obvious mismatches, then download the best that
    # passes the threshold. This is the fallback when YT Music can't find
    # the exact track (e.g. the song isn't on YouTube at all).
    sc_query = f"{track_meta.get('artist','')} - {track_meta.get('title','')}".strip(" -")
    if sc_query:
        print(f"[Spotify] SoundCloud search (verified): {sc_query}")
        sc_probe_opts = {
            "format": "bestaudio/best",
            "quiet": True, "no_warnings": True,
            "default_search": "scsearch", "noplaylist": True,
            "socket_timeout": 20, "retries": 0, "extractor_retries": 1,
            "ignoreerrors": True, "nocheckcertificate": True,
            "logger": _SilentLogger(),
        }
        # deno is harmless for SoundCloud; required if a YouTube result sneaks in
        sc_probe_opts.update(_deno_opts())
        if COOKIES_AVAILABLE:
            sc_probe_opts["cookiefile"] = COOKIES_PATH

        try:
            # Phase 1: probe search — get candidate list WITHOUT downloading
            with yt_dlp.YoutubeDL(sc_probe_opts) as ydl_probe:
                search_info = ydl_probe.extract_info(f"scsearch5:{sc_query}", download=False)
            if not search_info:
                # SoundCloud can return None when it's blocking the request (403/429)
                print(f"[Spotify] SoundCloud search: no response for '{sc_query}' — failing honestly")
                candidates = []
            else:
                candidates = [e for e in (search_info.get("entries") or []) if e]
            if not candidates:
                if search_info is not None:
                    print(f"[Spotify] SoundCloud search: no results for '{sc_query}'")
            else:
                # Phase 2: score each candidate against the Spotify metadata
                # (same scoring approach as the YT Music step: title + artist + duration)
                from rapidfuzz import fuzz as _fuzz
                sp_title = (track_meta.get("title") or "").lower().strip()
                sp_artist = (track_meta.get("artist") or "").lower().strip()
                sp_artist_first = sp_artist.split(",")[0].strip() if sp_artist else ""
                sp_duration = track_meta.get("duration") or 0

                scored = []
                for c in candidates:
                    c_title = (c.get("title") or "").lower().strip()
                    c_artist = (extract_artist(c) or "").lower().strip()
                    c_duration = c.get("duration") or 0

                    title_score = _fuzz.token_set_ratio(sp_title, c_title) if sp_title and c_title else 0
                    # artist match: prefer matching the primary artist
                    if sp_artist_first and c_artist:
                        artist_score = _fuzz.token_set_ratio(sp_artist_first, c_artist)
                    else:
                        artist_score = 50.0
                    # duration: 100 if exact, 0 if off by >30s, linear in between
                    if sp_duration > 0 and c_duration > 0:
                        diff = abs(sp_duration - c_duration)
                        dur_score = max(0.0, 100 - (diff * 100.0 / 30)) if diff < 30 else 0.0
                    else:
                        dur_score = 50.0
                    # weighted: title 50% / artist 35% / duration 15% (same as YT Music step)
                    total = title_score * 0.5 + artist_score * 0.35 + dur_score * 0.15
                    scored.append((total, title_score, artist_score, dur_score, c))

                scored.sort(key=lambda x: x[0], reverse=True)

                # Thresholds — strict, matching the YT Music step's logic.
                # Reject remixes/covers/different-songs sharing a word.
                MIN_TITLE_SCORE = 80.0
                MIN_ARTIST_SCORE = 60.0
                MIN_TOTAL_SCORE = 75.0
                accepted = [(t, c) for (t, ts, as_, ds, c) in scored
                            if ts >= MIN_TITLE_SCORE and as_ >= MIN_ARTIST_SCORE and t >= MIN_TOTAL_SCORE]

                if not accepted:
                    if scored:
                        b = scored[0]
                        print(f"[Spotify] SoundCloud search: no result matched '{sc_query}' "
                              f"closely enough — best candidate "
                              f"(title={b[1]:.0f} artist={b[2]:.0f} dur={b[3]:.0f} "
                              f"total={b[0]:.0f}) — failing honestly")
                    else:
                        print(f"[Spotify] SoundCloud search: no scored candidates — failing honestly")
                else:
                    # Phase 3: download each accepted candidate (best score first),
                    # skipping DRM-protected ones and 30s premium previews.
                    sc_dl_opts = dict(sc_probe_opts)
                    sc_dl_opts["outtmpl"] = os.path.join(workdir, "%(title).80s.%(ext)s")
                    sc_dl_opts["postprocessors"] = [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": bitrate},
                    ]
                    downloaded = False
                    for total, c in accepted:
                        c_url = c.get("webpage_url") or c.get("url")
                        if not c_url:
                            continue
                        print(f"[Spotify] SoundCloud trying: {(c.get('title') or '?')[:50]} "
                              f"(score={total:.0f})")
                        # clear any partial files from a previous attempt
                        for f in os.listdir(workdir):
                            try: os.remove(os.path.join(workdir, f))
                            except Exception: pass
                        try:
                            with yt_dlp.YoutubeDL(sc_dl_opts) as ydl_dl:
                                ydl_dl.extract_info(c_url, download=True)
                            mp3_files = [f for f in os.listdir(workdir) if f.lower().endswith(".mp3")]
                            if mp3_files:
                                fp = os.path.join(workdir, sorted(
                                    mp3_files, key=lambda f: os.path.getmtime(os.path.join(workdir, f)))[-1])
                                # === Premium preview guard ===
                                # SoundCloud Go+ tracks download only ~30s without error.
                                # If the actual audio is ≤31s but the Spotify track is >40s,
                                # this is a preview — skip it and try the next candidate.
                                _probe_item = {"filepath": fp, "duration": track_meta.get("duration") or 0}
                                if _is_sc_premium_preview(_probe_item, track_meta.get("duration")):
                                    print(f"[Spotify] SoundCloud candidate is a 30s premium preview — "
                                          f"skipping: {(c.get('title') or '?')[:40]}")
                                    try: os.remove(fp)
                                    except Exception: pass
                                    continue
                                print(f"[Spotify] SoundCloud OK: {human_size(os.path.getsize(fp))}")
                                downloaded = True
                                return _process_spotdl_result(fp)
                        except Exception as e:
                            err_str = str(e).lower()
                            if "drm" in err_str or "drm protected" in err_str:
                                print(f"[Spotify] SoundCloud candidate DRM-protected, "
                                      f"skipping: {(c.get('title') or '?')[:40]}")
                                continue
                            print(f"[Spotify] SoundCloud candidate failed: "
                                  f"{type(e).__name__}: {str(e)[:80]}")
                            continue
                    if not downloaded:
                        print(f"[Spotify] SoundCloud search: all candidates failed "
                              f"(DRM or download error) — failing honestly")
        except Exception as e:
            print(f"[Spotify] SoundCloud search failed: {type(e).__name__}: {str(e)[:150]}")


    # === Step 3: spotdl as last resort, YouTube provider ONLY (PRIORITY 3) ===
    # spotdl is no longer the primary path — its internal yt-dlp config and
    # multi-provider loop added latency + sometimes picked wrong tracks. We
    # keep it ONLY as a YouTube-only fallback when both YT Music and
    # SoundCloud verified search failed. Two sub-steps:
    #   3a. Song.from_url(spotify_url) → search_and_download with ["youtube"]
    #   3b. Song.from_search_term(query) → search_and_download with ["youtube"]
    #       (only when we have a real Spotify track id; verified to avoid
    #       spotdl picking a DIFFERENT track that shares the name.)
    try:
        from spotdl.types.song import Song as _SpotdlSong
        # 3a: Song.from_url — needs a real Spotify track URL
        if track_id and track_id != "_no_spotify_id_":
            try:
                song = _SpotdlSong.from_url(track_url)
                print(f"[Spotify] spotdl YouTube-only (from_url): {song.artist} - {song.name} ({song.duration}s)")
                result = _try_spotdl(song, ["youtube"], " (youtube-only)")
                if result:
                    return result
            except Exception as e:
                print(f"[Spotify] spotdl Song.from_url failed: {type(e).__name__}: {e}")

        # 3b: Song.from_search_term — verified by track id when available
        lr_query = f"{track_meta.get('artist','')} - {track_meta.get('title','')}".strip(" -")
        print(f"[Spotify] last resort spotdl search (youtube-only, verified): {lr_query}")
        song2 = _SpotdlSong.from_search_term(lr_query)
        # Verify it's the SAME track we were asked to download (skip the check
        # when we don't have a real Spotify track id, e.g. re-download via cache).
        if track_id and track_id != "_no_spotify_id_" and song2.song_id and song2.song_id != track_id:
            print(f"[Spotify] spotdl picked a DIFFERENT track id "
                  f"(wanted={track_id}, got={song2.song_id})")
            print(f"[Spotify] last resort rejected wrong match for '{query_label}' — "
                  f"failing honestly")
        else:
            result = _try_spotdl(song2, ["youtube"], " (last resort, youtube-only)")
            if result:
                return result
    except Exception as e:
        print(f"[Spotify] last resort (spotdl youtube-only) failed: {type(e).__name__}: {str(e)[:150]}")

    print(f"[Spotify] ALL methods failed for '{query_label}'")
    return None

# ── 4.6  Pinterest downloader (download_pinterest_enhanced) ─────────────
# ===== Enhanced Pinterest Downloader =====
def download_pinterest_enhanced(url: str, workdir: str, progress_hook=None):
    """Enhanced Pinterest downloader with better error handling and multiple strategies"""
    print(f"Starting enhanced Pinterest download for: {url}")
    
    # Multiple download strategies
    strategies = [
        # Strategy 1: Direct yt-dlp with custom headers
        {
            "format": "best",
            "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
            "nocheckcertificate": True,
            "ignoreerrors": True,
            "extractor_retries": 3,
            "socket_timeout": 20,
        },
        # Strategy 2: Mobile user agent
        {
            "format": "best",
            "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
            },
            "nocheckcertificate": True,
            "ignoreerrors": True,
            "extractor_retries": 3,
        },
        # Strategy 3: Generic fallback
        {
            "format": "bestvideo+bestaudio/bestvideo/bestaudio/best",
            "outtmpl": os.path.join(workdir, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "nocheckcertificate": True,
            "ignoreerrors": True,
        }
    ]
    
    for i, opts in enumerate(strategies, 1):
        try:
            print(f"Trying Pinterest strategy {i}/{len(strategies)}")
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                
                if not info:
                    print(f"Strategy {i}: No info extracted")
                    continue
                
                info["_filename"] = ydl.prepare_filename(info)
                item = finalize_generic_item(info, workdir)
                
                if item:
                    print(f"Strategy {i}: Successfully downloaded Pinterest content")
                    return {"item": item, "ok": True}
                else:
                    print(f"Strategy {i}: Failed to finalize item")
                    continue
                    
        except Exception as e:
            print(f"Strategy {i} failed: {str(e)}")
            if i == len(strategies):
                return {"error": f"All Pinterest strategies failed. Last error: {str(e)}", "ok": False}
            continue
    
    return {"error": "All Pinterest download strategies failed", "ok": False}

# ── 4.7  Generic handlers/mergers for Instagram/TikTok/Twitter/etc. ─────
# YouTube Handlers, Statistics, and Core Functionality

# ===== YouTube Handler with Shorts Detection =====
def handle_download_youtube(chat_id, url):
    """Handle YouTube download with quality selection and Shorts detection"""
    lang = get_user_lang(chat_id) or "en"
    
    # Check if it's a YouTube Short
    is_short = is_youtube_short(url)
    
    # If URL detection is inconclusive, check video info
    if is_short is None:
        is_short = confirm_youtube_short(url)
    
    if is_short:
        print(f"YouTube Short detected: {url}")
        # Handle as YouTube Short
        msg = bot.send_message(chat_id, tr(chat_id, "youtube_shorts_detected"))
        msg_id = msg.message_id
        
        try:
            # Save URL for later use
            save_youtube_shorts_info(chat_id, url, True)
            
            # Create selection keyboard with new format
            kb = create_youtube_shorts_keyboard(chat_id)
            
            # Update message with selection options
            bot.edit_message_text(tr(chat_id, "youtube_shorts_prompt"), chat_id, msg_id, reply_markup=kb)
            
        except Exception as e:
            bot.edit_message_text(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
    else:
        print(f"Regular YouTube video detected: {url}")
        # Handle as regular YouTube video
        msg = bot.send_message(chat_id, tr(chat_id, "youtube_processing"))
        msg_id = msg.message_id
        
        try:
            # Get available qualities with merging
            qualities = get_youtube_qualities_with_merging(url, chat_id)
            
            if not qualities:
                bot.edit_message_text(tr(chat_id, "youtube_no_qualities"), chat_id, msg_id)
                return
            
            # Save URL for later use
            save_youtube_qualities(chat_id, url, qualities)
            
            # Create quality selection keyboard with new format
            kb = create_youtube_quality_keyboard(qualities, chat_id)
            
            # Update message with quality selection
            bot.edit_message_text(tr(chat_id, "youtube_quality_prompt"), chat_id, msg_id, reply_markup=kb)
            
        except Exception as e:
            bot.edit_message_text(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
# ===== Generic handlers for other platforms =====
def handle_download_pinterest(chat_id, url):
    handle_generic_download(chat_id, url, "Pinterest")
def handle_download_instagram(chat_id, url):
    handle_generic_download(chat_id, url, "Instagram")
def handle_download_tiktok(chat_id, url):
    handle_generic_download(chat_id, url, "TikTok")
def handle_download_twitter(chat_id, url):
    handle_generic_download(chat_id, url, "Twitter")
def handle_generic_download(chat_id, url, platform):
    """Generic download with optimized progress bar"""
    msg = bot.send_message(chat_id, tr(chat_id, "downloading"))
    msg_id = msg.message_id

    # Create progress bar instance
    progress_bar = ProgressBar(chat_id, msg_id)

    def hook(d):
        try:
            if d.get("status") == "downloading":
                done = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                progress_bar.update(done, total)
        except Exception as e:
            pass

    tmpdir = tempfile.mkdtemp(prefix="gendl_")
    try:
        res = download_generic(url, tmpdir, progress_hook=hook)
        if not res.get("ok"):
            safe_edit_message(tr(chat_id, "error", err=res.get("error", "failed")), chat_id, msg_id)
            return

        if "playlist" in res:
            for item in res["playlist"]:
                send_media_item(chat_id, item, platform, url)
        else:
            item = res["item"]
            send_media_item(chat_id, item, platform, url)
    except Exception as e:
        safe_edit_message(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
# ===== Generic download functions =====
def download_generic(url: str, workdir: str, progress_hook=None):
    """Generic download with smart platform detection"""
    if "pinterest.com" in url or "pin.it" in url:
        print("Detected Pinterest URL, using enhanced downloader")
        return download_pinterest_enhanced(url, workdir, progress_hook)

    opts = make_generic_opts(workdir, progress_hook=progress_hook)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if not info:
                return {"error": "No info extracted from URL", "ok": False}

            entries = info.get("entries")
            if entries and isinstance(entries, list):
                items = []
                for e in entries:
                    if e:
                        e["_filename"] = ydl.prepare_filename(e)
                        it = finalize_generic_item(e, workdir)
                        if it:
                            items.append(it)
                return {"playlist": items, "ok": True} if items else {"error": "No valid items found", "ok": False}
            else:
                info["_filename"] = ydl.prepare_filename(info)
                it = finalize_generic_item(info, workdir)
                if it:
                    return {"item": it, "ok": True}
                return {"error": "Failed to finalize item", "ok": False}
    except Exception as e:
        return {"error": str(e), "ok": False}

# ── 4.8  (old song_cache re-download helpers removed; media archive is PART 1.4) ────


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 5 — KEYBOARDS & MENUS                                        ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 5.1  lang_keyboard, join_keyboard, sc_quality_keyboard ──────────────
def join_keyboard(chat_id):
    lang = get_user_lang(chat_id) or "en"
    kb = InlineKeyboardMarkup()
    kb.row(btn(tr(chat_id, "join_came_btn"), url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}", style="primary"))
    return kb

def send_forced_join_prompt(chat_id):
    bot.send_message(
        chat_id,
        tr(chat_id, "must_join_start", chan=CHANNEL_USERNAME),
        reply_markup=join_keyboard(chat_id),
    )
def lang_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(
        btn("فارسی 🇮🇷", callback_data="start_lang:fa", style="primary"),
        btn("English 🇬🇧", callback_data="start_lang:en", style="primary"),
    )
    return kb
def sc_quality_keyboard(chat_id):
    lang = get_user_lang(chat_id) or "en"
    kb = InlineKeyboardMarkup()
    kb.row(
        btn(T[lang]["quality_high"], callback_data="quality:high", style="success"),
        btn(T[lang]["quality_low"], callback_data="quality:low", style="primary"),
    )
    return kb

# ── 5.2  Main menu (create_main_menu_keyboard, send_main_menu, send_main_messages, send_features_message) ────
# ===== Features message =====
def send_features_message(chat_id):
    lang = get_user_lang(chat_id)
    header = T[lang]["features_header"]
    lines = T[lang]["features_lines"]
    companion = T[lang]["companion_label"].format(id=COMPANION_ID)
    text = header + "\n" + "\n".join(lines) + "\n" + companion
    # Add a main-menu button so the user can navigate back
    kb = InlineKeyboardMarkup()
    kb.row(btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"))
    bot.send_message(chat_id, text, reply_markup=kb)
# ===== Main menu =====
def create_main_menu_keyboard(chat_id):
    """Main menu keyboard shown after /start (for returning users)."""
    kb = InlineKeyboardMarkup()
    kb.row(
        btn(tr(chat_id, "menu_settings"), callback_data="open_settings", style="primary"),
        btn(tr(chat_id, "menu_features"), callback_data="show_features", style="primary"),
    )
    kb.row(
        btn(tr(chat_id, "menu_stats"), callback_data="open_stats", style="primary"),
        btn(tr(chat_id, "menu_search"), callback_data="start_search", style="primary"),
    )
    return kb
def send_main_menu(chat_id, greeting=False):
    """Send the main menu (welcome + send-link prompt + menu buttons)."""
    lang = get_user_lang(chat_id) or "en"
    if greeting:
        if lang == "fa":
            welcome = f"👋 خوش اومدی به <b>{BOT_NICKNAME}</b>!\n\n"
        else:
            welcome = f"👋 Welcome to <b>{BOT_NICKNAME}</b>!\n\n"
    else:
        welcome = ""
    text = welcome + tr(chat_id, "menu_send_link") + "\n\n" + tr(chat_id, "menu_title")
    bot.send_message(chat_id, text, reply_markup=create_main_menu_keyboard(chat_id))
def send_main_messages(chat_id):
    """Send the main menu after language selection / joining."""
    send_main_menu(chat_id, greeting=True)

# ── 5.3  Settings (create_settings_keyboard, create_language_keyboard, create_quality_picker_keyboard, _platform_display_name, create_search_engine_keyboard) ────
# ===== Settings =====
def create_language_keyboard(chat_id=None):
    """Language picker (used by /lang and Settings → Language).
    Includes a back button if chat_id is provided (returning to settings).
    """
    kb = InlineKeyboardMarkup()
    kb.row(
        btn("فارسی 🇮🇷", callback_data="set_lang:fa", style="primary"),
        btn("English 🇬🇧", callback_data="set_lang:en", style="primary"),
    )
    if chat_id is not None:
        kb.row(btn(tr(chat_id, "menu_back"), callback_data="open_settings", style="primary"))
    return kb
def create_search_engine_keyboard(chat_id):
    """Search engine picker (Spotify / SoundCloud)."""
    current = get_search_engine(chat_id)
    kb = InlineKeyboardMarkup()
    kb.row(btn(tr(chat_id, "search_engine_spotify"), callback_data="set_search_engine:spotify",
              style="success" if current == "spotify" else "primary"))
    kb.row(btn(tr(chat_id, "search_engine_soundcloud"), callback_data="set_search_engine:soundcloud",
              style="success" if current == "soundcloud" else "primary"))
    kb.row(btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"))
    return kb
# Map platform keys to settings labels
_PLATFORM_SETTINGS = [
    ("sc",        "settings_sc_quality",     "setqual:sc:"),
    ("spotify",   "settings_spotify_quality", "setqual:spotify:"),
    ("ig",        "settings_ig_quality",     "setqual:ig:"),
    ("tt",        "settings_tt_quality",     "setqual:tt:"),
    ("pin",       "settings_pin_quality",    "setqual:pin:"),
    ("yt_shorts", "settings_shorts_quality", "setqual:yt_shorts:"),
]
def create_settings_keyboard(chat_id):
    """Settings main menu: language + search engine + per-platform quality + dedupe."""
    kb = InlineKeyboardMarkup()
    lang = get_user_lang(chat_id) or "en"
    # Language row
    cur_lang_label = "فارسی 🇮🇷" if lang == "fa" else "English 🇬🇧"
    kb.row(btn(f"{tr(chat_id, 'settings_language')}  ({cur_lang_label})", callback_data="open_lang", style="primary"))
    # Search engine row
    cur_engine = get_search_engine(chat_id)
    engine_label = "Spotify" if cur_engine == "spotify" else "SoundCloud"
    kb.row(btn(f"{tr(chat_id, 'search_engine_title')}  ({engine_label})", callback_data="open_search_engine", style="primary"))
    # Dedupe discography toggle row
    dedupe_on = get_dedupe_setting(chat_id)
    dedupe_state = "✅" if dedupe_on else "❌"
    kb.row(btn(f"{tr(chat_id, 'dedupe_toggle_label')}  ({dedupe_state})",
               callback_data="toggle_dedupe", style="success" if dedupe_on else "primary"))
    # Per-platform quality rows (2 per row)
    row = []
    for key, label_key, _prefix in _PLATFORM_SETTINGS:
        b = btn(tr(chat_id, label_key),
                callback_data=f"qual_section:{key}", style="primary")
        row.append(b)
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    # back to main menu + close
    kb.row(
        btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
        btn(tr(chat_id, "close_menu"), callback_data="settings_close", style="danger"),
    )
    return kb
def create_quality_picker_keyboard(chat_id, platform_key):
    """Quality picker for a single platform."""
    kb = InlineKeyboardMarkup()
    lang = get_user_lang(chat_id) or "en"
    options = PLATFORM_QUALITIES.get(platform_key, [])
    current = get_platform_quality(chat_id, platform_key)
    for value, fa_label, en_label, _rank in options:
        label = fa_label if lang == "fa" else en_label
        marker = " ✅" if value == current else ""
        style = "success" if value == current else "primary"
        kb.row(btn(f"{label}{marker}", callback_data=f"setqual:{platform_key}:{value}", style=style))
    # back to settings + main menu
    kb.row(
        btn(tr(chat_id, "menu_back"), callback_data="open_settings", style="primary"),
        btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
    )
    return kb
def _platform_display_name(platform_key, lang):
    """Human-readable platform name for prompts."""
    names = {
        "sc":        ("SoundCloud", "SoundCloud"),
        "spotify":   ("Spotify",    "Spotify"),
        "ig":        ("Instagram",  "Instagram"),
        "tt":        ("TikTok",     "TikTok"),
        "pin":       ("Pinterest",  "Pinterest"),
        "yt_shorts": ("YouTube Shorts", "YouTube Shorts"),
    }
    return names.get(platform_key, (platform_key, platform_key))[0 if lang == "fa" else 1]

# ── 5.4  Spotify keyboards (create_spotify_keyboard, create_spotify_search_keyboard) ────
def create_spotify_keyboard(chat_id, tracks, page=0, per_page=10, panel_id=None):
    """Spotify album/playlist keyboard with 4-button main menu + paginated picker.

    page=0 → main menu (track count + cancel + download all + select track)
    page>=1 → paginated track list with back button.
    panel_id scopes callbacks to THIS panel's track list (panel-state fix).
    """
    pid = panel_id or "_"
    kb = InlineKeyboardMarkup()
    if not tracks:
        return kb

    total = len(tracks)

    # ---- Main 4-button menu (page == 0) ----
    if page == 0:
        # Row 1: track count (info) + cancel (red)
        kb.row(
            btn(tr(chat_id, "album_track_count", count=total), callback_data="noop", style="normal"),
            btn(tr(chat_id, "album_cancel"), callback_data=f"sp_close:{pid}", style="danger"),
        )
        # Row 2: Download All (green)
        kb.row(btn(tr(chat_id, "album_download_all"), callback_data=f"sp_batch:{pid}", style="success"))
        # Row 3: Select Track (blue) → opens picker page 1
        kb.row(btn(tr(chat_id, "album_select_track"), callback_data=f"sp_pickermode:{pid}:1", style="primary"))
        return kb

    # ---- Picker mode (paginated track list) ----
    start_idx = (page - 1) * per_page
    end_idx = min(start_idx + per_page, total)

    for i in range(start_idx, end_idx):
        t = tracks[i]
        artist = t.get("artist") or "Unknown"
        title = t.get("title") or "Unknown"
        label = f"{i+1}. {artist} - {title}"
        kb.row(btn(label[:60], callback_data=f"sp_pick:{pid}:{i}", style="primary"))

    # navigation row
    nav = []
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > 1:
        nav.append(btn(tr(chat_id, "previous_page"), callback_data=f"sp_pickermode:{pid}:{page-1}", style="primary"))
    nav.append(btn(tr(chat_id, "page_number", page=page, total_pages=total_pages), callback_data="noop", style="normal"))
    if end_idx < total:
        nav.append(btn(tr(chat_id, "next_page"), callback_data=f"sp_pickermode:{pid}:{page+1}", style="primary"))
    if nav:
        kb.row(*nav)

    # back to main album menu + cancel
    kb.row(
        btn(tr(chat_id, "menu_back"), callback_data=f"sp_pickermode:{pid}:0", style="primary"),
        btn(tr(chat_id, "album_cancel"), callback_data=f"sp_close:{pid}", style="danger"),
    )
    return kb
def create_spotify_search_keyboard(chat_id, tracks, page=0, per_page=10, panel_id=None):
    """Paginated keyboard for Spotify search results (panel-scoped)."""
    pid = panel_id or "_"
    kb = InlineKeyboardMarkup()
    if not tracks:
        return kb
    total = len(tracks)
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total)

    for i in range(start_idx, end_idx):
        t = tracks[i]
        artist = t.get("artist") or "Unknown"
        title = t.get("title") or "Unknown"
        label = f"{i+1}. {artist} - {title}"
        kb.row(btn(label[:60], callback_data=f"sp_pick:{pid}:{i}", style="primary"))

    nav = []
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > 0:
        nav.append(btn(tr(chat_id, "previous_page"), callback_data=f"sp_search_page:{pid}:{page-1}", style="primary"))
    nav.append(btn(tr(chat_id, "page_number", page=page+1, total_pages=total_pages), callback_data="noop", style="normal"))
    if end_idx < total:
        nav.append(btn(tr(chat_id, "next_page"), callback_data=f"sp_search_page:{pid}:{page+1}", style="primary"))
    if nav:
        kb.row(*nav)
    kb.row(
        btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
        btn(tr(chat_id, "close_menu"), callback_data=f"sp_close:{pid}", style="danger"),
    )
    return kb

# ── 5.5  SoundCloud keyboards (create_sc_album_menu, create_paginated_keyboard) ────
def create_sc_album_menu(chat_id, track_count, panel_id=None):
    """SoundCloud album/playlist main 4-button menu (panel-scoped).

    Layout:
      Row 1: 📊 تعداد ترک‌ها: N  |  ❌ لغو
      Row 2: ⬇️ دانلود همه (سبز)
      Row 3: 🎵 انتخاب ترک (آبی)
    """
    pid = panel_id or "_"
    kb = InlineKeyboardMarkup()
    kb.row(
        btn(tr(chat_id, "album_track_count", count=track_count), callback_data="noop", style="normal"),
        btn(tr(chat_id, "album_cancel"), callback_data="sc_close", style="danger"),
    )
    kb.row(btn(tr(chat_id, "album_download_all"), callback_data="sc_batch", style="success"))
    kb.row(btn(tr(chat_id, "album_select_track"), callback_data=f"sc_pickermode:{pid}", style="primary"))
    return kb
def create_paginated_keyboard(choices, chat_id, page=0, per_page=15, prefix="search", panel_id=None):
    """Create paginated keyboard with colored buttons (panel-scoped).

    For prefix=='playlist' (SoundCloud) this is the *picker* view (track list
    with pagination + back to album menu). The main 4-button album menu is
    built by create_sc_album_menu() instead.
    panel_id scopes callbacks to THIS panel's track list (panel-state fix).
    """
    lang = get_user_lang(chat_id) or "en"
    pid = panel_id or "_"
    kb = InlineKeyboardMarkup()

    if not choices:
        return kb

    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(choices))

    for i in range(start_idx, end_idx):
        ch = choices[i]
        if prefix == "search":
            artist = ch.get("artist", "Unknown Artist")
            title = ch.get("title", "Unknown Title")
            label = f"{i+1}. {artist} - {title}"
            callback_data = f"pick:{pid}:{i}"
            style = "primary"
        elif prefix == "playlist":
            artist = ch.get("artist", "Unknown Artist")
            title = ch.get("title", "Unknown Title")
            label = f"🎵 {artist} - {title}"
            callback_data = f"playlist_pick:{pid}:{i}"
            style = "primary"
        else:
            title = ch.get("title", "Unknown Title")
            label = f"{i+1}. {title}"
            callback_data = f"pick:{pid}:{i}"
            style = "primary"

        kb.row(btn(label[:64], callback_data=callback_data, style=style))

    nav_row = []
    if page > 0:
        nav_row.append(btn(tr(chat_id, "previous_page"), callback_data=f"{prefix}_page:{pid}:{page-1}", style="primary"))

    total_pages = (len(choices) + per_page - 1) // per_page
    nav_row.append(btn(tr(chat_id, "page_number", page=page+1, total_pages=total_pages), callback_data="noop", style="normal"))

    if end_idx < len(choices):
        nav_row.append(btn(tr(chat_id, "next_page"), callback_data=f"{prefix}_page:{pid}:{page+1}", style="primary"))

    if nav_row:
        kb.row(*nav_row)

    # navigation footer per context
    if prefix == "playlist":
        kb.row(
            btn(tr(chat_id, "menu_back"), callback_data=f"sc_album_menu:{pid}", style="primary"),
            btn(tr(chat_id, "album_cancel"), callback_data="sc_close", style="danger"),
        )
    elif prefix == "search":
        kb.row(
            btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
            btn(tr(chat_id, "close_menu"), callback_data="sc_close", style="danger"),
        )

    return kb

# ── 5.6  Artist keyboards (create_artist_menu, create_artist_picker_keyboard, ARTIST_LARGE_THRESHOLD, ARTIST_COUNT_PRESETS, SPOTIFY_ARTIST_MAX_ALBUMS) ────
# ===== Artist discography: Spotify + SoundCloud =====

# Threshold: if discography has more than this many tracks, show count-selection menu
ARTIST_LARGE_THRESHOLD = 15
# Preset counts shown in the count-selection menu
ARTIST_COUNT_PRESETS = [10, 20, 30, 50]
# Max albums to fetch for Spotify artist (safety limit)
SPOTIFY_ARTIST_MAX_ALBUMS = 50

# In-memory state for "awaiting custom count input"
# {chat_id: (kind, total, msg_id)} — when user is typing a number
_awaiting_custom_count = {}
def create_artist_menu(chat_id, kind, track_count, panel_id=None):
    """Artist discography main menu (panel-scoped).

    For small discographies (<= ARTIST_LARGE_THRESHOLD): 4-button menu
      Row 1: 📊 تعداد آثار: N  |  ❌ لغو
      Row 2: ⬇️ دانلود همه (سبز)
      Row 3: 🎵 انتخاب ترک (آبی)

    For large discographies (> ARTIST_LARGE_THRESHOLD): count-selection menu
      Row 1: 📊 تعداد آثار: N  |  ❌ لغو
      Row 2: ⬇️ دانلود همه (سبز)
      Row 3: 🎵 انتخاب ترک (آبی)
      Row 4: 🔢 تعداد دلخواه (آبی)
      Row 5+: preset counts (10/20/30/50) as primary buttons
    """
    pid = panel_id or "_"
    kb = InlineKeyboardMarkup()

    # Row 1: count + cancel
    kb.row(
        btn(tr(chat_id, "artist_track_count", count=track_count), callback_data="noop", style="normal"),
        btn(tr(chat_id, "artist_cancel"), callback_data=f"artist_close:{kind}:{pid}", style="danger"),
    )
    # Row 2: Download All (green)
    kb.row(btn(tr(chat_id, "artist_download_all", count=track_count),
              callback_data=f"artist_batch:{kind}:{pid}:0", style="success"))
    # Row 3: Select Track (blue) → opens picker at page 0 (first 10 tracks)
    kb.row(btn(tr(chat_id, "artist_select_track"),
              callback_data=f"artist_pickermode:{kind}:{pid}:0", style="primary"))

    # For large discographies: add custom count + presets
    if track_count > ARTIST_LARGE_THRESHOLD:
        # Row 4: Custom count (blue)
        kb.row(btn(tr(chat_id, "artist_custom_count"),
                  callback_data=f"artist_custom:{kind}:{pid}", style="primary"))
        # Row 5+: preset counts (2 per row)
        preset_row = []
        for p in ARTIST_COUNT_PRESETS:
            if p >= track_count:
                break
            preset_row.append(btn(
                tr(chat_id, "artist_download_first", count=p),
                callback_data=f"artist_batch:{kind}:{pid}:{p}",
                style="primary"
            ))
            if len(preset_row) == 2:
                kb.row(*preset_row)
                preset_row = []
        if preset_row:
            kb.row(*preset_row)

    return kb
def create_artist_picker_keyboard(chat_id, kind, tracks, page=0, per_page=10, panel_id=None):
    """Paginated track picker for artist discography (panel-scoped)."""
    pid = panel_id or "_"
    kb = InlineKeyboardMarkup()
    if not tracks:
        return kb

    total = len(tracks)
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total)

    for i in range(start_idx, end_idx):
        t = tracks[i]
        artist = t.get("artist") or "Unknown"
        title = t.get("title") or "Unknown"
        label = f"{i+1}. {artist} - {title}"
        kb.row(btn(label[:60], callback_data=f"artist_pick:{kind}:{pid}:{i}", style="primary"))

    # navigation
    nav = []
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > 0:
        nav.append(btn(tr(chat_id, "previous_page"), callback_data=f"artist_pickermode:{kind}:{pid}:{page-1}", style="primary"))
    nav.append(btn(tr(chat_id, "page_number", page=page+1, total_pages=total_pages), callback_data="noop", style="normal"))
    if end_idx < total:
        nav.append(btn(tr(chat_id, "next_page"), callback_data=f"artist_pickermode:{kind}:{pid}:{page+1}", style="primary"))
    if nav:
        kb.row(*nav)

    # back to artist menu + cancel
    kb.row(
        btn(tr(chat_id, "menu_back"), callback_data=f"artist_menu:{kind}:{pid}", style="primary"),
        btn(tr(chat_id, "artist_cancel"), callback_data=f"artist_close:{kind}:{pid}", style="danger"),
    )
    return kb

# ── 5.7  YouTube quality/shorts keyboards (defined in PART 4.2: create_youtube_quality_keyboard, create_youtube_shorts_keyboard) ────

# ── 5.8  Statistics keyboards + stats text builders ─────────────────────
# ===== Statistics Functions =====
def get_stats_text(chat_id):
    """Get statistics text for message editing"""
    user_stats = get_stats(chat_id)
    uptime_stats = get_uptime_stats()

    text = f"📊 {tr(chat_id, 'stats_title')}\n\n"
    text += f"👤 {tr(chat_id, 'your_stats')}:\n"
    text += f"📁 {tr(chat_id, 'downloads')}: {user_stats['user_count']}\n"
    text += f"💾 {tr(chat_id, 'volume')}: {human_size(user_stats['user_bytes'])}\n\n"

    text += f"🌍 {tr(chat_id, 'global_stats')}:\n"
    text += f"📁 {tr(chat_id, 'downloads')}: {user_stats['total_count']}\n"
    text += f"💾 {tr(chat_id, 'volume')}: {human_size(user_stats['total_bytes'])}\n"
    text += f"⏱️ {tr(chat_id, 'uptime')}: {uptime_stats['uptime']}\n"
    text += f"\n"
    text += f"🔍 {tr(chat_id, 'choose_category')}:"

    return text
def send_stats_main(chat_id):
    """Send main statistics page"""
    text = get_stats_text(chat_id)
    bot.send_message(chat_id, text, reply_markup=create_stats_keyboard(chat_id))
def edit_stats_main(chat_id, message_id):
    """Edit main statistics page (instead of sending new message)"""
    text = get_stats_text(chat_id)
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=create_stats_keyboard(chat_id))
    except Exception as e:
        print(f"Error editing stats message: {e}")
        # If editing fails, send new message
        bot.send_message(chat_id, text, reply_markup=create_stats_keyboard(chat_id))
def send_top_users_stats(chat_id, message_id, period='all'):
    """Send top users statistics - with message editing"""
    if period == 'daily':
        top_users = get_top_users_daily(3)
        title = tr(chat_id, 'daily_top_user_stats')
        user_period = 'daily'
    elif period == 'weekly':
        top_users = get_top_users_weekly(3)
        title = tr(chat_id, 'weekly_top_user_stats')
        user_period = 'weekly'
    else:
        top_users = get_top_users_all_time(3)
        title = tr(chat_id, 'top_user_stats')
        user_period = 'all'

    if not top_users:
        try:
            bot.edit_message_text(tr(chat_id, 'no_data'), chat_id, message_id, reply_markup=create_back_keyboard(chat_id))
        except Exception as e:
            print(f"Error editing message: {e}")
            bot.send_message(chat_id, tr(chat_id, 'no_data'), reply_markup=create_back_keyboard(chat_id))
        return

    text = f"{title}\n\n"

    for i, user in enumerate(top_users, 1):
        text += f"🏅 {tr(chat_id, 'rank')} {i}\n"
        text += f"👤 {tr(chat_id, 'user')}: {user['display_name']}\n"
        text += f"📁 {tr(chat_id, 'downloads')}: {user['download_count']}\n"
        text += f"💾 {tr(chat_id, 'volume')}: {human_size(user['total_size'])}\n"
        text += f"🎯 {tr(chat_id, 'most_used')}: {user['most_used_platform']}\n\n"

    # Add referring user's statistics
    user_stats = get_user_stats(chat_id, user_period)
    if user_stats['count'] > 0:
        if user_period == 'daily':
            text += f"📊 {tr(chat_id, 'your_daily_stats')}:\n"
        elif user_period == 'weekly':
            text += f"📊 {tr(chat_id, 'your_weekly_stats')}:\n"
        else:
            text += f"📊 {tr(chat_id, 'your_stats')}:\n"

        text += f"📁 {tr(chat_id, 'downloads')}: {user_stats['count']}\n"
        text += f"💾 {tr(chat_id, 'volume')}: {human_size(user_stats['bytes'])}\n"
    else:
        if user_period == 'daily':
            text += f"📊 {tr(chat_id, 'your_daily_stats')}: {tr(chat_id, 'no_user_data')}\n"
        elif user_period == 'weekly':
            text += f"📊 {tr(chat_id, 'your_weekly_stats')}: {tr(chat_id, 'no_user_data')}\n"
        else:
            text += f"📊 {tr(chat_id, 'your_stats')}: {tr(chat_id, 'no_user_data')}\n"

    # Edit main message instead of sending new one
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=create_back_keyboard(chat_id))
    except Exception as e:
        print(f"Error editing message: {e}")
        # If editing fails, send new message
        bot.send_message(chat_id, text, reply_markup=create_back_keyboard(chat_id))
def send_top_platforms_stats(chat_id, message_id, period='all'):
    """Send top platforms statistics"""
    if period == 'daily':
        platforms = get_platform_ranking_daily()
        title = tr(chat_id, 'daily_top_platform_stats')
        user_period = 'daily'
    elif period == 'weekly':
        platforms = get_platform_ranking_weekly()
        title = tr(chat_id, 'weekly_top_platform_stats')
        user_period = 'weekly'
    else:
        platforms = get_platform_ranking_all_time()
        title = tr(chat_id, 'top_platform_stats')
        user_period = 'all'

    if not platforms:
        try:
            bot.edit_message_text(tr(chat_id, 'no_data'), chat_id, message_id, reply_markup=create_back_keyboard(chat_id))
        except Exception as e:
            print(f"Error editing message: {e}")
            bot.send_message(chat_id, tr(chat_id, 'no_data'), reply_markup=create_back_keyboard(chat_id))
        return

    text = f"{title}\n\n"

    for i, platform in enumerate(platforms, 1):
        text += f"🏅 {tr(chat_id, 'rank')} {i}\n"
        text += f"🎯 {tr(chat_id, 'platform')}: {platform['platform']}\n"
        text += f"📁 {tr(chat_id, 'downloads')}: {platform['download_count']}\n"
        text += f"💾 {tr(chat_id, 'volume')}: {human_size(platform['total_size'])}\n\n"

    # Add referring user's platform statistics
    user_platforms = get_user_platform_stats(chat_id, user_period)
    if user_platforms:
        if user_period == 'daily':
            text += f"📊 {tr(chat_id, 'your_daily_stats')}:\n"
        elif user_period == 'weekly':
            text += f"📊 {tr(chat_id, 'your_weekly_stats')}:\n"
        else:
            text += f"📊 {tr(chat_id, 'your_stats')}:\n"

        for platform in user_platforms:
            text += f"🎯 {platform['platform']}: {platform['download_count']} ({human_size(platform['total_size'])})\n"
    else:
        if user_period == 'daily':
            text += f"📊 {tr(chat_id, 'your_daily_stats')}: {tr(chat_id, 'no_user_data')}\n"
        elif user_period == 'weekly':
            text += f"📊 {tr(chat_id, 'your_weekly_stats')}: {tr(chat_id, 'no_user_data')}\n"
        else:
            text += f"📊 {tr(chat_id, 'your_stats')}: {tr(chat_id, 'no_user_data')}\n"

    # Edit main message instead of sending new one
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=create_back_keyboard(chat_id))
    except Exception as e:
        print(f"Error editing message: {e}")
        # If editing fails, send new message
        bot.send_message(chat_id, text, reply_markup=create_back_keyboard(chat_id))
def create_stats_keyboard(chat_id):
    """Create main statistics keyboard with colored buttons"""
    kb = InlineKeyboardMarkup()

    kb.row(
        btn(f"👑 {tr(chat_id, 'top_users_all_time')}", callback_data="stats:top_users_all", style="primary"),
        btn(f"🏆 {tr(chat_id, 'top_platforms_all_time')}", callback_data="stats:top_platforms_all", style="primary")
    )

    kb.row(
        btn(f"📅 {tr(chat_id, 'top_users_daily')}", callback_data="stats:top_users_daily", style="primary"),
        btn(f"📊 {tr(chat_id, 'top_platforms_daily')}", callback_data="stats:top_platforms_daily", style="primary")
    )

    kb.row(
        btn(f"📆 {tr(chat_id, 'top_users_weekly')}", callback_data="stats:top_users_weekly", style="primary"),
        btn(f"📈 {tr(chat_id, 'top_platforms_weekly')}", callback_data="stats:top_platforms_weekly", style="primary")
    )

    # main menu + close
    kb.row(
        btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
        btn(tr(chat_id, "close_menu"), callback_data="stats:close", style="danger"),
    )

    return kb
def create_back_keyboard(chat_id):
    """Create back keyboard (back to stats + main menu)"""
    kb = InlineKeyboardMarkup()
    kb.row(
        btn(f"🔙 {tr(chat_id, 'back_to_stats')}", callback_data="stats:main", style="primary"),
        btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
    )
    return kb


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 6 — LINK HANDLERS (single / collection / artist)             ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 6.1  Forced join (is_member) ────────────────────────────────────────
# ===== Forced join =====
def is_member(chat_id):
    try:
        m = bot.get_chat_member(CHANNEL_USERNAME, chat_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

# ── 6.2  SoundCloud single (handle_download_soundcloud, handle_single_soundcloud, send_sc_item) ────
# ===== SoundCloud flow =====
def handle_download_soundcloud(chat_id, url):
    content_type = detect_content_type(url)
    lang = get_user_lang(chat_id) or "en"

    if content_type == "playlist":
        msg = bot.send_message(chat_id, tr(chat_id, "downloading_playlist"))
        msg_id = msg.message_id

        tmpdir = tempfile.mkdtemp(prefix="scdl_")
        try:
            ydl_opts_flat = {
                "quiet": True, "no_warnings": True, "extract_flat": True,
                "simulate": True, "skip_download": True,
            }

            with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
                info = ydl.extract_info(url, download=False)

                if "entries" in info and info["entries"]:
                    entries = [e for e in info["entries"] if e]

                    bot.edit_message_text(tr(chat_id, "playlist_detected", count=len(entries)), chat_id, msg_id)

                    playlist_items = []

                    for i, e in enumerate(entries):
                        if not e.get("title") or e.get("title") == "Unknown Title":
                            try:
                                single_opts = {
                                    "quiet": True, "no_warnings": True, "extract_flat": False,
                                    "simulate": True, "skip_download": True,
                                }

                                with yt_dlp.YoutubeDL(single_opts) as ydl_single:
                                    track_url = e.get("url") or e.get("webpage_url", "")
                                    if track_url:
                                        track_info = ydl_single.extract_info(track_url, download=False)
                                        e = track_info
                            except Exception as ex:
                                print(f"Error getting track info: {ex}")

                        title = e.get("title")
                        if not title or title == "Unknown Title":
                            url_text = e.get("webpage_url", e.get("url", ""))
                            if url_text:
                                import re
                                url_match = re.search(r'/([^/]+)(?:\?|$)', url_text)
                                if url_match:
                                    title = url_match.group(1).replace('-', ' ').replace('_', ' ').title()

                        artist = extract_artist(e)
                        if not artist or artist == "unknown":
                            if title and " - " in title:
                                artist = title.split(" - ")[0].strip()
                                title = title.split(" - ", 1)[1].strip()

                        final_title = title if title else f"Track {i+1}"
                        final_artist = artist if artist else "Unknown Artist"

                        playlist_items.append({
                            "title": final_title, "artist": final_artist,
                            "url": e.get("webpage_url", e.get("url", "")),
                            "duration": e.get("duration", 0), "thumb": e.get("thumbnail"),
                        })

                        if (i + 1) % 5 == 0:
                            bot.edit_message_text(tr(chat_id, "processing_playlist") + f" ({i+1}/{len(entries)})", chat_id, msg_id)

                    # Save playlist choices and send keyboard (panel-scoped)
                    save_playlist_choices(chat_id, playlist_items)

                    # Save batch meta (title + cover) for "Download All"
                    pl_title = info.get("title") or "SoundCloud Playlist"
                    pl_cover = None
                    thumbs = info.get("thumbnails") or []
                    if thumbs:
                        try:
                            best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
                            pl_cover = best.get("url")
                        except Exception:
                            pl_cover = thumbs[0].get("url")
                    if not pl_cover and playlist_items:
                        pl_cover = playlist_items[0].get("thumb")
                    save_batch_meta(chat_id, "soundcloud", pl_title, "", pl_cover, url, len(playlist_items))
                    pid = panel_save(chat_id, "sc_playlist", playlist_items, meta={"kind": "soundcloud", "title": pl_title, "cover": pl_cover, "url": url, "count": len(playlist_items)})
                    panel_cleanup(chat_id)

                    kb = create_sc_album_menu(chat_id, len(playlist_items), panel_id=pid)
                    bot.send_message(chat_id, tr(chat_id, "playlist_song_selection"), reply_markup=kb)
                else:
                    bot.edit_message_text(tr(chat_id, "no_results_found"), chat_id, msg_id)
                    
        except Exception as e:
            bot.edit_message_text(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
        finally:
            # Clean up AFTER processing (very important!)
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        handle_single_soundcloud(chat_id, url)
def handle_single_soundcloud(chat_id, url):
    """Download single SoundCloud track (proxy support removed).

    Premium/Go+ detection: SoundCloud tracks that require a subscription
    download only ~30s of audio without an error. We detect this by comparing
    the actual downloaded audio duration against the SoundCloud metadata
    duration, and fall back to YouTube Music / YouTube search.
    Retries for transient network errors are handled inside
    download_soundcloud_with_retry. DRM-protected tracks are routed to the
    YouTube fallback path.
    """
    quality = archive_quality_for(chat_id, "soundcloud")
    track_key = extract_track_key("soundcloud", {"url": url}, url)
    if ENABLE_MEDIA_ARCHIVE and track_key:
        cached = get_archive_record("soundcloud", track_key, quality)
        if cached:
            caption = build_sc_caption(chat_id, {
                "artist": cached.get("artist") or "Unknown",
                "title": cached.get("title") or "Unknown",
                "duration": cached.get("duration") or 0,
                "size": cached.get("size") or 0,
            }, url)
            if try_send_from_archive(chat_id, "soundcloud", track_key, quality, caption,
                                     performer=cached.get("artist"), title=cached.get("title"),
                                     duration=cached.get("duration")):
                return

    msg = bot.send_message(chat_id, tr(chat_id, "downloading_single"))
    msg_id = msg.message_id

    # Create progress bar instance
    progress_bar = ProgressBar(chat_id, msg_id)

    def hook(d):
        try:
            if d.get("status") == "downloading":
                done = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                progress_bar.update(done, total)
        except Exception:
            pass

    tmpdir = tempfile.mkdtemp(prefix="scdl_")

    def _do_yt_fallback():
        """Fetch SoundCloud metadata then download via YouTube. Returns item or None."""
        track_meta = {"title": "Unknown", "artist": "Unknown", "duration": 0, "thumb": None}
        try:
            ydl_opts_meta = {"quiet": True, "no_warnings": True, "extract_flat": True, "simulate": True, "skip_download": True, "socket_timeout": 15}
            with yt_dlp.YoutubeDL(ydl_opts_meta) as ydl_meta:
                meta_info = ydl_meta.extract_info(url, download=False)
            if meta_info:
                track_meta["title"] = meta_info.get("title") or "Unknown"
                track_meta["artist"] = extract_artist(meta_info) or "Unknown"
                track_meta["duration"] = meta_info.get("duration") or 0
                track_meta["thumb"] = meta_info.get("thumbnail")
        except Exception:
            pass
        return _sc_youtube_fallback(track_meta, tmpdir), track_meta

    try:
        res = download_soundcloud_with_retry(url, tmpdir, get_user_quality(chat_id),
                                              is_search=False, progress_hook=hook,
                                              max_retries=3)

        if not res.get("ok"):
            # DRM-protected track → try YouTube fallback
            if res.get("drm"):
                print(f"[SoundCloud] DRM detected, trying YouTube fallback")
                safe_edit_message(tr(chat_id, "drm_fallback_msg"), chat_id, msg_id)
                item, _ = _do_yt_fallback()
                if item:
                    safe_edit_message(tr(chat_id, "track_done"), chat_id, msg_id)
                    try:
                        send_sc_item(chat_id, item, url)
                    except Exception as e:
                        safe_edit_message(tr(chat_id, "error", err=f"send failed: {e}"), chat_id, msg_id)
                    finally:
                        if tmpdir and os.path.exists(tmpdir):
                            shutil.rmtree(tmpdir, ignore_errors=True)
                    return
                else:
                    safe_edit_message(tr(chat_id, "error", err=tr(chat_id, "err_drm_fallback")), chat_id, msg_id)
                    if tmpdir and os.path.exists(tmpdir):
                        shutil.rmtree(tmpdir, ignore_errors=True)
                    return
            else:
                safe_edit_message(tr(chat_id, "error", err=res.get("error", "failed")), chat_id, msg_id)
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
                return

        item = res["item"]

        # === Premium / Go+ preview detection (direct links only) ===
        # SoundCloud premium tracks download only ~30s. Detect via actual audio
        # duration vs metadata duration, then fall back to YouTube.
        if _is_sc_premium_preview(item, item.get("duration")):
            print(f"[SoundCloud] premium preview detected, falling back to YouTube")
            safe_edit_message(tr(chat_id, "drm_fallback_msg"), chat_id, msg_id)
            # clear the 30s preview file before fallback
            try:
                if item.get("filepath") and os.path.exists(item["filepath"]):
                    os.remove(item["filepath"])
            except Exception:
                pass
            yt_item, _ = _do_yt_fallback()
            if yt_item:
                safe_edit_message(tr(chat_id, "track_done"), chat_id, msg_id)
                try:
                    send_sc_item(chat_id, yt_item, url)
                except Exception as e:
                    safe_edit_message(tr(chat_id, "error", err=f"send failed: {e}"), chat_id, msg_id)
                finally:
                    if tmpdir and os.path.exists(tmpdir):
                        shutil.rmtree(tmpdir, ignore_errors=True)
                return
            else:
                # fallback failed too — user explicitly said don't send 30s previews.
                safe_edit_message(tr(chat_id, "error", err=tr(chat_id, "err_drm_fallback")), chat_id, msg_id)
                if tmpdir and os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
                return

        try:
            send_sc_item(chat_id, item, url)
        except Exception as e:
            print(f"Error sending item: {e}")
            safe_edit_message(tr(chat_id, "error", err=f"Failed to send file: {str(e)}"), chat_id, msg_id)
        finally:
            # Clean up AFTER sending (very important!)
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)

    except Exception as e:
        safe_edit_message(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
        try:
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
        except:
            pass
def _telegram_send_audio(chat_id, filepath, caption, performer, title, duration, thumb_file=None):
    """Send audio and return the Telegram Message (needed for archive copy)."""
    kwargs = {
        "caption": caption, "parse_mode": "HTML",
        "performer": performer, "title": title,
        "duration": duration or None,
    }
    with open(filepath, "rb") as f:
        if thumb_file and os.path.exists(thumb_file):
            try:
                with open(thumb_file, "rb") as tf:
                    kwargs["thumb"] = tf
                    return bot.send_audio(chat_id, f, **kwargs)
            except Exception:
                with open(filepath, "rb") as f2:
                    kwargs.pop("thumb", None)
                    return bot.send_audio(chat_id, f2, **kwargs)
        return bot.send_audio(chat_id, f, **kwargs)

def send_sc_item(chat_id, item, original_url=None, skip_archive=False):
    caption = build_sc_caption(chat_id, item, original_url)
    quality = archive_quality_for(chat_id, "soundcloud")
    track_key = extract_track_key("soundcloud", item, original_url or item.get("url"))

    if ENABLE_MEDIA_ARCHIVE and track_key:
        if try_send_from_archive(chat_id, "soundcloud", track_key, quality, caption,
                                 performer=item.get("artist"), title=item.get("title"),
                                 duration=item.get("duration")):
            return True

    if item.get("thumb_file"):
        try:
            with open(item["thumb_file"], "rb") as tf:
                bot.send_photo(chat_id, tf, caption=tr(chat_id, "cover_sent"))
        except Exception:
            pass

    safe_fp = force_audio_extension(item["filepath"])

    if item["size"] <= TELEGRAM_UPLOAD_LIMIT:
        msg = _telegram_send_audio(
            chat_id, safe_fp, caption, item.get("artist"), item.get("title"),
            item.get("duration"), item.get("thumb_file"),
        )
        add_stats_with_platform(chat_id, "SoundCloud", "audio", item["size"])
        if ENABLE_MEDIA_ARCHIVE and (not skip_archive) and msg and track_key:
            archive_message_in_background(
                chat_id, msg.message_id, "soundcloud", track_key, quality, "audio",
                title=item.get("title"), artist=item.get("artist"),
                duration=item.get("duration"), size=item.get("size"),
            )
        return msg
    bot.send_message(chat_id, tr(chat_id, "error", err=f"File too large: {human_size(item['size'])}"))
    return None

# ── 6.3  Spotify single (handle_download_spotify, handle_single_spotify, send_spotify_item) ────
# ===== Spotify Handlers =====
def handle_download_spotify(chat_id, url):
    """Route a Spotify link to single / album / playlist handler."""
    if not SPOTIFY_ENABLED:
        bot.send_message(chat_id, tr(chat_id, "spotify_disabled"))
        return
    kind, sid = extract_spotify_id(url)
    if not kind or not sid:
        bot.send_message(chat_id, tr(chat_id, "spotify_invalid"))
        return
    if kind == "track":
        handle_single_spotify(chat_id, sid)
    elif kind == "album":
        handle_spotify_collection(chat_id, sid, "album")
    elif kind == "playlist":
        handle_spotify_collection(chat_id, sid, "playlist")
    else:
        bot.send_message(chat_id, tr(chat_id, "spotify_invalid"))
def handle_single_spotify(chat_id, track_id):
    """Download a single Spotify track (metadata via spotapi, audio via search)."""
    quality = archive_quality_for(chat_id, "spotify")
    if ENABLE_MEDIA_ARCHIVE and track_id:
        cached = get_archive_record("spotify", str(track_id), quality)
        if cached:
            caption = _caption_for_archive_track(chat_id, "spotify", {"id": track_id, "url": f"https://open.spotify.com/track/{track_id}"}, cached, f"https://open.spotify.com/track/{track_id}")
            if try_send_from_archive(chat_id, "spotify", str(track_id), quality, caption,
                                     performer=cached.get("artist"), title=cached.get("title"),
                                     duration=cached.get("duration")):
                return

    msg = bot.send_message(chat_id, tr(chat_id, "spotify_fetching_track"))
    msg_id = msg.message_id

    try:
        meta = spotify_track_info(track_id)
    except Exception as e:
        safe_edit_message(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
        return

    if not meta:
        safe_edit_message(tr(chat_id, "error", err="track not found"), chat_id, msg_id)
        return

    track_key = extract_track_key("spotify", meta, meta.get("url"))
    caption_preview = caption_builder.build_caption(chat_id, "Spotify", {
        "artist": meta.get("artist"), "title": meta.get("title"),
        "album": meta.get("album"), "track_number": meta.get("track_number"),
        "duration": meta.get("duration") or 0, "size": 0,
        "year": meta.get("year"), "release_date": meta.get("release_date"),
    }, meta.get("url"))
    if ENABLE_MEDIA_ARCHIVE and track_key:
        if try_send_from_archive(chat_id, "spotify", track_key, quality, caption_preview,
                                 performer=meta.get("artist"), title=meta.get("title"),
                                 duration=meta.get("duration")):
            safe_edit_message(tr(chat_id, "spotify_single_done"), chat_id, msg_id)
            return

    safe_edit_message(tr(chat_id, "spotify_track_downloading") + "\n" + tr(chat_id, "spotify_searching_audio") + f"\n🎵 {meta['artist']} - {meta['title']}", chat_id, msg_id)

    progress_bar = ProgressBar(chat_id, msg_id)

    def hook(d):
        try:
            if d.get("status") == "downloading":
                done = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                progress_bar.update(done, total)
        except Exception:
            pass

    tmpdir = tempfile.mkdtemp(prefix="spdl_")
    try:
        item = download_spotify_track_audio(meta, tmpdir, progress_hook=hook, chat_id=chat_id)
        if not item:
            safe_edit_message(tr(chat_id, "spotify_audio_failed"), chat_id, msg_id)
            return
        safe_edit_message(tr(chat_id, "spotify_single_done"), chat_id, msg_id)
        try:
            send_spotify_item(chat_id, item, meta.get("url"), send_cover_photo=True)
        except Exception as e:
            print(f"[Spotify] send error: {e}")
            safe_edit_message(tr(chat_id, "error", err=f"send failed: {e}"), chat_id, msg_id)
    except Exception as e:
        safe_edit_message(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
def send_spotify_item(chat_id, item, original_url=None, send_cover_photo=False, skip_archive=False):
    """Send a Spotify track as audio (with embedded cover thumb) + caption."""
    caption = caption_builder.build_caption(chat_id, "Spotify", item, original_url)
    quality = archive_quality_for(chat_id, "spotify")
    track_key = extract_track_key("spotify", item, original_url or item.get("url"))

    if ENABLE_MEDIA_ARCHIVE and track_key:
        if try_send_from_archive(chat_id, "spotify", track_key, quality, caption,
                                 performer=item.get("artist"), title=item.get("title"),
                                 duration=item.get("duration")):
            return True

    if send_cover_photo and item.get("thumb_file"):
        try:
            with open(item["thumb_file"], "rb") as tf:
                bot.send_photo(chat_id, tf, caption=tr(chat_id, "cover_sent"))
        except Exception:
            pass

    safe_fp = force_audio_extension(item["filepath"]) if not item["filepath"].lower().endswith(".mp3") else item["filepath"]

    if item["size"] <= TELEGRAM_UPLOAD_LIMIT:
        msg = _telegram_send_audio(
            chat_id, safe_fp, caption, item.get("artist"), item.get("title"),
            item.get("duration"), item.get("thumb_file"),
        )
        add_stats_with_platform(chat_id, "Spotify", "audio", item["size"])
        if ENABLE_MEDIA_ARCHIVE and (not skip_archive) and msg and track_key:
            archive_message_in_background(
                chat_id, msg.message_id, "spotify", track_key, quality, "audio",
                title=item.get("title"), artist=item.get("artist"),
                duration=item.get("duration"), size=item.get("size"),
            )
        return msg
    bot.send_message(chat_id, tr(chat_id, "error", err=f"File too large: {human_size(item['size'])}"))
    return None

# ── 6.4  Spotify collection (album/playlist) (handle_spotify_collection) ────
def handle_spotify_collection(chat_id, sid, kind):
    """Fetch album/playlist metadata and show selection + Download All keyboard."""
    if kind == "album":
        msg = bot.send_message(chat_id, tr(chat_id, "spotify_fetching_album"))
    else:
        msg = bot.send_message(chat_id, tr(chat_id, "spotify_fetching_playlist"))
    msg_id = msg.message_id

    try:
        if kind == "album":
            data = spotify_album_info(sid)
            owner_label = data.get("artist") or ""
            found_key = "spotify_album_found"
        else:
            data = spotify_playlist_info(sid)
            owner_label = data.get("owner") or ""
            found_key = "spotify_playlist_found"
    except Exception as e:
        safe_edit_message(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
        return

    tracks = data.get("tracks") or []
    if not tracks:
        safe_edit_message(tr(chat_id, "spotify_no_tracks"), chat_id, msg_id)
        return

    # cache tracks + meta for pick / batch (panel-scoped for panel-state fix)
    pid = panel_save(chat_id, "spotify", tracks, meta={"title": data.get("title"), "artist": (data.get("artist") or owner_label), "cover": data.get("cover"), "url": data.get("url"), "count": len(tracks), "kind": "spotify"})
    panel_cleanup(chat_id)
    save_spotify_choices(chat_id, tracks)  # also keep global cache for backward compat
    save_batch_meta(chat_id, "spotify", data.get("title"), data.get("artist") or owner_label, data.get("cover"), data.get("url"), len(tracks))

    # inform user
    text = tr(chat_id, found_key, name=data.get("title") or "Unknown",
              artist=(data.get("artist") or owner_label or "-"), count=len(tracks))
    safe_edit_message(text, chat_id, msg_id)

    # send selection keyboard (panel-scoped)
    kb = create_spotify_keyboard(chat_id, tracks, page=0, per_page=10, panel_id=pid)
    bot.send_message(chat_id, tr(chat_id, "spotify_select_track"), reply_markup=kb)

# ── 6.5  Spotify artist (handle_spotify_artist, spotify_artist_info) ────
def spotify_artist_info(artist_id):
    """Fetch an artist's full discography (all tracks across all releases).

    Uses spotapi's Artist.get_artist (queryArtistOverview) to get the artist's
    name + avatar, then Artist.paginate_artist_discography to get the list of
    releases (albums/singles/compilations), then fetches tracks for each
    release via spotify_album_info.

    Returns:
        {
            "name": str,
            "cover": str|None,  # artist avatar
            "url": str,
            "tracks": list of track dicts (same format as album tracks),
            "id": str,
            "kind": "artist",
        }
    """
    try:
        from spotapi import Artist as _SpotapiArtist
    except Exception:
        raise RuntimeError("spotapi not available")

    artist = _SpotapiArtist()

    # Get artist name + avatar via get_artist (queryArtistOverview)
    # This is the CORRECT way — paginate_artists searches by name and returns
    # the wrong artist when given an ID.
    artist_name = "Unknown Artist"
    artist_cover = None
    try:
        resp = artist.get_artist(artist_id)
        au = (resp.get("data") or {}).get("artistUnion") or {}
        profile = au.get("profile") or {}
        artist_name = profile.get("name") or artist_name
        visuals = au.get("visuals") or au.get("visualIdentity") or {}
        avatar = visuals.get("avatarImage") or {}
        sources = avatar.get("sources") or []
        if sources:
            artist_cover = _sp_largest_image(sources)
    except Exception as e:
        print(f"[Spotify artist] name fetch warning: {e}")

    # Get discography (all releases)
    all_releases = []
    for chunk in artist.paginate_artist_discography(artist_id, section="all"):
        for r in chunk:
            items = r.get("releases", {}).get("items") or []
            all_releases.extend(items)

    if not all_releases:
        return {
            "name": artist_name, "cover": artist_cover,
            "url": f"https://open.spotify.com/artist/{artist_id}",
            "tracks": [], "id": artist_id, "kind": "artist",
        }

    # Safety limit
    all_releases = all_releases[:SPOTIFY_ARTIST_MAX_ALBUMS]

    # Fetch tracks for each release
    all_tracks = []
    for idx, rel in enumerate(all_releases, 1):
        uri = rel.get("uri") or ""
        if not uri.startswith("spotify:album:"):
            continue
        album_id = uri.split(":")[-1]
        album_cover = _sp_largest_image((rel.get("coverArt") or {}).get("sources"))

        try:
            album_data = spotify_album_info(album_id)
            for t in album_data.get("tracks", []):
                # Use the artist's name as fallback if track artist is unknown
                if not t.get("artist") or t["artist"] == "Unknown":
                    t["artist"] = artist_name
                # Use album cover as fallback for track cover
                if not t.get("cover"):
                    t["cover"] = album_cover or artist_cover
                all_tracks.append(t)
        except Exception as e:
            print(f"[Spotify artist] album {album_id} fetch failed: {e}")
            continue

    return {
        "name": artist_name, "cover": artist_cover,
        "url": f"https://open.spotify.com/artist/{artist_id}",
        "tracks": all_tracks, "id": artist_id, "kind": "artist",
    }
def handle_spotify_artist(chat_id, artist_id):
    """Fetch Spotify artist discography and show the artist menu."""
    msg = bot.send_message(chat_id, tr(chat_id, "artist_fetching"))
    msg_id = msg.message_id

    try:
        data = spotify_artist_info(artist_id)
    except Exception as e:
        safe_edit_message(tr(chat_id, "artist_fetch_error", err=str(e)[:120]), chat_id, msg_id)
        return

    tracks = data.get("tracks") or []
    if not tracks:
        safe_edit_message(tr(chat_id, "artist_no_tracks"), chat_id, msg_id)
        return

    # Cache tracks + meta
    save_spotify_choices(chat_id, tracks)
    save_batch_meta(chat_id, "spotify_artist", data.get("name"), data.get("name"),
                    data.get("cover"), data.get("url"), len(tracks))
    pid = panel_save(chat_id, "spotify_artist", tracks, meta={"kind": "spotify_artist", "title": data.get("name"), "cover": data.get("cover"), "url": data.get("url"), "count": len(tracks)})
    panel_cleanup(chat_id)

    # Show artist menu
    text = tr(chat_id, "artist_found", name=data.get("name") or "Unknown", count=len(tracks))
    safe_edit_message(text, chat_id, msg_id)

    kb = create_artist_menu(chat_id, "spotify_artist", len(tracks), panel_id=pid)
    bot.send_message(chat_id, tr(chat_id, "artist_select_track") if len(tracks) <= ARTIST_LARGE_THRESHOLD else tr(chat_id, "artist_large_notice"),
                    reply_markup=kb)

# ── 6.6  SoundCloud artist (handle_soundcloud_artist, soundcloud_artist_tracks) ────
def soundcloud_artist_tracks(artist_url):
    """Fetch all tracks from a SoundCloud artist page.

    Uses yt-dlp with extract_flat on the /tracks URL.
    Returns a tuple: (artist_name, artist_cover, tracks)
      - artist_name: display name from yt-dlp info (not URL slug)
      - artist_cover: artist avatar URL if available
      - tracks: list of {title, artist, url, duration, thumb}
    """
    # Normalize URL to /tracks
    url = artist_url.rstrip("/")
    if "/tracks" not in url:
        if "/sets/" in url or "/albums/" in url:
            # It's a playlist, not an artist page
            return ("Unknown", None, [])
        url = url + "/tracks"

    ydl_opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True,
        "simulate": True, "skip_download": True,
        "socket_timeout": 20, "extractor_retries": 2,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise RuntimeError(f"yt-dlp error: {e}")

    # Extract artist name from info (NOT from URL slug)
    # yt-dlp returns the channel/uploader name in various fields
    artist_name = (info.get("channel") or info.get("uploader") or
                   info.get("channel_name") or info.get("uploader_id") or
                   "SoundCloud Artist")
    # The title field often has "Artist Name (Tracks)" — extract just the name
    info_title = info.get("title") or ""
    if " (Tracks)" in info_title:
        artist_name = info_title.replace(" (Tracks)", "").strip()
    elif " (All)" in info_title:
        artist_name = info_title.replace(" (All)", "").strip()

    # Artist avatar: yt-dlp may provide thumbnails for the playlist/channel
    artist_cover = None
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        try:
            best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            artist_cover = best.get("url")
        except Exception:
            artist_cover = thumbnails[0].get("url")

    entries = info.get("entries") or []
    # Extract the URL slug for comparison (e.g. "amira-elfeky-201651668")
    url_slug = ""
    url_clean = url.replace("https://", "").replace("http://", "").split("?")[0].rstrip("/")
    parts = [p for p in url_clean.split("/")[1:] if p]
    if parts:
        url_slug = parts[0]

    tracks = []
    for e in entries:
        if not e:
            continue
        title = e.get("title") or "Unknown"
        track_url = e.get("url") or e.get("webpage_url") or ""
        # For flat SoundCloud entries, extract_artist returns the URL slug.
        # Use the artist_name from the page instead, unless extract_artist
        # returns something genuinely different.
        track_artist = extract_artist(e)
        if (not track_artist or track_artist == "unknown" or
            track_artist == url_slug or track_artist.lower() == url_slug.lower()):
            track_artist = artist_name
        tracks.append({
            "title": title,
            "artist": track_artist,
            "url": track_url,
            "duration": e.get("duration") or 0,
            "thumb": e.get("thumbnail"),
        })
    return (artist_name, artist_cover, tracks)
def handle_soundcloud_artist(chat_id, url):
    """Fetch SoundCloud artist tracks and show the artist menu."""
    msg = bot.send_message(chat_id, tr(chat_id, "artist_fetching"))
    msg_id = msg.message_id

    try:
        artist_name, artist_cover, tracks = soundcloud_artist_tracks(url)
    except Exception as e:
        safe_edit_message(tr(chat_id, "artist_fetch_error", err=str(e)[:120]), chat_id, msg_id)
        return

    if not tracks:
        safe_edit_message(tr(chat_id, "artist_no_tracks"), chat_id, msg_id)
        return

    # Cache tracks + meta (reuse playlist_cache for SoundCloud)
    save_playlist_choices(chat_id, tracks)
    save_batch_meta(chat_id, "soundcloud_artist", artist_name, "", artist_cover, url, len(tracks))
    pid = panel_save(chat_id, "soundcloud_artist", tracks, meta={"kind": "soundcloud_artist", "title": artist_name, "cover": artist_cover, "url": url, "count": len(tracks)})
    panel_cleanup(chat_id)

    # Show artist menu
    text = tr(chat_id, "artist_found", name=artist_name, count=len(tracks))
    safe_edit_message(text, chat_id, msg_id)

    kb = create_artist_menu(chat_id, "soundcloud_artist", len(tracks), panel_id=pid)
    bot.send_message(chat_id, tr(chat_id, "artist_select_track") if len(tracks) <= ARTIST_LARGE_THRESHOLD else tr(chat_id, "artist_large_notice"),
                    reply_markup=kb)

# ── 6.7  YouTube / Shorts file senders (send_youtube_item, send_youtube_short_item, send_media_item, build_*_caption wrappers) ────
# ===== File Senders =====
def build_sc_caption(chat_id, item, original_url=None):
    """Wrapper for backward compatibility"""
    return caption_builder.build_caption(chat_id, "SoundCloud", item, original_url)
def build_youtube_caption(chat_id, item, original_url=None, audio_only=False):
    """Wrapper for backward compatibility"""
    return caption_builder.build_caption(chat_id, "YouTube", item, original_url, audio_only=audio_only)
def build_media_caption(chat_id, item, platform, original_url=None):
    """Wrapper for backward compatibility"""
    return caption_builder.build_caption(chat_id, platform, item, original_url)
def send_youtube_item(chat_id, item, original_url=None, audio_only=False):
    caption = build_youtube_caption(chat_id, item, original_url, audio_only)

    # Send thumbnail first for YouTube regular videos (allowed platform)
    if item.get("thumb_file") and not audio_only:
        try:
            with open(item["thumb_file"], "rb") as tf:
                bot.send_photo(chat_id, tf, caption=tr(chat_id, "youtube_preview"))
        except Exception:
            pass

    if item["size"] <= TELEGRAM_UPLOAD_LIMIT:
        if audio_only:
            # Send as audio file
            with open(item["filepath"], "rb") as f:
                kwargs = {
                    "caption": caption,
                    "title": item["title"],
                    "duration": item["duration"] or None,
                }
                if item.get("thumb_file"):
                    try:
                        with open(item["thumb_file"], "rb") as tf:
                            kwargs["thumb"] = tf
                            bot.send_audio(chat_id, f, **kwargs)
                    except Exception:
                        bot.send_audio(chat_id, f, **kwargs)
                else:
                    bot.send_audio(chat_id, f, **kwargs)
            add_stats_with_platform(chat_id, "YouTube", "audio", item["size"])
        else:
            # Send as video file
            with open(item["filepath"], "rb") as f:
                kwargs = {
                    "caption": caption,
                    "duration": item.get("duration") or None,
                    "supports_streaming": True,
                }
                if item.get("thumb_file"):
                    try:
                        with open(item["thumb_file"], "rb") as tf:
                            kwargs["thumb"] = tf
                            bot.send_video(chat_id, f, **kwargs)
                    except Exception:
                        bot.send_video(chat_id, f, **kwargs)
                else:
                    bot.send_video(chat_id, f, **kwargs)
            add_stats_with_platform(chat_id, "YouTube", "video", item["size"])
    else:
        bot.send_message(chat_id, tr(chat_id, "error", err=f"File too large: {human_size(item['size'])}"))
def send_youtube_short_item(chat_id, item, original_url=None, audio_only=False):
    """Send YouTube Short WITHOUT thumbnail"""
    caption = build_youtube_caption(chat_id, item, original_url, audio_only)

    # NO thumbnail for YouTube Shorts
    if item["size"] <= TELEGRAM_UPLOAD_LIMIT:
        if audio_only:
            # Send as audio file
            with open(item["filepath"], "rb") as f:
                kwargs = {
                    "caption": caption,
                    "title": item["title"],
                    "duration": item["duration"] or None,
                }
                bot.send_audio(chat_id, f, **kwargs)
            add_stats_with_platform(chat_id, "YouTube", "audio", item["size"])
        else:
            # Send as video file WITHOUT thumbnail
            with open(item["filepath"], "rb") as f:
                kwargs = {
                    "caption": caption,
                    "duration": item.get("duration") or None,
                    "supports_streaming": True,
                }
                bot.send_video(chat_id, f, **kwargs)
            add_stats_with_platform(chat_id, "YouTube", "video", item["size"])
    else:
        bot.send_message(chat_id, tr(chat_id, "error", err=f"File too large: {human_size(item['size'])}"))
def send_media_item(chat_id, item, platform, original_url=None):
    caption = build_media_caption(chat_id, item, platform, original_url)
    ext = (item.get("ext") or "").lower()
    size = item.get("size", 0)

    if size > TELEGRAM_UPLOAD_LIMIT:
        bot.send_message(chat_id, tr(chat_id, "error", err=f"File too large: {human_size(size)}"))
        return

    # NO thumbnail sending for non-allowed platforms (Pinterest, Instagram, TikTok, Twitter)
    # Only YouTube and SoundCloud are allowed to send thumbnails

    if ext in ["jpg", "jpeg", "png", "webp"]:
        try:
            with open(item["filepath"], "rb") as f:
                bot.send_photo(chat_id, f, caption=caption)
        except Exception as e:
            bot.send_message(chat_id, tr(chat_id, "error", err=str(e)))
        add_stats_with_platform(chat_id, platform, "image", size)
    else:
        # Ensure video has .mp4 extension
        video_path = force_video_extension(item["filepath"])
        
        # Video
        try:
            with open(video_path, "rb") as f:
                kwargs = {
                    "caption": caption, 
                    "duration": item.get("duration") or None, 
                    "supports_streaming": True,
                }
                bot.send_video(chat_id, f, **kwargs)
        except Exception as e:
            bot.send_message(chat_id, tr(chat_id, "error", err=str(e)))
        add_stats_with_platform(chat_id, platform, "video", size)

# ── 6.8  Pinterest/Instagram/TikTok/Twitter handlers (defined in PART 4.7: handle_download_pinterest/instagram/tiktok/twitter, handle_generic_download) ────

# ── 6.9  Search (do_search_spotify, do_search) ──────────────────────────
def do_search_spotify(chat_id, query):
    """Search Spotify for tracks and show paginated results."""
    lang = get_user_lang(chat_id) or "en"
    initial_msg = bot.send_message(chat_id, tr(chat_id, "searching_spotify"))
    msg_id = initial_msg.message_id

    try:
        choices = spotify_search_tracks(query, limit=15)
        if not choices:
            bot.edit_message_text(tr(chat_id, "no_results_found"), chat_id, msg_id)
            # Offer SoundCloud fallback
            kb = InlineKeyboardMarkup()
            kb.row(btn("🎵 SoundCloud", callback_data=f"search_sc:{query[:60]}", style="primary"))
            kb.row(btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"))
            bot.send_message(chat_id, tr(chat_id, "search_try_other"), reply_markup=kb)
            return

        # Save choices in spotify_cache for picker (panel-scoped)
        save_spotify_choices(chat_id, choices)
        pid = panel_save(chat_id, "spotify_search", choices, meta={"kind": "spotify_search"})
        panel_cleanup(chat_id)
        bot.edit_message_text(tr(chat_id, "search_results_found", count=len(choices)), chat_id, msg_id)

        # Use the Spotify artist picker keyboard (reuse for search results)
        kb = create_spotify_search_keyboard(chat_id, choices, page=0, per_page=10, panel_id=pid)
        bot.send_message(chat_id, tr(chat_id, "pick_from_results"), reply_markup=kb)
    except Exception as e:
        bot.edit_message_text(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
def do_search(chat_id, query):
    lang = get_user_lang(chat_id) or "en"

    initial_msg = bot.send_message(chat_id, tr(chat_id, "searching"))
    msg_id = initial_msg.message_id

    tmpdir = tempfile.mkdtemp(prefix="scsrch_")
    try:
        ydl_opts = {
            "quiet": True, "no_warnings": True, "extract_flat": True,
            "simulate": True, "skip_download": True,
            "socket_timeout": 15, "extractor_retries": 2,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"scsearch15:{query}"
            info = ydl.extract_info(search_query, download=False)

            entries = info.get("entries") or []
            choices = []

            if entries:
                bot.edit_message_text(tr(chat_id, "searching_with_count", count=len(entries)), chat_id, msg_id)

                for i, e in enumerate(entries):
                    if e:
                        title = e.get("title")
                        if not title or title == "Unknown Title":
                            url_text = e.get("webpage_url", e.get("url", ""))
                            if url_text:
                                import re
                                url_match = re.search(r'/([^/]+)(?:\?|$)', url_text)
                                if url_match:
                                    title = url_match.group(1).replace('-', ' ').replace('_', ' ').title()

                        artist = extract_artist(e)
                        if not artist or artist == "unknown":
                            if title and " - " in title:
                                artist = title.split(" - ")[0].strip()
                                title = title.split(" - ", 1)[1].strip()

                        final_title = title if title else f"Track {i+1}"
                        final_artist = artist if artist else "Unknown Artist"

                        choices.append({
                            "title": final_title, "artist": final_artist,
                            "url": e.get("webpage_url", ""), "duration": e.get("duration", 0),
                            "thumb": e.get("thumbnail"),
                        })

                        if (i + 1) % 5 == 0:
                            bot.edit_message_text(tr(chat_id, "processing_results") + f" ({i+1}/{len(entries)})", chat_id, msg_id)

        if not choices:
            bot.edit_message_text(tr(chat_id, "no_results_found"), chat_id, msg_id)
            return

        save_search_choices(chat_id, choices)
        pid = panel_save(chat_id, "sc_search", choices, meta={"kind": "sc_search"})
        panel_cleanup(chat_id)

        bot.edit_message_text(tr(chat_id, "search_results_found", count=len(choices)), chat_id, msg_id)

        kb = create_paginated_keyboard(choices, chat_id, 0, 10, "search", panel_id=pid)
        bot.send_message(chat_id, tr(chat_id, "pick_from_results"), reply_markup=kb)

    except Exception as e:
        bot.edit_message_text(tr(chat_id, "error", err=str(e)), chat_id, msg_id)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 7 — BATCH DOWNLOAD & RESUME                                  ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 7.1  Cancel infra (_batch_cancel_flags, _set_batch_cancel, _is_batch_cancelled, _clear_batch_cancel, _sp_panel_tracks, _sc_panel_tracks) ────
# ===== Batch Download (Download All) for SoundCloud playlists + Spotify albums/playlists + artist discography =====

# Cancel infrastructure: each chat_id gets a threading.Event when a batch starts.
# The batch loop checks this event before processing each track.
_batch_cancel_flags = {}
def _sp_panel_tracks(chat_id, panel_id):
    """Return the tracks snapshot for a Spotify panel_id.

    If the panel_id is found in the `panels` table, return its tracks.
    Otherwise (old-format callback, or panel expired) fall back to the
    global per-chat spotify_cache — preserving backward compatibility.
    """
    if panel_id:
        try:
            _kind, tracks, _meta = panel_get(chat_id, panel_id)
            if tracks:
                return tracks
        except Exception:
            pass
    return get_all_spotify_choices(chat_id)
def _sc_panel_tracks(chat_id, panel_id):
    """Return the tracks snapshot for a SoundCloud panel_id, else global cache."""
    if panel_id:
        try:
            _kind, tracks, _meta = panel_get(chat_id, panel_id)
            if tracks:
                return tracks
        except Exception:
            pass
    return get_all_playlist_choices(chat_id)
def _set_batch_cancel(chat_id):
    """Mark the batch for this chat as cancelled."""
    ev = _batch_cancel_flags.get(chat_id)
    if ev:
        ev.set()
def _is_batch_cancelled(chat_id):
    """Check if the batch for this chat has been cancelled."""
    ev = _batch_cancel_flags.get(chat_id)
    return ev is not None and ev.is_set()
def _clear_batch_cancel(chat_id):
    """Remove the cancel flag after the batch finishes."""
    _batch_cancel_flags.pop(chat_id, None)

# ── 7.2  Progress helpers (_make_progress_bar, _format_failed_list) ─────
def _make_progress_bar(pct, width=20):
    """Create a visual progress bar string: [████████░░░░░░░░░░░░]"""
    pct = max(0, min(100, int(pct)))
    filled = int(width * pct / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"
def _format_failed_list(chat_id, failed, max_chars=3000):
    """Format the list of failed tracks for the final (or live progress) message.

    Returns a string like '\n\n⚠️ Failed (3):\n• Artist - Title\n• ...' or '' if
    no failures.

    Lists ALL failed tracks (one per line) — the user explicitly wants every
    failed track name reported, not just the first 10. If the combined text
    would exceed max_chars (to stay within Telegram's 4096-char message limit),
    the list is truncated and '... and N more' is appended.

    Args:
        chat_id: Telegram chat id (for i18n).
        failed: list of (title, error) tuples.
        max_chars: soft cap on the rendered list text (excluding the header).
    """
    if not failed:
        return ""
    names = []
    shown = 0
    total_len = 0
    for title, _err in failed:
        line = f"• {title}"
        # +1 for the newline join, small margin
        if shown > 0 and total_len + len(line) + 1 > max_chars:
            break
        names.append(line)
        total_len += len(line) + 1
        shown += 1
    lines = "\n".join(names)
    if shown < len(failed):
        lines += tr(chat_id, "batch_failed_more", count=len(failed) - shown)
    return tr(chat_id, "batch_failed_list", count=len(failed), list=lines)

# ── 7.3  Send helpers (_send_audio_for_batch, _send_with_retry, _get_batch_tracks, _build_track_meta_for_batch) ────
def _send_audio_for_batch(chat_id, item, platform, original_url, from_archive=False):
    """Send a single audio file during a batch. Returns the Telegram Message or True.

    Collections skip immediate archive copy: the ordered collection job
    copies every track into the archive channel after (or alongside) user send.
    """
    caption = caption_builder.build_caption(chat_id, platform, item, original_url)
    plat = archive_platform_name(platform)
    quality = archive_quality_for(chat_id, plat)
    track_key = extract_track_key(plat, item, original_url or item.get("url"))

    if ENABLE_MEDIA_ARCHIVE and track_key:
        if try_send_from_archive(chat_id, plat, track_key, quality, caption,
                                 performer=item.get("artist"), title=item.get("title"),
                                 duration=item.get("duration")):
            return {"from_archive": True, "track_key": track_key, "platform": plat,
                    "title": item.get("title"), "artist": item.get("artist"),
                    "duration": item.get("duration"), "size": item.get("size")}

    safe_fp = item.get("filepath")
    if not safe_fp or not os.path.exists(safe_fp):
        raise RuntimeError("file missing")
    if item.get("size", 0) > TELEGRAM_UPLOAD_LIMIT:
        raise RuntimeError(f"file too large ({human_size(item.get('size',0))})")

    msg = _telegram_send_audio(
        chat_id, safe_fp, caption, item.get("artist"), item.get("title"),
        item.get("duration"), item.get("thumb_file"),
    )
    add_stats_with_platform(chat_id, platform, "audio", item.get("size", 0))
    return {
        "from_archive": False, "message": msg, "track_key": track_key, "platform": plat,
        "title": item.get("title"), "artist": item.get("artist"),
        "duration": item.get("duration"), "size": item.get("size"),
        "from_chat_id": chat_id, "message_id": getattr(msg, "message_id", None),
        "file_type": "audio",
    }
def _send_with_retry(chat_id, item, platform, original_url, max_tries=2):
    """Send an audio file, retrying once on Telegram flood errors. Returns send info or True."""
    last_err = None
    for attempt in range(max_tries):
        try:
            return _send_audio_for_batch(chat_id, item, platform, original_url)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "too many requests" in msg or "flood" in msg or "retry after" in msg:
                wait = 5
                try:
                    import re as _re
                    m = _re.search(r"retry after (\d+)", msg)
                    if m:
                        wait = int(m.group(1)) + 1
                except Exception:
                    pass
                print(f"[batch] flood control, waiting {wait}s")
                time.sleep(wait)
                continue
            raise
    if last_err:
        raise last_err
    return False
def _get_batch_tracks(chat_id, kind):
    """Get the cached track list for a batch, based on kind.

    Kinds:
      - "spotify" / "spotify_artist" → spotify_cache
      - "soundcloud" / "soundcloud_artist" → playlist_cache
    """
    if kind in ("spotify", "spotify_artist"):
        return get_all_spotify_choices(chat_id)
    else:
        return get_all_playlist_choices(chat_id)
def _build_track_meta_for_batch(trk, kind, chat_id):
    """Build the track_meta dict needed for downloading a single track in a batch."""
    title = trk.get("title") or "Unknown"
    artist = trk.get("artist") or "Unknown"
    if kind in ("spotify", "spotify_artist"):
        return {
            "title": title, "artist": artist, "album": trk.get("album"),
            "duration": trk.get("duration") or 0, "cover": trk.get("cover"),
            "track_number": trk.get("track_number"),
            "id": trk.get("id"), "url": trk.get("url"),
        }, "Spotify"
    else:
        # SoundCloud: return (track_url, platform) — handled differently
        return trk.get("url"), "SoundCloud"

# ── 7.4  batch_download_and_send (with dedupe + state persistence) ──────
def batch_download_and_send(chat_id, kind, count=None):
    """Download all (or first `count`) tracks of a batch.

    Supports kinds: "spotify", "soundcloud", "spotify_artist", "soundcloud_artist".

    Features:
      - Sends the cover ONCE (with title, count, source link).
      - Downloads each track, tags it, and sends it.
      - Shows live progress with a visual bar + cancel button.
      - REAL cancel: checks a threading.Event before each track.
      - Collects failed track names and reports them in the final message.
      - If `count` is given, only downloads the first `count` tracks.

    Args:
        chat_id: Telegram chat id.
        kind: One of "spotify", "soundcloud", "spotify_artist", "soundcloud_artist".
        count: Optional int — if given, only download the first `count` tracks.
    """
    meta = get_batch_meta(chat_id)
    if not meta:
        bot.send_message(chat_id, tr(chat_id, "batch_no_data"))
        return
    # Accept kind match OR spotify→spotify_artist / soundcloud→soundcloud_artist
    meta_kind = meta.get("kind", "")
    kind_matches = (meta_kind == kind or
                    (kind == "spotify_artist" and meta_kind == "spotify") or
                    (kind == "spotify" and meta_kind == "spotify_artist") or
                    (kind == "soundcloud_artist" and meta_kind == "soundcloud") or
                    (kind == "soundcloud" and meta_kind == "soundcloud_artist"))
    if not kind_matches:
        bot.send_message(chat_id, tr(chat_id, "batch_no_data"))
        return

    tracks = _get_batch_tracks(chat_id, kind)
    if not tracks:
        bot.send_message(chat_id, tr(chat_id, "batch_no_data"))
        return

    # Apply count limit if specified
    total_available = len(tracks)
    if count is not None and count > 0 and count < total_available:
        tracks = tracks[:count]
    total = len(tracks)

    # Determine display labels
    lang = get_user_lang(chat_id) or "en"
    kind_map = {
        "spotify": ("Spotify", "اسپاتیفای"),
        "spotify_artist": ("Spotify", "اسپاتیفای"),
        "soundcloud": ("SoundCloud", "ساندکلاد"),
        "soundcloud_artist": ("SoundCloud", "ساندکلاد"),
    }
    kind_label_en, kind_label_fa = kind_map.get(kind, (kind, kind))
    kind_display = kind_label_fa if lang == "fa" else kind_label_en

    # === Duplicate skip (optional, per-user setting) ===
    # In a discography, the same track may appear across multiple albums / as a
    # single. When enabled, skip tracks whose (artist+title) we already sent.
    dedupe_on = get_dedupe_setting(chat_id)
    seen_keys = set()  # only populated when dedupe_on
    skipped_dupes = 0
    if dedupe_on:
        print(f"[batch] dedupe ON for chat {chat_id}")

    # Set up cancel flag
    cancel_event = _threading_mod.Event()
    _batch_cancel_flags[chat_id] = cancel_event

    # 1) send cover once (with album/playlist/artist link)
    cover_sent = False
    cover_tmpdir = tempfile.mkdtemp(prefix="batchcover_")
    try:
        if meta.get("cover"):
            cover_path = file_processor.download_thumb(meta["cover"], cover_tmpdir)
            if cover_path and os.path.exists(cover_path):
                try:
                    with open(cover_path, "rb") as cf:
                        bot.send_photo(chat_id, cf, caption=tr(chat_id, "batch_cover_sent",
                                                               kind=kind_display, title=meta.get("title") or "",
                                                               count=total, url=meta.get("url") or ""))
                    cover_sent = True
                except Exception as e:
                    print(f"[batch] cover send error: {e}")
    finally:
        shutil.rmtree(cover_tmpdir, ignore_errors=True)
    if not cover_sent:
        cover_caption = tr(chat_id, "batch_cover_sent",
                           kind=kind_display, title=meta.get("title") or "",
                           count=total, url=meta.get("url") or "")
        bot.send_message(chat_id, cover_caption)

    # 2) progress message WITH cancel button
    cancel_kb = InlineKeyboardMarkup()
    cancel_kb.row(btn(tr(chat_id, "batch_cancel"), callback_data="batch_cancel", style="danger"))
    prog_msg = bot.send_message(chat_id, tr(chat_id, "batch_starting", count=total), reply_markup=cancel_kb)
    prog_id = prog_msg.message_id
    last_update = 0

    done = 0
    failed = []  # list of (title, error)
    done_indices = []  # indices (1-based) of successfully sent tracks
    archive_sent_items = []  # ordered copies for the archive channel
    remaining_for_archive = []

    plat_for_kind = "spotify" if kind in ("spotify", "spotify_artist") else "soundcloud"
    quality = archive_quality_for(chat_id, plat_for_kind)
    collection_url = meta.get("url") or ""
    if kind in ("spotify_artist", "soundcloud_artist"):
        collection_key = f"{plat_for_kind}:artist:{collection_url or meta.get('title')}"
        artist_key = f"{plat_for_kind}:artist:{(meta.get('artist') or meta.get('title') or '').strip().lower()}"
    elif kind in ("spotify",):
        collection_key = f"{plat_for_kind}:album:{collection_url or meta.get('title')}"
        artist_key = f"{plat_for_kind}:artist:{(meta.get('artist') or '').strip().lower()}" if meta.get("artist") else None
    else:
        collection_key = f"{plat_for_kind}:playlist:{collection_url or meta.get('title')}"
        artist_key = None

    # === Persist batch state for resume-after-restart ===
    def _persist(status):
        try:
            batch_state_save(chat_id, kind, "", meta.get("title") or "",
                             meta.get("artist") or "", meta.get("cover") or "",
                             meta.get("url") or "", total, tracks, done_indices, failed, status)
        except Exception as e:
            print(f"[batch] persist error: {e}")
    _persist("running")

    for i, trk in enumerate(tracks, start=1):
        # === CHECK CANCEL before each item ===
        if cancel_event.is_set():
            break

        title = trk.get("title") or "Unknown"
        artist = trk.get("artist") or "Unknown"
        current_label = f"{artist} - {title}"

        # === Duplicate skip ===
        if dedupe_on:
            dupe_key = (artist.strip().lower(), title.strip().lower())
            if dupe_key in seen_keys:
                skipped_dupes += 1
                print(f"[batch] skip duplicate ({i}/{total}): {current_label[:40]}")
                continue
            seen_keys.add(dupe_key)

        # update progress (rate-limited) with visual bar + failed list so far
        now = time.time()
        pct = int((i - 1) * 100 / total) if total > 0 else 0
        if now - last_update >= 2.0 or i == 1 or i == total:
            try:
                bar = _make_progress_bar(pct)
                # show failed tracks below the bar (compact cap to keep msg short)
                failed_str = _format_failed_list(chat_id, failed, max_chars=1500) if failed else ""
                safe_edit_message(
                    tr(chat_id, "batch_progress_cancelable", bar=bar, pct=pct, done=done, total=total, current=current_label[:40], failed=failed_str),
                    chat_id, prog_id, reply_markup=cancel_kb
                )
                last_update = now
            except Exception:
                pass

        tmpdir = None
        try:
            hit = _try_archive_hit_for_track(
                chat_id, plat_for_kind, trk, quality, trk.get("url"),
            )
            if hit:
                send_info = hit
                item = True
                platform = "Spotify" if plat_for_kind == "spotify" else "SoundCloud"
            else:
                tmpdir = tempfile.mkdtemp(prefix=f"batch_{kind}_")
                if kind in ("spotify", "spotify_artist"):
                    track_meta = {
                        "title": title, "artist": artist, "album": trk.get("album"),
                        "duration": trk.get("duration") or 0, "cover": trk.get("cover"),
                        "track_number": trk.get("track_number"),
                        "id": trk.get("id"), "url": trk.get("url"),
                        "year": trk.get("year"), "release_date": trk.get("release_date"),
                    }
                    item = download_spotify_track_audio(track_meta, tmpdir, chat_id=chat_id)
                    platform = "Spotify"
                else:
                    track_url = trk.get("url")
                    if not track_url:
                        raise RuntimeError("no url")
                    res = download_soundcloud_with_retry(track_url, tmpdir, get_user_quality(chat_id),
                                                         is_search=False, max_retries=3)
                    if not res.get("ok") or not res.get("item"):
                        if res.get("drm"):
                            print(f"[batch] SC DRM detected, YouTube fallback for: {current_label}")
                            track_meta = {
                                "title": title, "artist": artist,
                                "duration": trk.get("duration") or 0,
                                "thumb": trk.get("thumb") or trk.get("cover"),
                            }
                            item = _sc_youtube_fallback(track_meta, tmpdir)
                            if not item:
                                raise RuntimeError("DRM + YouTube fallback failed")
                        else:
                            raise RuntimeError(res.get("error", "download failed"))
                    else:
                        item = res["item"]
                    platform = "SoundCloud"
                send_info = _send_with_retry(chat_id, item, platform, trk.get("url") or item.get("url") or meta.get("url"))

            if item:
                done += 1
                done_indices.append(i)
                if isinstance(send_info, dict):
                    if send_info.get("already_archived"):
                        pass
                    elif send_info.get("from_archive"):
                        rec = get_archive_record(send_info.get("platform"), send_info.get("track_key"), quality)
                        if rec:
                            archive_sent_items.append({
                                "from_chat_id": rec["archive_chat_id"],
                                "message_id": rec["archive_msg_id"],
                                "platform": send_info.get("platform"),
                                "track_key": send_info.get("track_key"),
                                "file_type": "audio",
                                "title": send_info.get("title"),
                                "artist": send_info.get("artist"),
                                "duration": send_info.get("duration"),
                                "size": send_info.get("size"),
                            })
                        else:
                            remaining_for_archive.append(trk)
                    elif send_info.get("message_id"):
                        archive_sent_items.append(send_info)
                    else:
                        remaining_for_archive.append(trk)
                src = "archive" if isinstance(send_info, dict) and send_info.get("from_archive") else "download"
                print(f"[batch] {done}/{total} OK ({src}): {current_label[:40]}")
                _persist("running")
            else:
                failed.append((current_label, "no audio"))
                remaining_for_archive.append(trk)
                print(f"[batch] FAILED ({i}/{total}): {current_label[:40]} — no audio")
        except Exception as e:
            err_msg = str(e)[:80]
            failed.append((current_label, err_msg))
            remaining_for_archive.append(trk)
            print(f"[batch] FAILED ({i}/{total}): {current_label[:40]} — {err_msg}")
        finally:
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
        time.sleep(0.15)

    if cancel_event.is_set():
        for j in range(i, total + 1):
            if j not in done_indices and 1 <= j <= len(tracks):
                remaining_for_archive.append(tracks[j - 1])

    if ENABLE_MEDIA_ARCHIVE and (archive_sent_items or remaining_for_archive):
        enqueue_archive_job({
            "type": "collection",
            "kind": kind,
            "quality": quality,
            "collection_key": collection_key,
            "artist_key": artist_key,
            "title": meta.get("title"),
            "artist": meta.get("artist"),
            "url": collection_url,
            "chat_id": chat_id,
            "items": archive_sent_items,
            "remaining": remaining_for_archive,
        })

    # 3) final summary
    errors_str = _format_failed_list(chat_id, failed)

    if cancel_event.is_set():
        _persist("paused")
        final_text = tr(chat_id, "batch_cancelled", done=done, total=total, errors=errors_str)
    else:
        _persist("done")
        final_text = tr(chat_id, "batch_done", done=done, total=total, errors=errors_str)
        if skipped_dupes:
            final_text += f"\n\n🔁 {skipped_dupes} " + ("ترک تکراری رد شد" if lang == "fa" else "duplicate tracks skipped")

    # update progress to 100% or cancelled
    try:
        bar = _make_progress_bar(100 if not cancel_event.is_set() else int(done * 100 / total) if total > 0 else 0)
        if cancel_event.is_set():
            safe_edit_message(final_text, chat_id, prog_id)
        else:
            # final 100% progress with the complete failed list (larger cap)
            failed_str = _format_failed_list(chat_id, failed, max_chars=3000) if failed else ""
            safe_edit_message(
                tr(chat_id, "batch_progress_cancelable", bar=bar, pct=100, done=done, total=total, current="—", failed=failed_str),
                chat_id, prog_id
            )
    except Exception:
        pass

    # send final summary as a new message
    bot.send_message(chat_id, final_text)

    # clear cancel flag + caches
    _clear_batch_cancel(chat_id)
    if not cancel_event.is_set():
        # fully completed → clear persisted state + caches
        batch_state_clear(chat_id)
        if kind in ("spotify", "spotify_artist"):
            clear_spotify_cache(chat_id)
        clear_batch_meta(chat_id)
    else:
        # cancelled/paused → keep batch_state (so user can resume), but clear caches
        if kind in ("spotify", "spotify_artist"):
            clear_spotify_cache(chat_id)
        clear_batch_meta(chat_id)

# ── 7.5  _download_one_batch_track ──────────────────────────────────────
def _download_one_batch_track(kind, trk, chat_id, tmpdir):
    """Download a single track in a batch context. Returns (item, platform) or raises."""
    title = trk.get("title") or "Unknown"
    artist = trk.get("artist") or "Unknown"
    if kind in ("spotify", "spotify_artist"):
        track_meta = {
            "title": title, "artist": artist, "album": trk.get("album"),
            "duration": trk.get("duration") or 0, "cover": trk.get("cover"),
            "track_number": trk.get("track_number"),
            "id": trk.get("id"), "url": trk.get("url"),
            "year": trk.get("year"), "release_date": trk.get("release_date"),
        }
        item = download_spotify_track_audio(track_meta, tmpdir, chat_id=chat_id)
        return item, "Spotify"
    else:
        track_url = trk.get("url")
        if not track_url:
            raise RuntimeError("no url")
        res = download_soundcloud_with_retry(track_url, tmpdir, get_user_quality(chat_id),
                                             is_search=False, max_retries=1)
        if not res.get("ok") or not res.get("item"):
            if res.get("drm"):
                track_meta = {"title": title, "artist": artist,
                              "duration": trk.get("duration") or 0,
                              "thumb": trk.get("thumb") or trk.get("cover")}
                item = _sc_youtube_fallback(track_meta, tmpdir)
                if not item:
                    raise RuntimeError("DRM + YouTube fallback failed")
                return item, "SoundCloud"
            raise RuntimeError(res.get("error", "download failed"))
        return res["item"], "SoundCloud"

# ── 7.6  resume_batch ───────────────────────────────────────────────────
def resume_batch(chat_id):
    """Resume the most recent paused/interrupted batch for this chat."""
    state = batch_state_get_running(chat_id)
    if not state:
        bot.send_message(chat_id, tr(chat_id, "batch_no_data"))
        return
    tracks = state.get("tracks") or []
    total = state.get("total") or len(tracks)
    kind = state.get("kind") or "spotify"
    done_set = set(state.get("done") or [])
    failed = list(state.get("failed") or [])
    meta = {"title": state.get("title"), "artist": state.get("artist"),
            "cover": state.get("cover"), "url": state.get("url")}
    lang = get_user_lang(chat_id) or "en"

    cancel_event = _threading_mod.Event()
    _batch_cancel_flags[chat_id] = cancel_event

    cancel_kb = InlineKeyboardMarkup()
    cancel_kb.row(btn(tr(chat_id, "batch_cancel"), callback_data="batch_cancel", style="danger"))
    remaining = [i for i in range(1, total + 1) if i not in done_set]
    prog_msg = bot.send_message(
        chat_id,
        (f"▶️ ادامه دانلود ({len(done_set)}/{total} انجام شده، {len(remaining)} باقی‌مانده)" if lang == "fa"
         else f"▶️ Resuming ({len(done_set)}/{total} done, {len(remaining)} left)"),
        reply_markup=cancel_kb)
    prog_id = prog_msg.message_id
    last_update = 0
    done = len(done_set)
    done_indices = sorted(done_set)

    dedupe_on = get_dedupe_setting(chat_id)
    seen_keys = set()
    if dedupe_on:
        for i in done_indices:
            if 1 <= i <= len(tracks):
                t = tracks[i-1]
                seen_keys.add(((t.get("artist") or "").strip().lower(), (t.get("title") or "").strip().lower()))

    skipped_dupes = 0
    archive_sent_items = []
    remaining_for_archive = []
    plat_for_kind = "spotify" if kind in ("spotify", "spotify_artist") else "soundcloud"
    quality = archive_quality_for(chat_id, plat_for_kind)
    collection_url = meta.get("url") or ""
    if kind in ("spotify_artist", "soundcloud_artist"):
        collection_key = f"{plat_for_kind}:artist:{collection_url or meta.get('title')}"
        artist_key = f"{plat_for_kind}:artist:{(meta.get('artist') or meta.get('title') or '').strip().lower()}"
    elif kind in ("spotify",):
        collection_key = f"{plat_for_kind}:album:{collection_url or meta.get('title')}"
        artist_key = f"{plat_for_kind}:artist:{(meta.get('artist') or '').strip().lower()}" if meta.get("artist") else None
    else:
        collection_key = f"{plat_for_kind}:playlist:{collection_url or meta.get('title')}"
        artist_key = None
    for i in range(1, total + 1):
        if cancel_event.is_set():
            break
        if i in done_set:
            continue
        if i < 1 or i > len(tracks):
            continue
        trk = tracks[i-1]
        title = trk.get("title") or "Unknown"
        artist = trk.get("artist") or "Unknown"
        current_label = f"{artist} - {title}"
        if dedupe_on:
            dk = (artist.strip().lower(), title.strip().lower())
            if dk in seen_keys:
                skipped_dupes += 1
                continue
            seen_keys.add(dk)

        now = time.time()
        pct = int((i - 1) * 100 / total) if total > 0 else 0
        if now - last_update >= 2.0 or i == total:
            try:
                bar = _make_progress_bar(pct)
                extra = f" (+{skipped_dupes} dup)" if skipped_dupes else ""
                safe_edit_message(
                    tr(chat_id, "batch_progress_cancelable", bar=bar, pct=pct, done=done, total=total, current=(current_label[:36] + extra), failed=""),
                    chat_id, prog_id, reply_markup=cancel_kb)
                last_update = now
            except Exception:
                pass

        tmpdir = None
        try:
            hit = _try_archive_hit_for_track(
                chat_id, plat_for_kind, trk, quality, trk.get("url"),
            )
            if hit:
                send_info = hit
                item = True
            else:
                tmpdir = tempfile.mkdtemp(prefix=f"batch_resume_{kind}_")
                item, platform = _download_one_batch_track(kind, trk, chat_id, tmpdir)
                send_info = _send_with_retry(chat_id, item, platform, trk.get("url") or item.get("url") or meta.get("url")) if item else None
            if item:
                done += 1
                done_indices.append(i)
                done_set.add(i)
                if isinstance(send_info, dict):
                    if send_info.get("already_archived"):
                        pass
                    elif send_info.get("from_archive"):
                        rec = get_archive_record(send_info.get("platform"), send_info.get("track_key"), quality)
                        if rec:
                            archive_sent_items.append({
                                "from_chat_id": rec["archive_chat_id"],
                                "message_id": rec["archive_msg_id"],
                                "platform": send_info.get("platform"),
                                "track_key": send_info.get("track_key"),
                                "file_type": "audio",
                                "title": send_info.get("title"),
                                "artist": send_info.get("artist"),
                                "duration": send_info.get("duration"),
                                "size": send_info.get("size"),
                            })
                        else:
                            remaining_for_archive.append(trk)
                    elif send_info.get("message_id"):
                        archive_sent_items.append(send_info)
                    else:
                        remaining_for_archive.append(trk)
                batch_state_save(chat_id, kind, "", meta.get("title") or "",
                                 meta.get("artist") or "", meta.get("cover") or "",
                                 meta.get("url") or "", total, tracks, done_indices, failed, "running")
        except Exception as e:
            err_msg = str(e)[:80]
            failed.append((current_label, err_msg))
            remaining_for_archive.append(trk)
            print(f"[batch resume] FAILED ({i}/{total}): {current_label[:40]} — {err_msg}")
        finally:
            if tmpdir and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
        time.sleep(0.15)

    if cancel_event.is_set():
        for j in range(1, total + 1):
            if j not in done_set and 1 <= j <= len(tracks):
                remaining_for_archive.append(tracks[j - 1])
    if ENABLE_MEDIA_ARCHIVE and (archive_sent_items or remaining_for_archive):
        enqueue_archive_job({
            "type": "collection",
            "kind": kind,
            "quality": quality,
            "collection_key": collection_key,
            "artist_key": artist_key,
            "title": meta.get("title"),
            "artist": meta.get("artist"),
            "url": collection_url,
            "chat_id": chat_id,
            "items": archive_sent_items,
            "remaining": remaining_for_archive,
        })

    errors_str = _format_failed_list(chat_id, failed)
    if cancel_event.is_set():
        batch_state_mark(chat_id, "paused")
        final_text = tr(chat_id, "batch_cancelled", done=done, total=total, errors=errors_str)
    else:
        batch_state_mark(chat_id, "done")
        final_text = tr(chat_id, "batch_done", done=done, total=total, errors=errors_str)
        if skipped_dupes:
            final_text += f"\n\n🔁 {skipped_dupes} " + ("ترک تکراری رد شد" if lang == "fa" else "duplicate tracks skipped")
    try:
        safe_edit_message(final_text, chat_id, prog_id)
    except Exception:
        pass
    bot.send_message(chat_id, final_text)
    _clear_batch_cancel(chat_id)
    if not cancel_event.is_set():
        batch_state_clear(chat_id)

# ── 7.7  offer_resume_on_startup ────────────────────────────────────────
def offer_resume_on_startup():
    """On bot startup, notify users whose batches were interrupted (status='running')
    that they can resume."""
    try:
        running = batch_state_get_all_running()
        for st in running:
            chat_id = st["chat_id"]
            done = len(st.get("done") or [])
            total = st.get("total") or 0
            title = st.get("title") or ""
            lang = get_user_lang(chat_id) or "en"
            try:
                kb = InlineKeyboardMarkup()
                kb.row(btn("▶️ ادامه دانلود" if lang == "fa" else "▶️ Resume",
                           callback_data="batch_resume", style="success"))
                kb.row(btn("🗑 حذف" if lang == "fa" else "🗑 Discard",
                           callback_data="batch_discard", style="danger"))
                msg = (f"♻️ دانلود ناتمام پیدا شد:\n<b>{title}</b>\n{done}/{total} " +
                       ("انجام شده" if lang == "fa" else "done")) if lang == "fa" else \
                      (f"♻️ Interrupted download found:\n<b>{title}</b>\n{done}/{total} done")
                bot.send_message(chat_id, msg, reply_markup=kb)
            except Exception as e:
                print(f"[resume] notify {chat_id} error: {e}")
    except Exception as e:
        print(f"[resume] startup error: {e}")


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 8 — TELEGRAM BOT: COMMANDS, CALLBACKS, MESSAGE ROUTER        ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 8.1  Commands (@bot.message_handler for /start, /lang, /settings, /quality, /stats, /search) ────
# Telegram Downloader Bot: Enhanced Version - Part 5
# Main Commands, Handlers, File Senders, and Flask Server

# ===== Commands =====
@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    lang = get_user_lang(chat_id)

    # First-time users: ask for language
    if not lang or lang not in LANGS:
        welcome_text = (
            f"👋 {BOT_NICKNAME}\n\n"
            "🌐 خوش آمدید! / Welcome!\n\n"
            "لطفاً زبان خود را انتخاب کنید:\n"
            "Please select your language:"
        )
        bot.send_message(chat_id, welcome_text, reply_markup=lang_keyboard())
        return

    # Returning users: skip language prompt, go straight to main menu
    if not is_member(chat_id):
        send_forced_join_prompt(chat_id)
        return

    send_main_menu(chat_id, greeting=True)
@bot.message_handler(commands=["lang"])
def cmd_lang(message):
    chat_id = message.chat.id
    if not is_member(chat_id):
        bot.send_message(chat_id, tr(chat_id, "must_join", chan=CHANNEL_USERNAME), reply_markup=join_keyboard(chat_id))
        return
    # open the language section of settings
    bot.send_message(chat_id, tr(chat_id, "settings_lang_prompt"), reply_markup=create_language_keyboard(chat_id))
@bot.message_handler(commands=["settings"])
def cmd_settings(message):
    chat_id = message.chat.id
    if not is_member(chat_id):
        bot.send_message(chat_id, tr(chat_id, "must_join", chan=CHANNEL_USERNAME), reply_markup=join_keyboard(chat_id))
        return
    bot.send_message(chat_id, tr(chat_id, "settings_title") + "\n" + tr(chat_id, "settings_intro"),
                    reply_markup=create_settings_keyboard(chat_id))
@bot.message_handler(commands=["quality"])
def cmd_quality(message):
    chat_id = message.chat.id
    if not is_member(chat_id):
        bot.send_message(chat_id, tr(chat_id, "must_join", chan=CHANNEL_USERNAME), reply_markup=join_keyboard(chat_id))
        return
    # redirect to the new settings page
    bot.send_message(chat_id, tr(chat_id, "settings_title") + "\n" + tr(chat_id, "settings_intro"),
                    reply_markup=create_settings_keyboard(chat_id))
@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    chat_id = message.chat.id
    if not is_member(chat_id):
        bot.send_message(chat_id, tr(chat_id, "must_join", chan=CHANNEL_USERNAME), reply_markup=join_keyboard(chat_id))
        return

    send_stats_main(chat_id)
@bot.message_handler(commands=["search"])
def cmd_search(message):
    chat_id = message.chat.id
    if not is_member(chat_id):
        bot.send_message(chat_id, tr(chat_id, "must_join", chan=CHANNEL_USERNAME), reply_markup=join_keyboard(chat_id))
        return
    query = message.text.replace("/search", "").strip()
    if not query:
        bot.send_message(chat_id, tr(chat_id, "search_no_query"))
        return
    # Route to selected search engine
    engine = get_search_engine(chat_id)
    if engine == "spotify":
        do_search_spotify(chat_id, query)
    else:
        do_search(chat_id, query)

# ── 8.2  The single @bot.callback_query_handler on_callback (branches: language → quality → search engine → dedupe → batch resume/discard → re-download → artist → spotify → soundcloud → sc_album → main menu → settings → stats → noop) ────
# ===== Callbacks =====
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    chat_id = call.message.chat.id
    data = call.data or ""
    lang = get_user_lang(chat_id) or "en"

    # Handle initial language selection
    if data.startswith("start_lang:"):
        _, lang = data.split(":", 1)
        if lang in LANGS:
            set_user_lang(chat_id, lang)
            bot.answer_callback_query(call.id, f"Language set to {lang}")

            if not is_member(chat_id):
                send_forced_join_prompt(chat_id)
            else:
                send_main_messages(chat_id)
        return

    # Handle YouTube quality selection
    if data.startswith("yt_quality:"):
        handle_youtube_quality_selection(call)
        return

    # Handle YouTube Shorts selection
    if data.startswith("yt_shorts:"):
        handle_youtube_shorts_selection(call)
        return

    # Handle YouTube cancel (quality/shorts picker)
    if data == "yt_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception as e:
            print(f"yt_cancel delete error: {e}")
        try:
            clear_youtube_quality_cache(chat_id)
        except Exception:
            pass
        return

    # Handle statistics callbacks - with message editing
    if data.startswith("stats:"):
        _, action = data.split(":", 1)

        if action == "main":
            edit_stats_main(chat_id, call.message.message_id)
        elif action == "close":
            # Delete stats message
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception as e:
                print(f"Error deleting stats message: {e}")
        elif action == "top_users_all":
            send_top_users_stats(chat_id, call.message.message_id, 'all')
        elif action == "top_users_daily":
            send_top_users_stats(chat_id, call.message.message_id, 'daily')
        elif action == "top_users_weekly":
            send_top_users_stats(chat_id, call.message.message_id, 'weekly')
        elif action == "top_platforms_all":
            send_top_platforms_stats(chat_id, call.message.message_id, 'all')
        elif action == "top_platforms_daily":
            send_top_platforms_stats(chat_id, call.message.message_id, 'daily')
        elif action == "top_platforms_weekly":
            send_top_platforms_stats(chat_id, call.message.message_id, 'weekly')

        bot.answer_callback_query(call.id)
        return

    # Handle regular callbacks
    if data.startswith("lang:"):
        _, lang = data.split(":", 1)
        if lang in LANGS:
            set_user_lang(chat_id, lang)
            bot.answer_callback_query(call.id, tr(chat_id, "lang_set", lang=lang))
            send_features_message(chat_id)
    elif data.startswith("quality:"):
        _, q = data.split(":", 1)
        if q in ("high", "low"):
            set_user_quality(chat_id, q)
            bot.answer_callback_query(call.id, tr(chat_id, "quality_set", q=q))
    elif data.startswith("pick:"):
        # pick:<panel_id>:<idx>  OR  pick:<idx>  (legacy)
        parts = data.split(":")
        try:
            if len(parts) >= 3:
                pid = parts[1]; idx = int(parts[2])
            else:
                pid = "_"; idx = int(parts[1])
        except Exception:
            bot.answer_callback_query(call.id, "Invalid choice")
            return
        choice = None
        if pid and pid != "_":
            _k, tracks, _m = panel_get(chat_id, pid)
            if tracks and 0 <= idx < len(tracks):
                choice = tracks[idx]
        if not choice:
            choice = get_search_choice(chat_id, idx)
        bot.answer_callback_query(call.id, "OK")
        if choice:
            enqueue_user_job(chat_id, handle_download_soundcloud, chat_id, choice.get("url"))
        else:
            bot.send_message(chat_id, tr(chat_id, "error", err="choice expired"))
    elif data.startswith("playlist_pick:"):
        # playlist_pick:<panel_id>:<idx>  OR  playlist_pick:<idx>
        parts = data.split(":")
        try:
            if len(parts) >= 3:
                pid = parts[1]; idx = int(parts[2])
            else:
                pid = "_"; idx = int(parts[1])
        except Exception:
            bot.answer_callback_query(call.id, "Invalid choice")
            return
        choice = None
        if pid and pid != "_":
            _k, tracks, _m = panel_get(chat_id, pid)
            if tracks and 0 <= idx < len(tracks):
                choice = tracks[idx]
        if not choice:
            choice = get_playlist_choice(chat_id, idx)
        bot.answer_callback_query(call.id, "OK")
        if choice:
            enqueue_user_job(chat_id, handle_download_soundcloud, chat_id, choice.get("url"))
        else:
            bot.send_message(chat_id, tr(chat_id, "error", err="choice expired"))
    elif data.startswith("search_page:"):
        # search_page:<panel_id>:<page>  OR  search_page:<page>
        parts = data.split(":")
        try:
            if len(parts) >= 3:
                pid = parts[1]; page = int(parts[2])
            else:
                pid = "_"; page = int(parts[1])
        except Exception:
            bot.answer_callback_query(call.id, "Invalid page")
            return
        choices = []
        if pid and pid != "_":
            _k, tracks, _m = panel_get(chat_id, pid)
            choices = tracks or []
        if not choices:
            with db_pool.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT url, title, artist, duration FROM search_cache WHERE chat_id=? ORDER BY idx", (chat_id,))
                rows = c.fetchall()
            choices = [{"url": r[0], "title": r[1], "artist": r[2], "duration": r[3]} for r in rows]
        if choices:
            kb = create_paginated_keyboard(choices, chat_id, page, 10, "search", panel_id=(pid if pid != "_" else None))
            try:
                bot.edit_message_text(tr(chat_id, "pick_from_results"), call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception as e:
                print(f"search_page edit error: {e}")
        bot.answer_callback_query(call.id)
    elif data.startswith("playlist_page:"):
        parts = data.split(":")
        try:
            if len(parts) >= 3:
                pid = parts[1]; page = int(parts[2])
            else:
                pid = "_"; page = int(parts[1])
        except Exception:
            bot.answer_callback_query(call.id, "Invalid page")
            return
        choices = []
        if pid and pid != "_":
            _k, tracks, _m = panel_get(chat_id, pid)
            choices = tracks or []
        if not choices:
            with db_pool.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT url, title, artist, duration FROM playlist_cache WHERE chat_id=? ORDER BY idx", (chat_id,))
                rows = c.fetchall()
            choices = [{"url": r[0], "title": r[1], "artist": r[2], "duration": r[3]} for r in rows]
        if choices:
            kb = create_paginated_keyboard(choices, chat_id, page, 10, "playlist", panel_id=(pid if pid != "_" else None))
            try:
                bot.edit_message_text(tr(chat_id, "playlist_song_selection"), call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception as e:
                print(f"playlist_page edit error: {e}")
        bot.answer_callback_query(call.id)
    elif data.startswith("sp_pick:"):
        # sp_pick:<panel_id>:<idx>  OR  sp_pick:<idx>
        parts = data.split(":")
        try:
            if len(parts) >= 3:
                pid = parts[1]; idx = int(parts[2])
            else:
                pid = "_"; idx = int(parts[1])
        except Exception:
            bot.answer_callback_query(call.id, "Invalid")
            return
        choice = None
        if pid and pid != "_":
            _k, tracks, _m = panel_get(chat_id, pid)
            if tracks and 0 <= idx < len(tracks):
                choice = tracks[idx]
        if not choice:
            choice = get_spotify_choice(chat_id, idx)
        bot.answer_callback_query(call.id, "OK")
        if choice and choice.get("id"):
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            enqueue_user_job(chat_id, handle_single_spotify, chat_id, choice["id"])
        else:
            bot.send_message(chat_id, tr(chat_id, "error", err="choice expired"))
    elif data.startswith("sp_page:"):
        # legacy single-arg form
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            bot.answer_callback_query(call.id, "Invalid page")
            return
        tracks = get_all_spotify_choices(chat_id)
        if tracks:
            kb = create_spotify_keyboard(chat_id, tracks, page=page, per_page=10)
            try:
                bot.edit_message_text(tr(chat_id, "spotify_select_track"), call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception as e:
                print(f"sp_page edit error: {e}")
        bot.answer_callback_query(call.id)
    elif data == "sp_batch" or data.startswith("sp_batch:"):
        bot.answer_callback_query(call.id, "OK")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        enqueue_user_job(chat_id, batch_download_and_send, chat_id, "spotify")
    elif data == "sc_batch":
        bot.answer_callback_query(call.id, "OK")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        enqueue_user_job(chat_id, batch_download_and_send, chat_id, "soundcloud")
    elif data == "sp_close" or data.startswith("sp_close:") or data == "sc_close":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception as e:
            print(f"close delete error: {e}")
    # ===== Spotify picker mode (album menu ↔ track list) =====
    elif data.startswith("sp_pickermode:"):
        # sp_pickermode:<panel_id>:<page>  OR  sp_pickermode:<page>
        parts = data.split(":")
        try:
            if len(parts) >= 3:
                pid = parts[1]; page = int(parts[2])
            else:
                pid = "_"; page = int(parts[1])
        except Exception:
            pid = "_"; page = 0
        tracks = _sp_panel_tracks(chat_id, pid if pid != "_" else None)
        if tracks:
            kb = create_spotify_keyboard(chat_id, tracks, page=page, per_page=10, panel_id=(pid if pid != "_" else None))
            try:
                bot.edit_message_text(tr(chat_id, "spotify_select_track"), call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception as e:
                print(f"sp_pickermode edit error: {e}")
        bot.answer_callback_query(call.id)
    # ===== SoundCloud picker mode (album menu ↔ track list) =====
    elif data.startswith("sc_pickermode:"):
        parts = data.split(":")
        try:
            if len(parts) >= 2:
                pid = parts[1]
            else:
                pid = "_"
        except Exception:
            pid = "_"
        # sc_pickermode currently always opens at page 0
        choices = _sc_panel_tracks(chat_id, pid if pid != "_" else None)
        if not choices:
            with db_pool.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT url, title, artist, duration FROM playlist_cache WHERE chat_id=? ORDER BY idx", (chat_id,))
                rows = c.fetchall()
            choices = [{"url": r[0], "title": r[1], "artist": r[2], "duration": r[3]} for r in rows]
        if choices:
            kb = create_paginated_keyboard(choices, chat_id, 0, 10, "playlist", panel_id=(pid if pid != "_" else None))
            try:
                bot.edit_message_text(tr(chat_id, "playlist_song_selection"), call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception as e:
                print(f"sc_pickermode edit error: {e}")
        bot.answer_callback_query(call.id)
    elif data == "sc_album_menu" or data.startswith("sc_album_menu:"):
        parts = data.split(":")
        pid = parts[1] if len(parts) >= 2 else "_"
        # back to the SoundCloud album main 4-button menu
        count = 0
        if pid and pid != "_":
            _k, tracks, _m = panel_get(chat_id, pid)
            count = len(tracks) if tracks else 0
        if not count:
            with db_pool.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM playlist_cache WHERE chat_id=?", (chat_id,))
                row = c.fetchone()
            count = row[0] if row else 0
        if count:
            kb = create_sc_album_menu(chat_id, count)
            try:
                bot.edit_message_text(tr(chat_id, "playlist_song_selection"), call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception as e:
                print(f"sc_album_menu edit error: {e}")
        bot.answer_callback_query(call.id)
    # ===== Main menu callbacks =====
    elif data == "open_main_menu":
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                tr(chat_id, "menu_send_link") + "\n\n" + tr(chat_id, "menu_title"),
                chat_id, call.message.message_id,
                reply_markup=create_main_menu_keyboard(chat_id)
            )
        except Exception as e:
            print(f"open_main_menu edit error: {e}")
    elif data == "open_settings":
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                tr(chat_id, "settings_title") + "\n" + tr(chat_id, "settings_intro"),
                chat_id, call.message.message_id,
                reply_markup=create_settings_keyboard(chat_id)
            )
        except Exception as e:
            print(f"open_settings edit error: {e}")
    elif data == "show_features":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        send_features_message(chat_id)
    elif data == "open_stats":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        send_stats_main(chat_id)
    elif data == "settings_close":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
    # ===== Settings → language =====
    elif data == "open_lang":
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(tr(chat_id, "settings_lang_prompt"),
                                  chat_id, call.message.message_id,
                                  reply_markup=create_language_keyboard(chat_id))
        except Exception as e:
            print(f"open_lang edit error: {e}")
    elif data.startswith("set_lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang in LANGS:
            set_user_lang(chat_id, new_lang)
            bot.answer_callback_query(call.id, tr(chat_id, "lang_set", lang=new_lang))
        else:
            bot.answer_callback_query(call.id, "Invalid")
        try:
            bot.edit_message_text(
                tr(chat_id, "settings_title") + "\n" + tr(chat_id, "settings_intro"),
                chat_id, call.message.message_id,
                reply_markup=create_settings_keyboard(chat_id)
            )
        except Exception as e:
            print(f"set_lang back-to-settings edit error: {e}")
    # ===== Settings → quality section picker =====
    elif data.startswith("qual_section:"):
        pkey = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        lang = get_user_lang(chat_id) or "en"
        platform_name = _platform_display_name(pkey, lang)
        try:
            bot.edit_message_text(
                tr(chat_id, "settings_quality_prompt", platform=platform_name),
                chat_id, call.message.message_id,
                reply_markup=create_quality_picker_keyboard(chat_id, pkey)
            )
        except Exception as e:
            print(f"qual_section edit error: {e}")
    elif data.startswith("setqual:"):
        parts = data.split(":")
        if len(parts) >= 3:
            pkey = parts[1]
            value = parts[2]
            if pkey in PLATFORM_QUALITIES and any(v == value for v, _, _, _ in PLATFORM_QUALITIES[pkey]):
                set_platform_quality(chat_id, pkey, value)
                lang = get_user_lang(chat_id) or "en"
                platform_name = _platform_display_name(pkey, lang)
                val_label = value
                for v, fa, en, _ in PLATFORM_QUALITIES[pkey]:
                    if v == value:
                        val_label = fa if lang == "fa" else en
                        break
                bot.answer_callback_query(call.id, tr(chat_id, "settings_quality_set", platform=platform_name, value=val_label))
            else:
                bot.answer_callback_query(call.id, "Invalid")
        try:
            bot.edit_message_text(
                tr(chat_id, "settings_title") + "\n" + tr(chat_id, "settings_intro"),
                chat_id, call.message.message_id,
                reply_markup=create_settings_keyboard(chat_id)
            )
        except Exception as e:
            print(f"setqual back-to-settings edit error: {e}")
    # ===== Search engine callbacks =====
    elif data == "start_search":
        # Main menu → Search: show search prompt with current engine info
        bot.answer_callback_query(call.id)
        engine = get_search_engine(chat_id)
        engine_label = "Spotify" if engine == "spotify" else "SoundCloud"
        prompt_key = "search_ready_spotify" if engine == "spotify" else "search_ready_soundcloud"
        try:
            kb = InlineKeyboardMarkup()
            kb.row(
                btn(tr(chat_id, "search_engine_title"), callback_data="open_search_engine", style="primary"),
                btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
            )
            bot.edit_message_text(tr(chat_id, prompt_key), chat_id, call.message.message_id, reply_markup=kb)
        except Exception as e:
            print(f"start_search edit error: {e}")
    elif data == "open_search_engine":
        # Settings → Search Engine picker
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                tr(chat_id, "search_engine_prompt"),
                chat_id, call.message.message_id,
                reply_markup=create_search_engine_keyboard(chat_id)
            )
        except Exception as e:
            print(f"open_search_engine edit error: {e}")
    elif data.startswith("set_search_engine:"):
        engine = data.split(":", 1)[1]
        if engine in ("spotify", "soundcloud"):
            set_search_engine(chat_id, engine)
            engine_label = "Spotify" if engine == "spotify" else "SoundCloud"
            bot.answer_callback_query(call.id, tr(chat_id, "search_engine_set", engine=engine_label))
        else:
            bot.answer_callback_query(call.id, tr(chat_id, "ui_invalid"))
        # Show ready-to-search prompt with main-menu button
        prompt_key = "search_ready_spotify" if engine == "spotify" else "search_ready_soundcloud"
        try:
            kb = InlineKeyboardMarkup()
            kb.row(
                btn(tr(chat_id, "search_engine_title"), callback_data="open_search_engine", style="primary"),
                btn(tr(chat_id, "menu_main"), callback_data="open_main_menu", style="primary"),
            )
            bot.edit_message_text(tr(chat_id, prompt_key), chat_id, call.message.message_id, reply_markup=kb)
        except Exception as e:
            print(f"set_search_engine edit error: {e}")
    elif data.startswith("sp_search_page:"):
        # sp_search_page:<panel_id>:<page>  OR  sp_search_page:<page>
        parts = data.split(":")
        try:
            if len(parts) >= 3:
                pid = parts[1]; page = int(parts[2])
            else:
                pid = "_"; page = int(parts[1])
        except Exception:
            bot.answer_callback_query(call.id, "Invalid page")
            return
        tracks = _sp_panel_tracks(chat_id, pid if pid != "_" else None)
        if tracks:
            kb = create_spotify_search_keyboard(chat_id, tracks, page=page, per_page=10, panel_id=(pid if pid != "_" else None))
            try:
                bot.edit_message_text(tr(chat_id, "pick_from_results"), call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception as e:
                print(f"sp_search_page edit error: {e}")
        bot.answer_callback_query(call.id)
    elif data.startswith("search_sc:"):
        # Fallback: search on SoundCloud when Spotify had no results
        query = data.split(":", 1)[1] if ":" in data else ""
        bot.answer_callback_query(call.id, "OK")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        if query:
            do_search(chat_id, query)
    # ===== Batch cancel (real cancel) =====
    elif data == "batch_cancel":
        bot.answer_callback_query(call.id, tr(chat_id, "batch_cancelled_short"))
        _set_batch_cancel(chat_id)
    # ===== Batch resume / discard (after restart or cancel) =====
    elif data == "batch_resume":
        bot.answer_callback_query(call.id, "OK")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        enqueue_user_job(chat_id, resume_batch, chat_id)
    elif data == "batch_discard":
        bot.answer_callback_query(call.id, "OK")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        batch_state_clear(chat_id)
    # ===== Dedupe discography toggle =====
    elif data == "toggle_dedupe":
        new_val = not get_dedupe_setting(chat_id)
        set_dedupe_setting(chat_id, new_val)
        bot.answer_callback_query(call.id, tr(chat_id, "dedupe_set", state=("✅" if new_val else "❌")))
        try:
            bot.edit_message_text(tr(chat_id, "settings_title") + "\n" + tr(chat_id, "settings_intro"),
                                  chat_id, call.message.message_id,
                                  reply_markup=create_settings_keyboard(chat_id))
        except Exception as e:
            print(f"toggle_dedupe edit error: {e}")
    # ===== Artist discography callbacks =====
    elif data.startswith("artist_batch:"):
        # artist_batch:kind:<panel_id>:count  OR  artist_batch:kind:count  (legacy)
        parts = data.split(":")
        akind = parts[1] if len(parts) >= 2 else "spotify_artist"
        if len(parts) >= 4:
            # new format: kind:panel_id:count
            try:
                acount = int(parts[3])
            except Exception:
                acount = 0
        elif len(parts) >= 3:
            try:
                acount = int(parts[2])
            except Exception:
                acount = 0
        else:
            acount = 0
        bot.answer_callback_query(call.id, "OK")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        enqueue_user_job(chat_id, batch_download_and_send, chat_id, akind, count=(acount if acount > 0 else None))
    elif data.startswith("artist_pickermode:"):
        # artist_pickermode:kind:<panel_id>:page  OR  artist_pickermode:kind:page
        parts = data.split(":")
        akind = parts[1] if len(parts) >= 2 else "spotify_artist"
        if len(parts) >= 4:
            try:
                apage = int(parts[3])
                pid = parts[2]
            except Exception:
                apage = int(parts[2]) if len(parts) >= 3 else 0
                pid = "_"
        else:
            try:
                apage = int(parts[2]) if len(parts) >= 3 else 0
            except Exception:
                apage = 0
            pid = "_"
        if akind in ("spotify", "spotify_artist"):
            tracks = _sp_panel_tracks(chat_id, pid if pid != "_" else None)
        else:
            tracks = _sc_panel_tracks(chat_id, pid if pid != "_" else None)
        if tracks:
            kb = create_artist_picker_keyboard(chat_id, akind, tracks, page=apage, per_page=10, panel_id=(pid if pid != "_" else None))
            try:
                bot.edit_message_text(
                    tr(chat_id, "artist_select_track"),
                    chat_id, call.message.message_id, reply_markup=kb
                )
            except Exception as e:
                print(f"artist_pickermode edit error: {e}")
        bot.answer_callback_query(call.id)
    elif data.startswith("artist_pick:"):
        # artist_pick:kind:<panel_id>:idx  OR  artist_pick:kind:idx
        parts = data.split(":")
        akind = parts[1] if len(parts) >= 2 else "spotify_artist"
        if len(parts) >= 4:
            try:
                aidx = int(parts[3])
                pid = parts[2]
            except Exception:
                aidx = -1
                pid = "_"
        elif len(parts) >= 3:
            try:
                aidx = int(parts[2])
            except Exception:
                aidx = -1
            pid = "_"
        else:
            aidx = -1
            pid = "_"
        choice = None
        if pid and pid != "_":
            _k, tracks, _m = panel_get(chat_id, pid)
            if tracks and 0 <= aidx < len(tracks):
                choice = tracks[aidx]
        if akind in ("spotify", "spotify_artist"):
            if not choice:
                choice = get_spotify_choice(chat_id, aidx)
            bot.answer_callback_query(call.id, "OK")
            if choice and choice.get("id"):
                try:
                    bot.delete_message(chat_id, call.message.message_id)
                except Exception:
                    pass
                enqueue_user_job(chat_id, handle_single_spotify, chat_id, choice["id"])
            else:
                bot.send_message(chat_id, tr(chat_id, "error", err="choice expired"))
        else:
            # SoundCloud
            if not choice:
                choice = get_playlist_choice(chat_id, aidx)
            bot.answer_callback_query(call.id, "OK")
            if choice:
                try:
                    bot.delete_message(chat_id, call.message.message_id)
                except Exception:
                    pass
                enqueue_user_job(chat_id, handle_download_soundcloud, chat_id, choice.get("url") or choice.get("id") or "")
            else:
                bot.send_message(chat_id, tr(chat_id, "error", err="choice expired"))
    elif data.startswith("artist_menu:"):
        # back to artist main menu: artist_menu:kind:<panel_id>  OR  artist_menu:kind
        parts = data.split(":")
        akind = parts[1] if len(parts) >= 2 else "spotify_artist"
        pid = parts[2] if len(parts) >= 3 else "_"
        if akind in ("spotify", "spotify_artist"):
            tracks = _sp_panel_tracks(chat_id, pid if pid != "_" else None)
        else:
            tracks = _sc_panel_tracks(chat_id, pid if pid != "_" else None)
        if tracks:
            kb = create_artist_menu(chat_id, akind, len(tracks), panel_id=(pid if pid != "_" else None))
            try:
                bot.edit_message_text(
                    tr(chat_id, "artist_select_track") if len(tracks) <= ARTIST_LARGE_THRESHOLD else tr(chat_id, "artist_large_notice"),
                    chat_id, call.message.message_id, reply_markup=kb
                )
            except Exception as e:
                print(f"artist_menu edit error: {e}")
        bot.answer_callback_query(call.id)
    elif data.startswith("artist_custom:"):
        # start custom count input: artist_custom:kind:<panel_id>  OR  artist_custom:kind
        parts = data.split(":")
        akind = parts[1] if len(parts) >= 2 else "spotify_artist"
        pid = parts[2] if len(parts) >= 3 else "_"
        if akind in ("spotify", "spotify_artist"):
            tracks = _sp_panel_tracks(chat_id, pid if pid != "_" else None)
        else:
            tracks = _sc_panel_tracks(chat_id, pid if pid != "_" else None)
        total = len(tracks)
        _awaiting_custom_count[chat_id] = (akind, total, call.message.message_id, pid)
        bot.answer_callback_query(call.id, "OK")
        try:
            bot.edit_message_text(
                tr(chat_id, "artist_custom_prompt", max=total),
                chat_id, call.message.message_id
            )
        except Exception as e:
            print(f"artist_custom edit error: {e}")
    elif data.startswith("artist_close:") or data == "artist_close":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception as e:
            print(f"artist_close delete error: {e}")
        # Clear awaiting custom count state if present
        _awaiting_custom_count.pop(chat_id, None)
    elif data == "noop":
        bot.answer_callback_query(call.id)

# ── 8.3  Main message handler (@bot.message_handler(func=lambda m: True)) — link routing + custom count input ────
# ===== Main message handler =====
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    chat_id = message.chat.id

    # If user hasn't selected language yet
    if not get_user_lang(chat_id) or get_user_lang(chat_id) not in LANGS:
        welcome_text = (
            f"👋 {BOT_NICKNAME}\n\n"
            "🌐 خوش آمدید! / Welcome!\n\n"
            "لطفاً زبان خود را انتخاب کنید:\n"
            "Please select your language:"
        )
        bot.send_message(chat_id, welcome_text, reply_markup=lang_keyboard())
        return

    # Check membership for users who have selected language
    if not is_member(chat_id):
        send_forced_join_prompt(chat_id)
        return

    text = (message.text or "").strip()
    if not text:
        bot.reply_to(message, tr(chat_id, "invalid_link"))
        return

    # === Handle custom count input for artist discography ===
    if chat_id in _awaiting_custom_count:
        entry = _awaiting_custom_count.pop(chat_id)
        # tuple shape: (akind, total, orig_msg_id, pid)  (pid may be absent in legacy)
        if len(entry) == 4:
            akind, total, orig_msg_id, pid = entry
        else:
            akind, total, orig_msg_id = entry
            pid = "_"
        # Try to parse the number
        try:
            # Remove Persian digits
            cleaned = text.replace("۰","0").replace("۱","1").replace("۲","2").replace("۳","3").replace("۴","4").replace("۵","5").replace("۶","6").replace("۷","7").replace("۸","8").replace("۹","9")
            n = int(cleaned.strip())
        except Exception:
            n = 0
        if n < 1 or n > total:
            bot.send_message(chat_id, tr(chat_id, "artist_custom_invalid", max=total))
            # Re-set the awaiting state so user can try again
            _awaiting_custom_count[chat_id] = (akind, total, orig_msg_id, pid)
            return
        # Valid number — start batch with this count
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        bot.send_message(chat_id, tr(chat_id, "artist_download_first", count=n) if n < total else tr(chat_id, "artist_download_all", count=total))
        enqueue_user_job(chat_id, batch_download_and_send, chat_id, akind, count=(n if n < total else None))
        return

    if text.startswith("http"):
        final_url = resolve_url(text)
        try:
            # Delete user message after starting download
            bot.delete_message(chat_id, message.message_id)
        except Exception as e:
            print(f"Error deleting user message: {e}")

        if "soundcloud.com" in final_url:
            # Check if it's an artist page vs track/playlist
            # Artist: soundcloud.com/USERNAME or soundcloud.com/USERNAME/tracks
            # Track:  soundcloud.com/USERNAME/TRACK-SLUG (2 segments, 2nd != "tracks")
            # Playlist: soundcloud.com/USERNAME/sets/PLAYLIST-SLUG
            url_clean = final_url.replace("https://", "").replace("http://", "").split("?")[0].rstrip("/")
            path_parts = url_clean.split("/")[1:]  # skip domain
            # Remove empty parts
            path_parts = [p for p in path_parts if p]
            is_sc_artist = False
            if len(path_parts) == 1:
                # soundcloud.com/USERNAME → artist
                is_sc_artist = True
            elif len(path_parts) == 2 and path_parts[1].lower() == "tracks":
                # soundcloud.com/USERNAME/tracks → artist
                is_sc_artist = True
            # else: 2+ segments without /tracks → track or playlist

            if is_sc_artist:
                enqueue_user_job(chat_id, handle_soundcloud_artist, chat_id, final_url)
            else:
                enqueue_user_job(chat_id, handle_download_soundcloud, chat_id, final_url)
        elif "spotify.com" in final_url or "spotify:" in final_url:
            # Check if it's an artist link
            m_artist = re.search(r"spotify\.com/artist/([a-zA-Z0-9]+)", final_url)
            if m_artist:
                enqueue_user_job(chat_id, handle_spotify_artist, chat_id, m_artist.group(1))
            else:
                enqueue_user_job(chat_id, handle_download_spotify, chat_id, final_url)
        elif "pinterest.com" in final_url or "pin.it" in final_url:
            enqueue_user_job(chat_id, handle_download_pinterest, chat_id, final_url)
        elif "instagram.com" in final_url or "instagr.am" in final_url:
            enqueue_user_job(chat_id, handle_download_instagram, chat_id, final_url)
        elif "youtube.com" in final_url or "youtu.be" in final_url:
            enqueue_user_job(chat_id, handle_download_youtube, chat_id, final_url)
        elif "tiktok.com" in final_url:
            enqueue_user_job(chat_id, handle_download_tiktok, chat_id, final_url)
        elif "twitter.com" in final_url or "x.com" in final_url or "t.co" in final_url:
            enqueue_user_job(chat_id, handle_download_twitter, chat_id, final_url)
        else:
            bot.send_message(chat_id, tr(chat_id, "error", err="Unsupported link"))
    else:
        # Route to selected search engine
        engine = get_search_engine(chat_id)
        if engine == "spotify":
            do_search_spotify(chat_id, text)
        else:
            do_search(chat_id, text)


# ╔══════════════════════════════════════════════════════════════════╗
# ║  PART 9 — ENTRY POINT                                              ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 9.1  Flask minimal endpoint (app = Flask(...), @app.route('/')) ─────
# ===== Minimal Flask endpoint (for hosting platforms that need an HTTP port) =====
app = Flask(__name__)
@app.route('/')
def home():
    return {"status": "ok", "bot": BOT_NICKNAME, "username": BOT_USERNAME}

# ── 9.2  Main block (if __name__ == '__main__': with _ensure_bot_me(), db_init(), resume-offer thread, flask thread, polling loop) + WSGI else-branch ────
# ===== Main Entry Point =====
if __name__ == '__main__':
    _ensure_bot_me()
    print(f"Starting {BOT_NICKNAME} (@{BOT_USERNAME}) on port {PORT}...")
    print(f"Cookies file available: {COOKIES_AVAILABLE}")
    print(f"Spotify module: {'enabled' if SPOTIFY_ENABLED else 'disabled'}")

    # Start a minimal Flask server in the background (some hosting platforms
    # require an open HTTP port to keep the process alive).
    def run_flask():
        import logging as _flask_log
        _flask_log.getLogger('werkzeug').setLevel(_flask_log.ERROR)
        app.run(host='0.0.0.0', port=PORT, debug=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    db_init()
    _ensure_archive_worker()

    # Offer to resume any batch downloads that were interrupted before restart.
    try:
        threading.Thread(target=lambda: (time.sleep(3), offer_resume_on_startup()), daemon=True).start()
    except Exception:
        pass

    print(f"{BOT_NICKNAME} is up. Polling for updates...")
    # NOTE: we deliberately do NOT `break` out of this loop. pyTelegramBotAPI's
    # polling can return normally (e.g. on certain 409 Conflict / network
    # conditions) without raising — if we `break`-d there, the bot would exit
    # silently. Instead we restart polling after a short pause so the bot
    # stays up forever (which is what long-running deployments expect).
    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except SystemExit as e:
            # pyTelegramBotAPI may raise SystemExit on fatal API errors; we
            # don't want those to kill the process — just restart polling.
            print(f"[Bot] polling requested exit (code={e.code}) — restarting in 5s")
            time.sleep(5)
        except Exception as e:
            print(f"[Bot] polling error: {e} — retrying in 10s")
            time.sleep(10)
        # If polling returned normally, restart after a brief pause
        print("[Bot] polling returned, restarting in 3s...")
        time.sleep(3)
else:
    # For WSGI servers like Gunicorn. Set BOT_NO_AUTOSTART=1 to import this
    # module without auto-starting polling (useful for tests / scripting).
    db_init()
    _ensure_archive_worker()
    if os.environ.get('BOT_NO_AUTOSTART') != '1':
        def run_bot():
            while True:
                try:
                    bot.polling(none_stop=True, timeout=60)
                except SystemExit as e:
                    print(f"[Bot] polling requested exit (code={e.code}) — restarting in 5s")
                    time.sleep(5)
                except Exception as e:
                    print(f"[Bot] polling error: {e} — retrying in 10s")
                    time.sleep(10)
                print("[Bot] polling returned, restarting in 3s...")
                time.sleep(3)

        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
