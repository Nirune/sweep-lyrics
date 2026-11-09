#!/usr/bin/env python3
"""
Sweep Lyrics
─────────────
Auto-scrolling synced lyrics for MPRIS players
(Chrome / YouTube / YT Music / Spotify / mpv / VLC …)

Core deps:  playerctl  +  rich  requests  pillow
    pip install rich requests pillow --break-system-packages

Optional:
    pykakasi          →  'y' romanize (JP)
    numpy             →  vocal onset hybrid
    cmudict           →  better English syllable weights

Kitty zoom (+/-) needs:
    allow_remote_control yes   in ~/.config/kitty/kitty.conf

Usage:
    ./sweep-lyrics.py
"""

import subprocess
import re
import time
import colorsys
import json
import hashlib
import sys
import os
import shutil
import select
import termios
import tty
import math
import tempfile
from pathlib import Path

import requests
from rich.live import Live
from rich.panel import Panel
from rich.align import Align
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from rich.color import Color
from rich.console import Group, Console
from rich.cells import cell_len
from rich.measure import Measurement
from rich.segment import ControlType, Segment

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    import cmudict
    HAVE_CMUDICT = True
except ImportError:
    HAVE_CMUDICT = False

try:
    import pykakasi
    HAVE_PYKAKASI = True
except ImportError:
    HAVE_PYKAKASI = False

try:
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import lyrics_providers
    HAVE_PROVIDERS = True
except Exception:
    HAVE_PROVIDERS = False

LRCLIB_API = "https://lrclib.net/api"
CACHE_DIR = Path.home() / ".cache" / "sweep-lyrics"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

POLL_INTERVAL = 0.02   # ~50 fps wipe
FULL_CHECK_INTERVAL = 1.0
POS_POLL_INTERVAL = 0.2  # how often to actually spawn playerctl for position/status;
                         # frames in between use SmoothClock interpolation
CONTEXT_LINES = 3
MAX_CONTEXT_LINES = 9

# ── visual language ─────────────────────────────────────────────────────
# Cool cyan primary. Terminal-native, distinct from the old magenta pair.
ACCENT        = "bright_cyan"      # borders, titles, progress fill
ARTIST_ACCENT = "bright_white"     # artist name
ACCENT_DIM    = "cyan"             # secondary labels / inactive
KARAOKE_ACCENT = "#00e5ff"         # active karaoke wipe (bright cyan)
WARN_ACCENT   = "bright_yellow"
MUTED         = "dim"

KARAOKE_LEAD = 0.6
KARAOKE_HOLD_MAX = 3.0              # held-word wipe: when the gap from a word's
                                     # start to the next start (word or line end)
                                     # is <= this, the wipe follows the REAL gap
                                     # instead of the syllable-estimate cap, so a
                                     # sustained "Tuhan~" keeps wiping while it's
                                     # held. 0 = off (old capped behaviour).
KARAOKE_SECONDS_PER_WORD = 0.6      # legacy/back-compat only, no longer
                                     # used to size the wipe (see
                                     # KARAOKE_SECONDS_PER_SYLLABLE below)
KARAOKE_SECONDS_PER_SYLLABLE = 0.22 # per-syllable wipe-duration estimate.
                                     # Replaces the old flat per-WORD
                                     # estimate, which gave "Penuhilah"
                                     # (4 syllables) and "lagi" (2
                                     # syllables) the same time budget —
                                     # badly undershooting long/syllable-
                                     # heavy Indonesian words and causing
                                     # the wipe to blast through a line
                                     # then sit fully-lit doing nothing
                                     # for the rest of its time on screen.
FONT_STEP = 1.0
GLOBAL_OFFSET_STEP = 0.10

# --------------------------------------------------------------- shazam mode
# For mashup/compilation videos ("Billboard Top 50 This Week", playlist
# mixes, etc.) MPRIS only exposes the VIDEO's title, which never changes
# as the individual songs inside it play — so title-based lookup can't
# track what's actually playing. Pressing 's' listens to system audio via
# SongRec and identifies the real song.
def _default_monitor():
    """Monitor source of the current default sink; override with SWEEP_SHAZAM_DEVICE."""
    env = os.environ.get("SWEEP_SHAZAM_DEVICE")
    if env:
        return env
    try:
        sink = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if sink:
            return sink + ".monitor"
    except Exception:
        pass
    return "@DEFAULT_MONITOR@"


SHAZAM_DEVICE = _default_monitor()
SHAZAM_LISTEN_TIMEOUT = 20    # seconds to wait for a match
SHAZAM_OFFSET_FUDGE = 2.0     # seconds — extra nudge added on top of the
                               # measured listen+match time, to cover fixed
                               # pipeline latency (audio monitor device
                               # buffering, etc.) that isn't captured by
                               # wall-clock timing alone. If lyrics still
                               # lag behind the audio after identifying,
                               # raise this a bit; if they run ahead, lower
                               # it (or set to 0).
SHAZAM_AUTO_RELOAD_COOLDOWN = 8.0  # seconds — throttle retries after a
                               # failed/timed-out match so SongRec isn't
                               # hammered every frame while still over
                               # the end threshold.
SHAZAM_AUTO_RELOAD_LINE_GRACE = 3.0  # seconds past the last synced line
                               # when lrclib has no duration — keep small
                               # so rescan lands near the real end.
SHAZAM_AUTO_RELOAD_END_GRACE = 2.0  # tiny buffer past lrclib duration
                               # (brief outro hang) without feeling late.
SHAZAM_AUTO_RELOAD_MIN_HOLD = 45.0  # never auto-rescan until the song
                               # has been playing at least this long
                               # after identify — stops wrong-short
                               # durations from looping SongRec early.
SHAZAM_AUTO_RELOAD_MAX_FAILURES = 3  # consecutive failed/no-match auto
                               # re-listen attempts before pausing further
                               # auto-retries (e.g. a silence/ad gap
                               # between songs in the mashup) — falls back
                               # to waiting for a manual 's' press instead
                               # of retrying forever every cooldown tick.
SHAZAM_AUTO_RELOAD_WARN_LEAD = 5.0  # show "re-identifying soon…" this
                               # many seconds *before* the end threshold,
                               # then rescan once pos crosses it.
SHAZAM_HISTORY_FILE = CACHE_DIR / "shazam_history.jsonl"
                               # append-only log of every song Shazam mode
                               # has successfully identified (manual 's'
                               # or auto re-identify), one JSON object per
                               # line: {"ts", "artist", "title"}
SHAZAM_HISTORY_MAX_SHOWN = 15  # how many recent entries the 'h' hotkey
                               # ("recently identified") displays

# Title/duration patterns that strongly suggest a YouTube mix /
# compilation / multi-hour upload — MPRIS title will never match a
# single lrclib track, so we auto-enter SongRec identify mode.
_COMPILATION_RE = re.compile(
    r"\b("
    r"top\s*hits?|best\s*of|greatest\s*hits?|playlist|compilation|"
    r"mashup|mix\b|mixed|medley|nonstop|non-stop|"
    r"\d+\s*hours?|\d+\s*hrs?|hour\s*long|"
    r"lagu\s*galau|lagu\s*hits?|kumpulan|kompilasi|"
    r"spotify\s*202\d|tiktok\s*hits?|viral\s*hits?"
    r")\b",
    re.I,
)
COMPILATION_DURATION_SEC = 15 * 60  # ≥15 min + no LRC → treat as mix


def looks_like_compilation(title: str, duration: int = 0, has_lyrics: bool = False) -> bool:
    """True when the MPRIS title/duration smells like a multi-song
    upload rather than a single track. Used to auto-trigger Shazam
    mode and to show a better empty-state hint."""
    t = title or ""
    if _COMPILATION_RE.search(t):
        return True
    if duration >= COMPILATION_DURATION_SEC and not has_lyrics:
        return True
    # very long generic titles with no real track structure
    if len(t) > 80 and not has_lyrics and ("-" in t or "|" in t):
        return True
    return False

# --------------------------------------------------------- sync offset cache
# Per-track lyric-sync correction (the '[' / ']' / 'r' offset), persisted
# across restarts so a song that needed nudging once doesn't need it
# nudged again next time it plays. Kept as a single small JSON file
# (not one file per track like the lyrics cache) since it's just a
# handful of bytes per entry.
OFFSET_CACHE_FILE = CACHE_DIR / "sync_offsets.json"

# ------------------------------------------------------------- translation
# Optional 't'-toggled translation of the current line, shown dim under
# the active lyric. Uses Google's public (unofficial, no API key)
# translate endpoint — best-effort only: if it ever changes shape or is
# unreachable, translation just silently stays unavailable rather than
# erroring out the whole app.
TRANSLATE_TARGET_LANG = "id"   # language code lyrics get translated INTO
TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_LINE_SEP = " \u241e "  # rare separator joining lines into one
                               # request, so line boundaries can be
                               # recovered from the translated response
TRANSLATE_CACHE_DIR = CACHE_DIR / "translations"
TRANSLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------- cover art
# Optional album/video-thumbnail art shown on the left via Kitty's icat
# kitten. Only attempted inside Kitty; silently skipped everywhere else
# (or if a track has no art / the fetch fails) — the lyrics box just
# uses the full width in that case, same as before this feature existed.
COVER_AVAILABLE = bool(os.environ.get("KITTY_WINDOW_ID")) and shutil.which("kitty") is not None
COVER_ENABLED = COVER_AVAILABLE  # also driven by the 'show_cover' setting (apply_settings)
COVER_CACHE_DIR = CACHE_DIR / "covers"
COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
COVER_COLS = 28        # terminal cells wide (was 24 — felt small/lost)
COVER_ROWS = 14        # terminal cells tall  (was 12)
COVER_MARGIN_LEFT = 2  # cells from the left edge
COVER_MARGIN_TOP = 2   # fallback top when height unknown
COVER_GUTTER = COVER_COLS + COVER_MARGIN_LEFT + 3  # total left offset given
                                                     # to the lyrics panel
                                                     # when a cover is shown
# How many text rows sit under the art (rule + title + artist + genre + badges).
# Used to vertically balance the left column with the Kitty image.
COVER_CAPTION_ROWS = 7

# ---------------------------------------------------------------- playerctl

def playerctl(*args, player=None):
    cmd = ["playerctl"]
    if player:
        cmd += ["-p", player]
    cmd += list(args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def _get_active_window_title() -> str:
    """Best-effort: the currently focused window's title. Used to
    disambiguate between multiple MPRIS players (e.g. several browser
    tabs each reporting "Playing" at once) — playerctl itself has no
    concept of which tab is actually in front. Returns '' if it can't
    be determined (different compositor, tools missing, etc.); callers
    should just fall back to picking the first Playing player."""
    if shutil.which("niri"):
        try:
            out = subprocess.run(
                ["niri", "msg", "--json", "focused-window"],
                capture_output=True, text=True, timeout=1,
            )
            if out.returncode == 0 and out.stdout:
                data = json.loads(out.stdout)
                if data:
                    return data.get("title", "") or ""
        except Exception:
            pass
    if shutil.which("xdotool"):
        try:
            out = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=1,
            )
            if out.returncode == 0:
                return out.stdout.strip()
        except Exception:
            pass
    return ""


def _player_short_name(player_id: str) -> str:
    """chromium.instance123 → chromium, spa_spotify → spotify, etc."""
    if not player_id:
        return "?"
    base = player_id.split(".")[0]
    base = base.replace("spa_", "").replace("instance", "")
    return base or player_id


def _pick_most_active(playing: list[str]) -> str:
    """Among several Playing MPRIS players, prefer the one whose
    position is actually advancing. Falls back to focused-window title
    match, then first in list."""
    now = time.time()
    ranked = []
    for p in playing:
        pos = get_position(p)
        prev = find_active_player._stale.get(p)
        advancing = 0.0
        if prev is not None:
            prev_pos, prev_t = prev
            dt = max(0.05, now - prev_t)
            if abs(pos - prev_pos) >= 0.15:
                advancing = abs(pos - prev_pos) / dt
        find_active_player._stale[p] = (pos, now)
        ranked.append((advancing, p))

    ranked.sort(key=lambda x: x[0], reverse=True)
    if ranked and ranked[0][0] > 0.05:
        return ranked[0][1]

    active_title = _get_active_window_title()
    if active_title:
        for p in playing:
            title = playerctl("metadata", "title", player=p) or ""
            if title and (title in active_title or active_title in title):
                return p
    return playing[0]


def find_active_player(preferred=None):
    """Pick the MPRIS player that is actually making sound.

    Priority:
      1. A Playing player whose position advances (real playback)
      2. Preferred lock — but ONLY while it stays Playing AND advancing
      3. Any other Playing player (never stick to Paused if something
         else is Playing — that was the Adele-stuck-while-Chrome-plays bug)
      4. Preferred if Paused and nothing else is Playing (freeze lyrics)
      5. Any Paused player as last resort
    """
    listed = playerctl("-l")
    if not listed:
        find_active_player._stale.clear()
        return None, None

    players = [p.strip() for p in listed.splitlines() if p.strip()]

    playing = []
    paused = []
    stopped = []
    for p in players:
        status = playerctl("status", player=p)
        if status == "Playing":
            playing.append(p)
        elif status == "Paused":
            paused.append(p)
        elif status == "Stopped":
            stopped.append(p)

    # --- preferred just blipped "Stopped" instead of actually going away.
    # Brave/Chromium-based MPRIS players report "Stopped" transiently
    # during buffering, ad breaks, or a seek, even while audio keeps
    # playing seconds later. Falling straight through to "any Paused
    # player" here used to lock onto some unrelated leftover MPRIS
    # instance (a stale/ghost tab) for that one tick, which changes
    # cur_key and forces a full track reload — then the real player
    # reports Playing again a moment later and it reloads BACK. That
    # rapid reload/reload-back is what looked like the display "looping".
    # Ride out the blip for a short grace window before treating it as a
    # real stop.
    now = time.time()
    if preferred and preferred in stopped:
        stale_since = find_active_player._stopped_since.get(preferred)
        if stale_since is None:
            find_active_player._stopped_since[preferred] = now
            return preferred, "Paused"
        if now - stale_since < STOPPED_GRACE_SECONDS:
            return preferred, "Paused"
        # genuinely stopped for a while now — let it fall through below
    elif preferred:
        find_active_player._stopped_since.pop(preferred, None)

    # --- someone is Playing: never stay locked on a Paused preferred ---
    if playing:

        # If preferred is among Playing and still advancing, keep it
        # (stable lock while you watch lyrics in the terminal).
        if preferred and preferred in playing:
            pos = get_position(preferred)
            now = time.time()
            prev = find_active_player._stale.get(preferred)
            if prev is None:
                find_active_player._stale[preferred] = (pos, now)
                return preferred, "Playing"
            prev_pos, prev_t = prev
            elapsed = now - prev_t
            if elapsed < 1.2:
                find_active_player._stale[preferred] = (pos, now)
                return preferred, "Playing"
            if abs(pos - prev_pos) >= 0.25:
                find_active_player._stale[preferred] = (pos, now)
                return preferred, "Playing"
            # preferred claims Playing but position frozen → drop lock
            find_active_player._stale.pop(preferred, None)

        # Pick the most active Playing player (may be preferred, may not)
        chosen = _pick_most_active(playing)
        return chosen, "Playing"

    # --- nothing Playing: freeze on preferred pause or any pause ---
    if preferred and preferred in paused:
        find_active_player._stale.pop(preferred, None)
        return preferred, "Paused"
    if paused:
        return paused[0], "Paused"
    return None, None


# Per-player (last_position, wall_clock_time) used by find_active_player
# to detect a locked player whose position has stopped advancing despite
# reporting "Playing" — see the staleness check above.
find_active_player._stale = {}

# How long to keep treating a preferred player as merely "Paused" after
# it reports "Stopped" before believing the Stopped status for real. Long
# enough to ride out Brave/Chromium's transient Stopped blips (buffering,
# ad breaks, seeks), short enough that actually closing the tab still
# gets noticed reasonably fast.
STOPPED_GRACE_SECONDS = 4.0

# Per-player wall-clock timestamp of when a "Stopped" blip was first seen,
# used by the grace-period check above.
find_active_player._stopped_since = {}



def get_metadata(player):
    """get_metadata plus a rescue for the YT Music idle-title quirk: while a
    tab is Playing, Brave native MPRIS can keep reporting the generic
    YouTube Music title with no artist for the whole track. In that case
    (and only while Playing) borrow the identity from the
    plasma-browser-integration copy of the same tab. Paused placeholders
    are left alone on purpose, the main loop relies on them to ride out
    the pause/play title flap."""
    meta = _get_metadata_raw(player)
    if not meta or not player or player.startswith("plasma-browser-integration"):
        return meta
    title = clean_youtube_title(meta.get("title") or "").lower().strip()
    artist = (meta.get("artist") or "").lower().strip()
    if not (title in _USELESS_TITLES and (not artist or artist in _USELESS_TITLES)):
        return meta
    if playerctl("status", player=player) != "Playing":
        return meta
    if playerctl("status", player="plasma-browser-integration") != "Playing":
        return meta
    plasma = _get_metadata_raw("plasma-browser-integration")
    if not plasma:
        return meta
    p_title = clean_youtube_title(plasma.get("title") or "").lower().strip()
    if not p_title or p_title in _USELESS_TITLES:
        return meta
    merged = dict(meta)
    for field in ("title", "artist", "album", "art_url", "track_url"):
        if (plasma.get(field) or "").strip():
            merged[field] = plasma[field]
    if not (plasma.get("art_url") or "").strip():
        # art_url from Brave belongs to its idle placeholder (its own icon);
        # blank it so the cover is derived from track_url instead.
        merged["art_url"] = ""
    if (plasma.get("duration") or 0) > 0:
        merged["duration"] = plasma["duration"]
    return merged




def _get_metadata_raw(player):
    """Single batched call instead of separate playerctl invocations."""
    fmt = ("{{xesam:title}}|||{{xesam:artist}}|||{{xesam:album}}|||"
           "{{mpris:length}}|||{{mpris:artUrl}}|||{{xesam:url}}")
    raw = playerctl("metadata", "--format", fmt, player=player)
    if raw is None:
        return None
    parts = raw.split("|||")
    while len(parts) < 6:
        parts.append("")
    title, artist, album, length_us, art_url, track_url = parts[:6]
    try:
        duration = int(length_us) // 1_000_000
    except ValueError:
        duration = 0
    return {"title": title, "artist": artist, "album": album,
            "duration": duration, "art_url": art_url, "track_url": track_url}


def get_position(player):
    pos = playerctl("position", player=player)
    try:
        return float(pos)
    except (TypeError, ValueError):
        return 0.0


class SmoothClock:
    """Interpolates playback position between coarse MPRIS samples.

    Chrome only refreshes its MPRIS position every ~0.5s, so using the raw
    value makes the karaoke wipe jump in steps. We free-run on
    time.monotonic() and only nudge forward when a fresh sample says we're
    behind; samples that merely look stale (clock slightly ahead) are
    ignored so the wipe never goes backwards. Big jumps (seek / new track)
    snap straight to the sample.
    """
    SNAP = 0.9    # |error| above this -> hard resync (seek, track change)
    GAIN = 0.25   # fraction of a "we're behind" error corrected per sample

    def __init__(self):
        self.base_pos = 0.0
        self.base_t = time.monotonic()
        self.last_raw = None
        self.playing = False

    def reset(self):
        self.last_raw = None

    def now_pos(self, now=None):
        if not self.playing:
            return self.base_pos
        if now is None:
            now = time.monotonic()
        return self.base_pos + (now - self.base_t)

    def feed(self, raw, playing):
        now = time.monotonic()
        if self.last_raw is None or not playing or playing != self.playing:
            self.base_pos, self.base_t = raw, now
        elif raw != self.last_raw:
            est = self.now_pos(now)
            err = raw - est
            if abs(err) > self.SNAP:
                self.base_pos, self.base_t = raw, now
            elif err > 0.05:
                self.base_pos = est + err * self.GAIN
                self.base_t = now
        self.last_raw, self.playing = raw, playing


# ---------------------------------------------------------- title cleaning

_TAG_RE = re.compile(
    r"\((official|lyrics?|audio|video|hd|4k|visualizer|mv)[^)]*\)|"
    r"\[(official|lyrics?|audio|video|hd|4k|visualizer|mv)[^\]]*\]",
    re.IGNORECASE,
)


def clean_youtube_title(raw: str) -> str:
    t = raw or ""
    t = re.sub(r"^\(\d+\)\s*", "", t)          # leading "(30) " view badge
    t = re.sub(r"\s*-\s*YouTube\s*$", "", t)    # trailing "- YouTube"
    # Some MPRIS sources (YT Music web app in particular) report the
    # window/media-session title as "Song Title | YouTube Music" — pipe
    # separator, not dash — which the rule above never caught. That left
    # "| YouTube Music" glued onto every search term (see the provider
    # debug log: 'Low | YouTube Music' instead of 'Low'), turning an
    # otherwise-clean title into a near-guaranteed miss.
    t = re.sub(r"\s*\|\s*YouTube\s*Music\s*$", "", t, flags=re.IGNORECASE)
    t = _TAG_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def split_artist_title(cleaned: str):
    if " - " in cleaned:
        artist, title = cleaned.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", cleaned.strip()


# YouTube's Media Session metadata sometimes localizes multi-artist credits
# (e.g. "Central Cee dan Lil Baby" instead of "Central Cee & Lil Baby"),
# which almost never matches lrclib's stored artist string. This generates
# a few plausible variants to try, most-specific first.
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|\bdan\b|\band\b|\bx\b|\bfeat\b\.?|\bft\b\.?)\s*",
    re.IGNORECASE,
)


def artist_variants(artist: str):
    variants = [artist]
    parts = [p for p in _ARTIST_SPLIT_RE.split(artist) if p]
    if len(parts) > 1:
        variants.append(" & ".join(parts))
        variants.append(", ".join(parts))
        variants.append(parts[0])  # primary artist only, last resort
    seen = set()
    for v in variants:
        v = v.strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            yield v


_USELESS_TITLES = {
    "youtube", "spotify", "music", "chrome", "chromium", "firefox",
    "brave", "google chrome", "media player", "unknown", "unknown title",
    "youtube music", "youtube premium",
}


def resolve_track_info(meta: dict):
    """Returns (artist, title) — handles both clean MPRIS sources
    (YT Music, Spotify) and raw YouTube tab titles.

    Filters out useless placeholder titles Chrome sometimes reports
    while a tab is still loading or after a player switch ("YouTube",
    "Chrome", …) so we don't wipe a good lyric view with garbage."""
    raw_title = meta.get("title") or ""
    raw_artist = meta.get("artist") or ""

    cleaned = clean_youtube_title(raw_title)
    if cleaned.lower().strip() in _USELESS_TITLES:
        # Prefer artist field if it carries the real title; else keep
        # cleaned so callers still get *something* rather than crashing.
        if raw_artist and raw_artist.lower().strip() not in _USELESS_TITLES:
            return raw_artist, cleaned
        return raw_artist, cleaned

    if raw_artist and raw_artist != raw_title:
        if raw_artist.lower().strip() in _USELESS_TITLES:
            artist, title = split_artist_title(cleaned)
            return artist, title
        return raw_artist, cleaned

    artist, title = split_artist_title(cleaned)
    return artist, title


# --------------------------------------------------------------- lrclib.net

def _cache_path(artist, title):
    key = hashlib.sha1(f"{artist}|{title}".lower().encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


# lrclib's /search endpoint can return multiple entries for the same
# song (different releases, snippets, mislabeled data). Blindly taking
# results[0] — or even just filtering by duration — risks grabbing an
# entry with LRC timing from a different recording entirely (Live take,
# Deluxe edit, TikTok Edit, ...) which reads as "the lyrics just don't
# sync". Score every candidate on title/artist/duration/synced-lyrics
# match and take the best one instead of the first plausible one.
MIN_PLAUSIBLE_DURATION = 30  # seconds — below this, treat as snippet/bad data

# A candidate below this score isn't "the best of the results" — it's
# "the least-bad of a batch of wrong songs" (e.g. an empty-artist /
# title-only fallback search that only ever returns unrelated tracks).
# Requires roughly: exact title match alone (50), or partial title +
# partial artist (20+10), or exact artist + close-ish duration (30+~15).
# Below this, showing nothing is better than showing the wrong song's
# lyrics in a different language entirely.
MIN_MATCH_SCORE = 40

_NORMALIZE_RE = re.compile(r"[^\w\s]")


def _normalize(s):
    """Lowercase + strip punctuation so 'Play Date' and 'Play Date (Live)'
    aren't treated as unrelated strings just because of formatting."""
    return _NORMALIZE_RE.sub("", (s or "").lower()).strip()


def _score_result(result, video_duration, artist, title):
    score = 0

    r_title = _normalize(result.get("trackName", ""))
    r_artist = _normalize(result.get("artistName", ""))
    n_title = _normalize(title)
    n_artist = _normalize(artist)

    if n_title and r_title == n_title:
        score += 50
    elif n_title and (n_title in r_title or r_title in n_title):
        score += 20  # partial match, e.g. base title vs "(Live)"/"(Remix)"

    if n_artist and r_artist == n_artist:
        score += 30
    elif n_artist and (n_artist in r_artist or r_artist in n_artist):
        score += 10

    dur = result.get("duration") or 0
    if dur and video_duration:
        score += max(0, 40 - abs(video_duration - dur))
    elif not dur:
        score -= 20  # no duration info at all — less trustworthy

    if result.get("syncedLyrics"):
        score += 20

    if dur and dur < MIN_PLAUSIBLE_DURATION:
        score -= 999  # snippet/bad data, never pick this over a real match

    return score


def _pick_best_result(results, video_duration=0, artist="", title=""):
    with_lyrics = [r for r in results if r.get("syncedLyrics")]
    if not with_lyrics:
        return None
    scored = sorted(
        with_lyrics,
        key=lambda r: _score_result(r, video_duration, artist, title),
        reverse=True,
    )
    best = scored[0]
    if _score_result(best, video_duration, artist, title) < MIN_MATCH_SCORE:
        # Top candidate still isn't a confident match (typically an
        # empty-artist / title-only fallback search with no good
        # title hit) — reject rather than show a wrong-song mismatch.
        return None
    return best


def fetch_lrc(artist, title, album, duration):
    """Returns (synced_lyrics_text, lrc_duration, plain_lyrics_text).
    lrc_duration comes from lrclib's own record for the matched track —
    used in Shazam mode where the MPRIS `duration` is the whole mashup
    video's length, not the individual song's. plain_lyrics_text is
    lrclib's unsynced lyrics field, kept as a fallback for tracks that
    have lyrics submitted but no LRC timing (common for remixes/edits
    where nobody's bothered to time-sync that specific version)."""
    cache_file = _cache_path(artist, title)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            # Caches written before "plain" existed as a field won't
            # have the key at all — that's different from having
            # checked and found no plain lyrics (which would still be
            # "plain": ""). Treat a missing key as stale/incomplete and
            # re-fetch instead of silently reporting a false miss for
            # every track cached before this feature existed.
            if "plain" in cached:
                return (cached.get("synced", ""), cached.get("duration", 0),
                         cached.get("plain", ""))
        except Exception:
            pass

    synced = ""
    lrc_duration = 0
    plain = ""

    for a in artist_variants(artist) if artist else [""]:
        # 1. exact /get lookup (most accurate — matches duration too)
        try:
            r = requests.get(
                f"{LRCLIB_API}/get",
                params={
                    "track_name": title,
                    "artist_name": a,
                    "album_name": album,
                    "duration": duration,
                },
                timeout=5,
            )
            if r.ok:
                data = r.json()
                synced = data.get("syncedLyrics") or ""
                lrc_duration = data.get("duration") or 0
                plain = plain or (data.get("plainLyrics") or "")
        except requests.RequestException:
            pass

        if synced:
            break

        # 2. looser /search with this artist variant
        try:
            r = requests.get(
                f"{LRCLIB_API}/search",
                params={"track_name": title, "artist_name": a},
                timeout=5,
            )
            if r.ok:
                results = r.json()
                best = _pick_best_result(results, duration, a, title) if results else None
                if best:
                    synced = best.get("syncedLyrics") or ""
                    lrc_duration = best.get("duration") or 0
                    plain = plain or (best.get("plainLyrics") or "")
                elif results and not plain:
                    # No entry had synced lyrics (why we're in the plain
                    # fallback path at all) — still grab plain lyrics
                    # from whichever result has them, so a track with
                    # lyrics-but-no-LRC-timing isn't treated the same
                    # as a track with no lyrics data at all.
                    for r2 in results:
                        if r2.get("plainLyrics"):
                            plain = r2["plainLyrics"]
                            break
        except requests.RequestException:
            pass

        if synced:
            break

    if not synced:
        # 3. last resort — search by title only (no artist filter at all)
        try:
            r = requests.get(
                f"{LRCLIB_API}/search",
                params={"track_name": title},
                timeout=5,
            )
            if r.ok:
                results = r.json()
                # no artist filter at this point in the fallback chain —
                # score on title/duration only
                best = _pick_best_result(results, duration, "", title) if results else None
                if best:
                    synced = best.get("syncedLyrics") or ""
                    lrc_duration = best.get("duration") or 0
                    plain = plain or (best.get("plainLyrics") or "")
                elif results and not plain:
                    for r2 in results:
                        if r2.get("plainLyrics"):
                            plain = r2["plainLyrics"]
                            break
        except requests.RequestException:
            pass

    cache_file.write_text(json.dumps({
        "synced": synced, "duration": lrc_duration, "plain": plain,
    }))
    return synced, lrc_duration, plain


_LRC_LINE_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text: str):
    """Returns a sorted list of (seconds, line_text)."""
    entries = []
    for line in text.splitlines():
        tags = _LRC_LINE_RE.findall(line)
        if not tags:
            continue
        content = _LRC_LINE_RE.sub("", line).strip()
        for mm, ss in tags:
            seconds = int(mm) * 60 + float(ss)
            entries.append((seconds, content))
    entries.sort(key=lambda e: e[0])
    return entries


# Enhanced / word-level LRC parsing.
# Format example per line:
#   [00:12.34]<00:12.34>First <00:12.67>line <00:13.12>of <00:13.45>lyrics
# Line timestamp still present; each word is prefixed by its own <mm:ss.xx>
# tag. When available this is far more accurate than syllable estimation —
# the wipe can snap to real word onsets instead of guessing.
_WORD_TS_RE = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>([^<]*)")


class WordTS(tuple):
    """(start_seconds, text) that also remembers .end (None when the source had no end time).
    Still a plain 2-tuple, so `for wstart, wtext in word_timings` keeps working."""

    syl = None  # ((t0, t1, char_from, char_to), ...) when the word has syllable timing

    def __new__(cls, start, text, end=None):
        obj = super().__new__(cls, (start, text))
        obj.end = end
        return obj

    def __getnewargs__(self):
        return (self[0], self[1], self.end)


def parse_enhanced_lrc(text: str):
    """Parse Enhanced LRC into (line_entries, word_timings).

    line_entries: same shape as parse_lrc → [(seconds, line_text), ...]
    word_timings: parallel list; word_timings[i] is either None (no word
    data for that line) or a list of (word_start_seconds, word_text) for
    the words in that line. Empty / malformed enhanced lines fall back
    to None so callers can keep using the syllable path for those rows.
    """
    line_entries = []
    word_timings = []
    for raw in text.splitlines():
        line_tags = _LRC_LINE_RE.findall(raw)
        if not line_tags:
            continue
        # strip the outer [mm:ss] tag(s) first
        content = _LRC_LINE_RE.sub("", raw).strip()
        words = []
        # keep untagged text that sits BEFORE the first <mm:ss> tag
        # (otherwise the first word is silently dropped)
        _m0 = _WORD_TS_RE.search(content)
        _lead = content[:_m0.start()].strip() if _m0 else ""
        # raw_words: [start, text, end, syllables]; a syllable is [t0, t1, char_from, char_to].
        # A tag whose text starts with U+200D (written by lyrics_providers.py) continues the
        # previous word as another syllable; every other tag starts a new word, as before.
        raw_words = []
        if _lead:
            _t = int(line_tags[0][0]) * 60 + float(line_tags[0][1])
            raw_words.append([_t, _lead, None, [[_t, None, 0, len(_lead)]]])
        for mm, ss, wtext in _WORD_TS_RE.findall(content):
            _cont = wtext.startswith("\u200d")
            w = wtext.replace("\u200d", "").strip()
            ts = int(mm) * 60 + float(ss)
            if not w:
                # empty tag = end time of the previous syllable
                if raw_words and raw_words[-1][3][-1][1] is None and ts > raw_words[-1][3][-1][0]:
                    raw_words[-1][3][-1][1] = ts
                continue
            if _cont and raw_words:
                _rw = raw_words[-1]
                _c0 = len(_rw[1])
                _rw[1] += w
                _rw[3].append([ts, None, _c0, len(_rw[1])])
            else:
                raw_words.append([ts, w, None, [[ts, None, 0, len(w)]]])
        for _st, _tx, _en, _sy in raw_words:
            for _i in range(len(_sy) - 1):
                if _sy[_i][1] is None:
                    _sy[_i][1] = _sy[_i + 1][0]
            _wd = WordTS(_st, _tx, _sy[-1][1])
            if len(_sy) > 1:
                _wd.syl = tuple(tuple(_s) for _s in _sy)
            words.append(_wd)
        # rebuild clean line text (join words, or fall back to stripping
        # any leftover <...> tags if the regex missed something)
        if words:
            line_text = " ".join(w for _, w in words)
        else:
            line_text = _WORD_TS_RE.sub("", content).strip()
        for mm, ss in line_tags:
            seconds = int(mm) * 60 + float(ss)
            line_entries.append((seconds, line_text))
            word_timings.append(words if words else None)
    # keep them sorted together
    paired = sorted(zip(line_entries, word_timings), key=lambda p: p[0][0])
    if not paired:
        return [], []
    line_entries, word_timings = zip(*paired)
    return list(line_entries), list(word_timings)


def load_lyrics_with_words(artist, title, album, duration):
    """Unified loader: try Enhanced (word-level) first, fall back to
    normal lrclib line-synced + plain.

    Returns (entries, plain_lyrics, lrc_duration, word_timings).
    word_timings is a parallel list to entries (or None when unavailable).
    """
    enhanced = ""
    if HAVE_PROVIDERS:  # multi-provider word-level lyrics first
        res = lyrics_providers.fetch(title, artist, album, duration)
        if res and res.word_level:
            enhanced = res.lrc
    if enhanced:
        entries, word_timings = parse_enhanced_lrc(enhanced)
        if entries and any(wt for wt in word_timings):
            # still grab duration/plain from lrclib so Shazam mode &
            # plain-fallback keep working the same way
            _, lrc_duration, plain = fetch_lrc(artist, title, album, duration)
            return entries, plain, lrc_duration, word_timings

    synced, lrc_duration, plain = fetch_lrc(artist, title, album, duration)
    entries = parse_lrc(synced) if synced else []
    return entries, plain, lrc_duration, None


# ------------------------------------------------------------- genre
# Best-effort genre lookup via Deezer public API (no key needed).
# Returns up to two genre names joined with " · ", e.g.
# "indie pop · bedroom pop" or "Alternative". Empty string on miss.
# Small override map for tracks where Deezer is too generic.
GENRE_OVERRIDES = {
    "yung kai|blue": "indie pop · bedroom pop",
    "yung kai|wildflower": "indie pop · bedroom pop",
}
GENRE_CACHE = {}


def fetch_genre(artist: str, title: str) -> str:
    """Return a short genre string for the track, cached per (artist, title)."""
    key = f"{(artist or '').lower()}|{(title or '').lower()}"
    if key in GENRE_CACHE:
        return GENRE_CACHE[key]
    if key in GENRE_OVERRIDES:
        GENRE_CACHE[key] = GENRE_OVERRIDES[key]
        return GENRE_OVERRIDES[key]
    genre = ""
    try:
        q = f"{artist} {title}".strip()
        if not q:
            GENRE_CACHE[key] = ""
            return ""
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": q, "limit": 1},
            timeout=3,
        )
        if r.ok:
            data = (r.json().get("data") or [])
            if data:
                album_id = (data[0].get("album") or {}).get("id")
                if album_id:
                    r2 = requests.get(
                        f"https://api.deezer.com/album/{album_id}",
                        timeout=3,
                    )
                    if r2.ok:
                        genres = (r2.json().get("genres") or {}).get("data") or []
                        names = [g.get("name") for g in genres if g.get("name")]
                        if names:
                            # keep at most two, joined the way user asked
                            genre = " · ".join(names[:2])
    except Exception:
        pass
    GENRE_CACHE[key] = genre
    return genre


def plain_lyrics_window(plain_text, pos, duration, max_lines):
    """Slices unsynced lyrics down to `max_lines` for display, since a
    full lyrics block often doesn't fit the panel. There's no per-line
    timing to follow here (that's the whole reason we're in this
    fallback), so this just approximates a scroll position from how
    far into the track we are — `pos / duration` mapped onto the line
    list — good enough to slowly advance through a long lyric block
    instead of freezing on line 1 for the whole song. If everything
    already fits, no scrolling is needed at all."""
    lines = [l for l in (plain_text or "").splitlines() if l.strip()]
    if not lines:
        return []
    n = len(lines)
    if n <= max_lines:
        return lines
    frac = 0.0
    if duration and duration > 0:
        frac = max(0.0, min(1.0, pos / duration))
    center = int(frac * (n - 1))
    half = max_lines // 2
    start = max(0, min(n - max_lines, center - half))
    return lines[start:start + max_lines]


def current_index(entries, pos):
    idx = -1
    for i, (t, _) in enumerate(entries):
        if t <= pos:
            idx = i
        else:
            break
    return idx


# ------------------------------------------------------------- text zoom

IN_KITTY = bool(os.environ.get("KITTY_WINDOW_ID"))


def nudge_kitty_font_size(delta):
    """Bumps Kitty's font size up/down via remote control.
    Returns True on success, False if unavailable (not Kitty, remote
    control disabled, etc.)."""
    if not IN_KITTY or shutil.which("kitty") is None:
        return False
    sign = "+" if delta > 0 else "-"
    try:
        result = subprocess.run(
            ["kitty", "@", "set-font-size", "--", f"{sign}{abs(delta)}"],
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except Exception:
        return False


# Net zoom (in FONT_STEP units) is remembered across runs: the +/- keys count
# steps, and on start they are re-applied on top of kitty's default font size.
ZOOM_FILE = CACHE_DIR / "zoom.json"
ZOOM_STEPS_LIMIT = 30


def load_zoom_steps():
    try:
        n = int(json.loads(ZOOM_FILE.read_text()).get("steps", 0))
    except Exception:
        return 0
    return max(-ZOOM_STEPS_LIMIT, min(ZOOM_STEPS_LIMIT, n))


def save_zoom_steps(n):
    try:
        ZOOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        ZOOM_FILE.write_text(json.dumps({"steps": int(n)}))
    except Exception:
        pass


# Cell size (px) of the UN-zoomed font. A kitty config reload (e.g. a wallpaper
# / theme switch) resets the font to default behind our back; we notice it
# because the cell size drops back to this value while a zoom is saved.
_ZOOM_DEFAULT_CELL = None
_ZOOM_LAST_HEAL = 0.0


def zoom_by(delta_steps, current):
    """Nudges kitty's font by `delta_steps` and stores the new net zoom.
    Returns the new step count (unchanged if kitty refused)."""
    global _ZOOM_DEFAULT_CELL
    if current == 0 and IN_KITTY:
        _ZOOM_DEFAULT_CELL = _term_cell_px()  # un-zoomed right now
    if not nudge_kitty_font_size(delta_steps * FONT_STEP):
        return current
    new = max(-ZOOM_STEPS_LIMIT, min(ZOOM_STEPS_LIMIT, current + delta_steps))
    save_zoom_steps(new)
    return new


def restore_kitty_zoom(steps):
    """Resets kitty's font to its default, then re-applies the saved zoom.
    Returns the step count actually in effect."""
    global _ZOOM_DEFAULT_CELL
    if not IN_KITTY or shutil.which("kitty") is None:
        return 0
    if not steps:
        _ZOOM_DEFAULT_CELL = _term_cell_px()
        return 0
    try:
        ok = subprocess.run(["kitty", "@", "set-font-size", "0"],
                            capture_output=True, timeout=2).returncode == 0
    except Exception:
        ok = False
    if not ok:
        _ZOOM_DEFAULT_CELL = _term_cell_px()
        return 0
    time.sleep(0.25)  # let kitty resize the pty before measuring
    _ZOOM_DEFAULT_CELL = _term_cell_px()
    if nudge_kitty_font_size(steps * FONT_STEP):
        return steps
    return 0


def heal_kitty_zoom(steps):
    """Call every loop tick. If a zoom is saved but the font is back at its
    default size (kitty config reload), re-apply the zoom."""
    global _ZOOM_LAST_HEAL
    if not steps or not IN_KITTY or _ZOOM_DEFAULT_CELL is None:
        return
    if _term_cell_px() != _ZOOM_DEFAULT_CELL:
        return
    now = time.time()
    if now - _ZOOM_LAST_HEAL < 2.0:  # give kitty time to resize; never spam
        return
    _ZOOM_LAST_HEAL = now
    nudge_kitty_font_size(steps * FONT_STEP)


_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def _enrich_from_plasma(meta: dict):
    """Brave's own MPRIS interface for a YT Music tab reports title/
    length just fine but leaves xesam:artist, mpris:artUrl and
    xesam:url ALL blank — confirmed via `playerctl -p brave.instance...
    metadata`, none of the three are in its output. KDE's
    plasma-browser-integration bridge for that *same* tab carries all
    three. Fill in whatever's missing from plasma's copy — but only
    once plasma's title looks like the same track, and never touch the
    title itself (placeholder-title detection elsewhere relies on
    seeing Brave's raw title unmodified, and title is the one field
    Brave DOES provide reliably)."""
    if not meta:
        return meta
    if (meta.get("artist") or "").strip() and \
       ((meta.get("art_url") or "").strip() or (meta.get("track_url") or "").strip()):
        return meta  # already complete — skip the extra playerctl call

    cleaned_title = clean_youtube_title(meta.get("title") or "").lower().strip()
    if not cleaned_title or cleaned_title in _USELESS_TITLES:
        return meta  # placeholder tick — nothing trustworthy to match against

    plasma = get_metadata("plasma-browser-integration")
    if not plasma:
        return meta
    plasma_title = clean_youtube_title(plasma.get("title") or "").lower().strip()
    if not plasma_title:
        return meta
    if (plasma_title != cleaned_title
            and plasma_title not in cleaned_title
            and cleaned_title not in plasma_title):
        return meta  # different tab — never donate its data to this one

    merged = dict(meta)
    for field in ("artist", "art_url", "track_url", "album"):
        if not (merged.get(field) or "").strip() and (plasma.get(field) or "").strip():
            merged[field] = plasma[field]
    return merged


def youtube_thumbnail_url(track_url: str) -> str:
    """Derives a public thumbnail URL from a YouTube watch/shorts/youtu.be
    URL. Returns '' if track_url isn't a recognizable YouTube link.
    Used as a fallback when mpris:artUrl is empty — Chrome's MediaSession
    integration doesn't always expose artwork for a plain YouTube tab,
    even though the video itself has a perfectly good public thumbnail."""
    if not track_url:
        return ""
    m = _YT_ID_RE.search(track_url)
    if not m:
        return ""
    return f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg"


# A YouTube video id in track_url beats the browser's artUrl: MediaSession /
# Plasma Browser Integration art can lag behind the title (a new artwork file
# holding the previous song's cover), while the id-derived thumbnail always
# matches the video that is playing. Set False to prefer artUrl again.
PREFER_YT_THUMBNAIL = True


def _pick_cover_url(meta) -> str:
    """Cover source URL for a metadata dict (see PREFER_YT_THUMBNAIL)."""
    art = meta.get("art_url", "") or ""
    thumb = youtube_thumbnail_url(meta.get("track_url", ""))
    if PREFER_YT_THUMBNAIL:
        return thumb or art
    return art or thumb


def _cover_cache_path(art_url: str) -> Path:
    key = hashlib.sha1(art_url.encode()).hexdigest()
    return COVER_CACHE_DIR / f"{key}.jpg"


def _square_crop(path: Path):
    """Center-crops the image at `path` to a 1:1 square, in place, so
    covers always fill the cell box edge-to-edge regardless of the
    source's aspect ratio. Without this, 16:9 YouTube thumbnails get
    letterboxed (shrunk + padded) by icat's --place fit-to-box scaling,
    while SongRec's near-square album art fills it fully — same box,
    visibly different sizes. No-op if Pillow isn't installed or the
    image can't be read; callers just get the uncropped original then."""
    if not HAVE_PIL:
        return
    try:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img.crop((left, top, left + side, top + side)).save(path, "JPEG", quality=90)
    except Exception:
        pass


def extract_cover_accent(path: Path):
    """Pick a karaoke accent that matches the cover's overall mood.

    Uses hue-histogram voting (not max-saturation pixel) so a small
    bright accent object (yellow bubble, red logo, etc.) can't steal
    the color from a large calm background. Returns (hex, (r,g,b))
    or (None, None)."""
    global COVER_ACCENT_HEX, COVER_ACCENT_RGB
    COVER_ACCENT_HEX = None
    COVER_ACCENT_RGB = None
    if not HAVE_PIL or path is None:
        return None, None
    try:
        img = Image.open(path).convert("RGB")
        # slightly larger sample for stabler histogram
        img = img.resize((64, 64), Image.Resampling.BILINEAR
                         if hasattr(Image, "Resampling") else Image.BILINEAR)
        pixels = list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata())

        # Bucket by hue (36 buckets = 10° each). Weight = area * sat * mid-value.
        # Skip near-gray / near-black / near-white so they don't dilute the vote.
        NB = 36
        weights = [0.0] * NB
        rgb_sums = [(0.0, 0.0, 0.0)] * NB
        counts = [0] * NB

        for r, g, b in pixels:
            rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
            h, s, v = colorsys.rgb_to_hsv(rf, gf, bf)
            if s < 0.12 or v < 0.15 or v > 0.95:
                continue
            # mid-tones get a bonus — backgrounds are usually there
            mid = 1.0 - abs(v - 0.45) * 1.4
            mid = max(0.15, mid)
            w = (s ** 0.85) * mid
            bucket = int(h * NB) % NB
            weights[bucket] += w
            rr, gg, bb = rgb_sums[bucket]
            rgb_sums[bucket] = (rr + r * w, gg + g * w, bb + b * w)
            counts[bucket] += 1

        if max(weights) <= 0:
            # pure grayscale / failed — soft average fallback
            mids = [(r, g, b) for r, g, b in pixels
                    if 40 < (r + g + b) / 3 < 210]
            if not mids:
                return None, None
            n = len(mids)
            best = (sum(p[0] for p in mids) // n,
                    sum(p[1] for p in mids) // n,
                    sum(p[2] for p in mids) // n)
        else:
            # also merge neighboring buckets so a green spread across
            # two 10° bins still wins as one region
            smooth = [0.0] * NB
            for i in range(NB):
                smooth[i] = (
                    weights[(i - 1) % NB] * 0.25
                    + weights[i]
                    + weights[(i + 1) % NB] * 0.25
                )
            winner = max(range(NB), key=lambda i: smooth[i])
            wsum = weights[winner]
            if wsum <= 0:
                return None, None
            rr, gg, bb = rgb_sums[winner]
            best = (int(rr / wsum), int(gg / wsum), int(bb / wsum))

        # lift into a readable karaoke range while keeping the hue
        rf, gf, bf = (c / 255.0 for c in best)
        h, s, v = colorsys.rgb_to_hsv(rf, gf, bf)
        s = max(0.38, min(0.70, s * 1.10 + 0.08))
        v = max(0.70, min(0.90, v * 1.05 + 0.10))
        r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
        rgb = (round(r2 * 255), round(g2 * 255), round(b2 * 255))
        hx = "#{:02x}{:02x}{:02x}".format(*rgb)
        COVER_ACCENT_HEX = hx
        COVER_ACCENT_RGB = rgb
        return hx, rgb
    except Exception:
        return None, None


def fetch_cover(art_url: str):
    """Downloads (or copies, for file:// sources) the given art URL to a
    local cache file and returns its path. Returns None if there's no
    URL, or the fetch fails for any reason — callers should just fall
    back to the no-cover layout in that case, never raise.

    NOTE on file:// sources: some MPRIS senders (e.g. KDE Plasma Browser
    Integration) reuse a single temp file path for every track's artwork,
    overwriting its *contents* each time the track changes while the
    *path* stays identical. A URL-keyed cache can't tell that apart from
    "already fetched this exact art" and would keep serving stale bytes
    copied for a previous track. So for file:// we skip the cache check
    entirely and always re-copy fresh — it's just a local file copy,
    cheap enough to redo every time, and the only way to guarantee the
    cover actually matches what's playing right now.
    """
    if not art_url:
        return None
    cache_file = _cover_cache_path(art_url)
    try:
        if art_url.startswith("file://"):
            src = Path(art_url[len("file://"):])
            if not src.exists():
                return None
            shutil.copy(src, cache_file)
            _square_crop(cache_file)
            return cache_file
        if cache_file.exists():
            return cache_file
        if art_url.startswith("http://") or art_url.startswith("https://"):
            r = requests.get(art_url, timeout=5)
            if r.ok and r.content:
                cache_file.write_bytes(r.content)
                _square_crop(cache_file)
                return cache_file
    except Exception:
        pass
    return None


def _cover_rows():
    """Rows the square cover needs so its box is square in pixels.

    COVER_ROWS assumes 1:2 cells; real fonts are often 10x22 or 9x20, which
    leaves a gap under the image. Uses the live pixel size of the tty and
    falls back to COVER_ROWS when the terminal does not report it."""
    try:
        import fcntl
        import struct
        import termios

        rows, cols, xpx, ypx = struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        )
        if rows and cols and xpx and ypx:
            cell_w, cell_h = xpx / cols, ypx / rows
            return max(8, min(20, math.ceil(COVER_COLS * cell_w / cell_h - 0.15)))
    except Exception:
        pass
    return COVER_ROWS


def _cover_place_top(term_height=None):
    """Top cell for the cover image.

    - dock layout → near the bottom (lines up with the dock strip)
    - classic     → upper-third of the left column so art + caption
                    form one visual block instead of a tiny stamp
                    floating in empty space
    """
    rows = _cover_rows()
    if LAYOUT_MODE == 4 and term_height and term_height > rows + 4:
        return max(COVER_MARGIN_TOP, term_height - rows - 1)
    if term_height and term_height > rows + COVER_CAPTION_ROWS + 4:
        stack = rows + COVER_CAPTION_ROWS
        free = max(0, term_height - stack - 2)
        # bias slightly toward the top (1/3) — dead-center looks floaty
        return max(1, free // 3)
    return COVER_MARGIN_TOP


def show_cover(image_path, term_height=None):
    """Draws `image_path` into Kitty's graphics layer on the left.
    Position depends on LAYOUT_MODE (dock → bottom, otherwise top).
    No-op outside Kitty or if there's no image.
    NOTE: --place uses WIDTHxHEIGHT@LEFTxTOP — the separator between
    left and top is a literal 'x', not a comma.
    NOTE: --scale-up matters — icat refuses to upscale past native
    resolution by default, so small thumbnails would otherwise render
    undersized inside the place box."""
    if not COVER_ENABLED or image_path is None:
        return
    if term_height is None:
        try:
            term_height = shutil.get_terminal_size(fallback=(80, 24)).lines
        except Exception:
            term_height = 24
    top = _cover_place_top(term_height)
    try:
        subprocess.run(
            [
                "kitty", "+kitten", "icat",
                "--stdin=no",
                "--transfer-mode=file",
                "--scale-up",
                f"--place={COVER_COLS}x{_cover_rows()}@{COVER_MARGIN_LEFT}x{top}",
                str(image_path),
            ],
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        pass


def clear_cover():
    """Removes any previously-placed cover image from the graphics
    layer — used when a track has no art, so a stale image from the
    previous track doesn't linger on screen."""
    if not COVER_ENABLED:
        return
    try:
        subprocess.run(
            ["kitty", "+kitten", "icat", "--clear"],
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except Exception:
        pass


def run_songrec(device=SHAZAM_DEVICE, timeout=SHAZAM_LISTEN_TIMEOUT):
    """Listens to `device` via SongRec and identifies the currently
    playing song. Returns a dict with
    artist/title/offset on success, or None on failure/timeout/missing
    binary. `offset` is where in the song SongRec's fingerprint matched
    (seconds) — used as the starting point for wall-clock-driven lyric
    sync in Shazam mode, since we have no MPRIS position for a song
    buried inside a mashup video."""
    if shutil.which("songrec") is None:
        return None

    listen_start = time.time()

    try:
        proc = subprocess.Popen(
            ["songrec", "listen", "--audio-device", device, "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None

    result_line = None
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
            except Exception:
                break
            if ready:
                line = proc.stdout.readline()
                if line and line.strip():
                    result_line = line.strip()
                    break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    if not result_line:
        return None

    # Time spent recording + waiting for SongRec's match — the returned
    # `offset` reflects where in the song the match was found, but that
    # much real-world time has already passed since then. Without adding
    # it back, shazam_pos always starts a fixed amount behind the actual
    # audio (the exact "consistently lagging by ~N seconds" symptom).
    listen_elapsed = time.time() - listen_start

    try:
        data = json.loads(result_line)
        track = data.get("track") or {}
        title = track.get("title") or ""
        artist = track.get("subtitle") or ""
        if not title:
            return None
        matches = data.get("matches") or [{}]
        offset = matches[0].get("offset") if matches else 0
        try:
            offset = float(offset)
        except (TypeError, ValueError):
            offset = 0.0
        images = track.get("images") or {}
        cover_url = images.get("coverart") or images.get("coverarthq") or ""
        return {
            "artist": artist,
            "title": title,
            "offset": offset + listen_elapsed + SHAZAM_OFFSET_FUDGE,
            "cover_url": cover_url,
        }
    except Exception:
        return None


def _track_hash(artist, title):
    return hashlib.sha1(f"{artist}|{title}".lower().encode()).hexdigest()


def load_offset_cache():
    """Loads the whole per-track sync-offset map from disk (small file,
    fine to keep entirely in memory for the session). Returns {} if the
    file doesn't exist yet or is unreadable/corrupt."""
    if OFFSET_CACHE_FILE.exists():
        try:
            return json.loads(OFFSET_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_offset_cache(cache):
    try:
        OFFSET_CACHE_FILE.write_text(json.dumps(cache))
    except Exception:
        pass


def offset_for_track(cache, artist, title):
    """Looks up this track's saved sync offset, defaulting to 0.0 for a
    track that's never been manually corrected before."""
    return cache.get(_track_hash(artist, title), 0.0)


def save_offset_for_track(cache, artist, title, offset):
    """Updates one track's entry in the in-memory cache dict and
    persists the whole map. `[`/`]`/`r` presses are rare enough that
    writing the full (tiny) file on every change is simpler than
    debouncing it."""
    cache[_track_hash(artist, title)] = offset
    save_offset_cache(cache)


# -------------------------------------------------------------- translation

def _translate_cache_path(artist, title):
    key = hashlib.sha1(f"{artist}|{title}|{TRANSLATE_TARGET_LANG}".lower().encode()).hexdigest()
    return TRANSLATE_CACHE_DIR / f"{key}.json"


def fetch_translation(artist, title, entries):
    """Bulk-translates every lyric line in one request and caches the
    result to disk, keyed by artist/title/target-language. Returns a
    list of translated strings parallel to `entries`, or None if
    there's nothing to translate, the request fails, or the response
    can't be split back into the same number of lines as the original
    (which would mean showing translations against the wrong lines —
    safer to show nothing than something misaligned)."""
    if not entries:
        return None

    cache_file = _translate_cache_path(artist, title)
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if isinstance(data, list) and len(data) == len(entries):
                return data
        except Exception:
            pass

    joined = TRANSLATE_LINE_SEP.join(line for _, line in entries)
    try:
        resp = requests.get(
            TRANSLATE_ENDPOINT,
            params={"client": "gtx", "sl": "auto", "tl": TRANSLATE_TARGET_LANG, "dt": "t", "q": joined},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        translated = "".join(chunk[0] for chunk in data[0] if chunk and chunk[0])
    except Exception:
        return None

    lines = [l.strip() for l in translated.split(TRANSLATE_LINE_SEP.strip())]
    if len(lines) != len(entries):
        return None

    try:
        cache_file.write_text(json.dumps(lines, ensure_ascii=False))
    except Exception:
        pass
    return lines


_KAKASI = None


def _get_kakasi():
    """Lazy-loads pykakasi's converter once per process — same pattern
    as _get_cmu_dict(). Returns None if the package isn't installed;
    callers treat that as "romanization unavailable" rather than
    erroring out."""
    global _KAKASI
    if _KAKASI is None and HAVE_PYKAKASI:
        try:
            _KAKASI = pykakasi.kakasi()
        except Exception:
            _KAKASI = False  # tried and failed — don't retry every call
    return _KAKASI or None


def romanize_lyrics(entries):
    """Converts every lyric line's kanji/hiragana/katakana into Hepburn
    romaji via pykakasi — purely local, no network round-trip, so
    (unlike fetch_translation) this doesn't need a disk cache; it's
    cheap enough to recompute per-track on demand. Returns a list
    parallel to `entries`, or None if pykakasi isn't installed or the
    conversion fails outright. Lines that are already plain ASCII
    (English sections mixed into JP lyrics, blank lines, etc.) just
    romanize to themselves, which is harmless."""
    if not entries:
        return None
    kks = _get_kakasi()
    if kks is None:
        return None
    try:
        out = []
        for _, line in entries:
            if not line:
                out.append("")
                continue
            parts = kks.convert(line)
            out.append(" ".join(p["hepburn"] for p in parts).strip())
        return out
    except Exception:
        return None


# ---------------------------------------------------------- identify history

def log_shazam_history(artist, title):
    """Appends one line to the identify-history log. Best-effort — a
    failure to write here shouldn't interrupt playback."""
    entry = {"ts": time.time(), "artist": artist, "title": title}
    try:
        with open(SHAZAM_HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_shazam_history(limit=SHAZAM_HISTORY_MAX_SHOWN):
    """Returns the most recent `limit` identify-history entries, newest
    first. Corrupt/unreadable lines are skipped rather than failing the
    whole read."""
    if not SHAZAM_HISTORY_FILE.exists():
        return []
    try:
        lines = SHAZAM_HISTORY_FILE.read_text().splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return list(reversed(out))


def show_shazam_history(live):
    """Pauses Live and shows a read-only panel of recently identified
    songs — same visual language as the 'Listening...' panel. Any
    keypress (or a short timeout) returns to the lyrics view."""
    live.stop()
    console = Console()
    console.clear()

    history = read_shazam_history()
    if not history:
        rows = [Text("No identifications yet — press 's' to try one.", style="dim italic", justify="center")]
    else:
        rows = []
        for entry in history:
            ts = time.strftime("%H:%M", time.localtime(entry.get("ts", 0)))
            hist_title = entry.get("title") or "?"
            hist_artist = entry.get("artist") or "?"
            rows.append(Text(f"{ts}   {hist_title} — {hist_artist}", justify="left"))

    panel = Panel(
        Group(
            Text("recent identifications", style=f"bold {ACCENT}", justify="center"),
            Text(""),
            *rows,
            Text(""),
            Text("any key to return", style="dim italic", justify="center"),
        ),
        border_style=ACCENT,
        padding=(1, 4),
        width=56,
        title=" history ",
        title_align="left",
    )
    console.print(Align.center(panel))
    poll_key(15)
    live.start()


def show_help(live, songrec_available=False):
    """Pauses Live and shows every key binding in a centered panel, same
    visual language as the history panel. Any key (or a long timeout)
    returns to the lyrics view."""
    live.stop()
    console = Console()
    console.clear()

    keys = [
        ("space", "play / pause"),
        ("+ / -", "zoom (kitty allow_remote_control)"),
        ("k", "karaoke wipe on / off"),
        ("d", "karaoke debug overlay"),
        ("[ / ]", "shift sync earlier / later"),
        ("r", "reset sync offset"),
        ("t", "translation on / off"),
    ]
    if HAVE_PYKAKASI:
        keys.append(("y", "romanization on / off"))
    if songrec_available:
        keys += [
            ("s", "identify the playing track"),
            ("h", "identification history"),
            ("b", "leave identify mode"),
        ]
    keys += [
        ("z", "minimal mode"),
        ("m", "search lyrics"),
        ("f", "alternate LRC"),
        ("p", "next player"),
        ("o", "settings"),
        ("? or /", "this help"),
        ("q", "quit"),
    ]
    rows = [Text.assemble((f"{k:<7}", f"bold {ACCENT}"), d) for k, d in keys]

    panel = Panel(
        Group(
            *rows,
            Text(""),
            Text("any key to close", style="dim italic", justify="center"),
        ),
        border_style=ACCENT,
        padding=(1, 4),
        width=58,
        title=" keys ",
        title_align="left",
    )
    console.print(Align.center(panel, vertical="middle", height=max(console.height - 1, 1)))
    poll_key(60)
    live.start()


# ------------------------------------------------------------ notifications

def notify_shazam_match(artist, title, cover_path=None):
    """Fires a desktop notification when Shazam mode successfully
    identifies a song. Silently
    does nothing if notify-send isn't installed (e.g. non-Linux-desktop
    environments) so this never blocks playback."""
    if shutil.which("notify-send") is None:
        return
    args = ["notify-send", "-a", "Sweep Lyrics", f"🎧 {title or 'Unknown title'}", artist or ""]
    if cover_path:
        args += ["-i", str(cover_path)]
    try:
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass


class RawKeys:
    """Puts stdin into cbreak mode so single keypresses (+/-) can be read
    instantly, without waiting for Enter, while the lyrics loop keeps
    refreshing. Ctrl+C still works normally (cbreak keeps ISIG enabled)."""

    def __enter__(self):
        self.enabled = sys.stdin.isatty()
        if self.enabled:
            self.fd = sys.stdin.fileno()
            self.old_settings = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.enabled:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)


def poll_key(timeout):
    """Non-blocking check for a keypress within `timeout` seconds.

    Returns a single character, or one of the special tokens:
      'up' | 'down' | 'left' | 'right' | 'enter' | 'esc'
    so the settings panel (and anything else) can handle arrows without
    each caller re-implementing CSI parsing. Plain printable keys are
    still returned as their 1-char string.
    """
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except Exception:
        time.sleep(timeout)
        return None
    if not ready:
        return None
    try:
        fd = sys.stdin.fileno()
        ch = os.read(fd, 1).decode(errors="ignore")
    except Exception:
        return None
    if not ch:
        return None

    if ch in ("\r", "\n"):
        return "enter"
    if ch != "\x1b":
        return ch

    # ESC — either a bare Escape, or the start of a CSI sequence (arrows).
    # Wait a short beat for the rest of the sequence; if nothing comes,
    # treat it as a plain Esc press.
    try:
        more, _, _ = select.select([sys.stdin], [], [], 0.05)
    except Exception:
        more = []
    if not more:
        return "esc"
    try:
        seq = os.read(sys.stdin.fileno(), 8).decode(errors="ignore")
    except Exception:
        return "esc"
    # CSI: '[' + A/B/C/D  or  'O' + A/B/C/D (application mode)
    if seq.startswith("[") and len(seq) >= 2:
        return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(seq[1], "esc")
    if seq.startswith("O") and len(seq) >= 2:
        return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(seq[1], "esc")
    return "esc"


# --------------------------------------------------------------- settings
SETTINGS_FILE = CACHE_DIR / "settings.json"

# Hierarchical settings: categories → items
# item tuple: (key, label, type, default, min, max, step)
# type: "float" | "bool" | "int"
# layout_mode is int 0..4 → classic / bait / timeline / hud / dock
_LAYOUT_NAMES = ("classic", "bait", "timeline", "hud", "dock")

_SETTINGS_CATEGORIES = [
    ("layout", "Layout", [
        ("layout_mode",          "Mode",                     "int",   0,    0,     4,    1),
        ("word_mode",            "Per-word mode",            "bool",  True,  None, None, None),
        ("word_size",            "Per-word size",            "int",   1,    0,     2,    1),
        ("word_style",           "Per-word style",           "int",   5,    0,     6,    1),
        ("word_gradient",        "Per-word gradient",        "int",   3,    0,     3,    1),
    ]),
    ("karaoke", "Karaoke / wipe", [
        ("karaoke_lead",         "Lead (s)",                 "float", 0.6,  0.0,   2.0,  0.05),
        ("karaoke_sec_per_word", "Sec per word (legacy, unused)", "float", 0.6,  0.2,   2.0,  0.05),
        ("karaoke_sec_per_syllable", "Sec per syllable (wipe cap)", "float", 0.22, 0.08,  0.6,  0.02),
        ("karaoke_hold_max",     "Held-word max (s, 0=off)", "float", 3.0,  0.0,   8.0,  0.5),
        ("karaoke_ease",         "Ease (0=lin, 1=smooth)",   "float", 0.35, 0.0,   1.0,  0.05),
        ("inter_word_pause",     "Inter-word pause weight",  "float", 0.45, 0.0,   1.5,  0.05),
        ("poll_interval",        "Wipe refresh (s)",         "float", 0.02, 0.02,  0.20, 0.01),
    ]),
    ("onset", "Onset hybrid", [
        ("onset_enabled",        "Enabled",                  "bool",  True, None,  None, None),
        ("onset_blend",          "Blend (0=off, 1=full)",    "float", 0.35, 0.0,   1.0,  0.05),
        ("onset_silence_rms",    "Silence RMS (hold)",       "float", 0.028, 0.001, 0.15, 0.005),
        ("onset_flux_pct",       "Flux percentile (sens)",   "float", 70.0, 40.0,  95.0, 5.0),
        ("onset_min_interval",   "Min interval (s)",         "float", 0.06, 0.03,  0.20, 0.01),
    ]),
    ("sync", "Sync", [
        ("global_offset_step",   "Offset step (s)",          "float", 0.10, 0.05,  1.0,  0.05),
        ("shazam_offset_fudge",  "Shazam fudge (s)",         "float", 2.0,  0.0,   6.0,  0.25),
    ]),
    ("display", "Display", [
        ("context_lines",        "Context lines (min)",      "int",   3,    1,     9,    1),
        ("show_cover",           "Show artwork",             "bool",  True,  None, None, None),
        ("cover_accent",         "Accent from cover art",    "bool",  False, None, None, None),
    ]),
]

# flat list of all items (for defaults / load / apply)
_SETTINGS_SCHEMA = [item for _, _, items in _SETTINGS_CATEGORIES for item in items]

# Menu tree for the settings panel (the schema above stays flat).
# "advanced" is a group: it opens a submenu of the categories listed in
# _SETTINGS_ADVANCED instead of an item list.
_SETTINGS_TOP = ["layout", "display", "advanced"]
_SETTINGS_ADVANCED = ["karaoke", "onset", "sync"]
_SETTINGS_BLURBS = {
    "layout": "mode and per-word look",
    "display": "context lines, cover accent",
    "advanced": "wipe timing, onset, sync",
    "karaoke": "wipe timing and easing",
    "onset": "audio onset snapping",
    "sync": "offset step, shazam lag",
}
_SETTINGS_BY_KEY = {k: (label, items) for k, label, items in _SETTINGS_CATEGORIES}


def _default_settings():
    return {k: default for k, _, _, default, *_ in _SETTINGS_SCHEMA}


def load_settings():
    s = _default_settings()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            if isinstance(data, dict):
                for k in s:
                    if k in data:
                        s[k] = data[k]
        except Exception:
            pass
    return s


def save_settings(s):
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def apply_settings(s):
    """Push settings dict into the live module-level knobs."""
    global KARAOKE_LEAD, KARAOKE_HOLD_MAX, KARAOKE_SECONDS_PER_WORD, KARAOKE_SECONDS_PER_SYLLABLE, KARAOKE_EASE
    global ONSET_ENABLED, ONSET_BLEND, ONSET_SILENCE_RMS, GLOBAL_OFFSET_STEP
    global CONTEXT_LINES, _INTER_WORD_PAUSE, POLL_INTERVAL
    global ONSET_FLUX_PERCENTILE, ONSET_MIN_INTERVAL, SHAZAM_OFFSET_FUDGE
    global LAYOUT_MODE, COVER_ACCENT_ENABLED, WORD_MODE, WORD_SIZE, WORD_STYLE, WORD_GRADIENT
    global COVER_ENABLED
    KARAOKE_LEAD = float(s.get("karaoke_lead", 0.6))
    KARAOKE_HOLD_MAX = float(s.get("karaoke_hold_max", 3.0))
    KARAOKE_SECONDS_PER_WORD = float(s.get("karaoke_sec_per_word", 0.6))
    KARAOKE_SECONDS_PER_SYLLABLE = float(s.get("karaoke_sec_per_syllable", 0.22))
    KARAOKE_EASE = float(s.get("karaoke_ease", 0.35))
    ONSET_ENABLED = bool(s.get("onset_enabled", True))
    ONSET_BLEND = float(s.get("onset_blend", 0.35))
    ONSET_SILENCE_RMS = float(s.get("onset_silence_rms", 0.028))
    ONSET_FLUX_PERCENTILE = float(s.get("onset_flux_pct", 70.0))
    ONSET_MIN_INTERVAL = float(s.get("onset_min_interval", 0.06))
    GLOBAL_OFFSET_STEP = float(s.get("global_offset_step", 0.10))
    SHAZAM_OFFSET_FUDGE = float(s.get("shazam_offset_fudge", 2.0))
    CONTEXT_LINES = int(s.get("context_lines", 3))
    _INTER_WORD_PAUSE = float(s.get("inter_word_pause", 0.45))
    POLL_INTERVAL = float(s.get("poll_interval", 0.02))
    COVER_ACCENT_ENABLED = bool(s.get("cover_accent", False))
    COVER_ENABLED = COVER_AVAILABLE and bool(s.get("show_cover", True))
    try:
        LAYOUT_MODE = int(s.get("layout_mode", 0))
    except (TypeError, ValueError):
        LAYOUT_MODE = 0
    LAYOUT_MODE = max(0, min(4, LAYOUT_MODE))
    WORD_MODE = bool(s.get("word_mode", True))
    try:
        WORD_SIZE = max(0, min(2, int(s.get("word_size", 1))))
    except (TypeError, ValueError):
        WORD_SIZE = 1
    try:
        WORD_GRADIENT = max(0, min(3, int(s.get("word_gradient", 3))))
    except (TypeError, ValueError):
        WORD_GRADIENT = 0
    try:
        WORD_STYLE = max(0, min(6, int(s.get("word_style", 5))))
    except (TypeError, ValueError):
        WORD_STYLE = 1


LAYOUT_MODE = 0  # 0 classic · 1 bait · 2 timeline · 3 hud · 4 dock
WORD_GRADIENT = 3  # 0 off · 1 letter · 2 column · 3 wipe (per-word mode)
WORD_STYLE = 5  # 0 dots · 1 solid · 2 text · 3 sextant · 4 scaled (OSC 66) · 5 image · 6 ascii
WORD_SIZE = 1  # 0 small · 1 medium · 2 large (per-word mode)
WORD_MODE = True  # True → show only the currently sung word, big + centered


# runtime inter-word pause (overridable via settings; _char_weights reads it)
_INTER_WORD_PAUSE = 0.45


def show_settings(live, settings):
    """Hierarchical settings panel.

    top       Layout · Display · Advanced ›
    advanced  Karaoke / wipe · Onset hybrid · Sync
    items     ↑↓ select · ←→/space change · r reset · enter save · esc back

    q cancels from anywhere. Unsaved edits are flagged with a yellow *.
    """
    live.stop()
    console = Console()
    draft = dict(settings)
    level = "top"  # top | adv | items
    cursor = {"top": 0, "adv": 0, "items": 0}
    cat_key = None
    back_to = "top"

    tips = {
        "layout_mode": "classic · bait · timeline · hud · dock",
        "word_mode": "one word at a time, big + centered",
        "show_cover": "off = title + artist move to the top header (dock: bottom strip)",
        "word_style": "dots/solid/text/sextant/scaled/image/ascii",
        "word_gradient": "off/letter/column/wipe (wipe = karaoke fill)",
        "word_size": "small · medium · large",
        "onset_flux_pct": "lower = more sensitive (try 55-65 if n=0 a lot)",
        "onset_min_interval": "lower = allow denser onsets (rap / fast vocals)",
        "onset_blend": "0 = pure syllable  ·  1 = fully snap to onsets",
        "onset_silence_rms": "raise if wipe freezes too often on quiet parts",
        "shazam_offset_fudge": "extra lag compensation after SongRec match",
    }
    menu_tips = {"advanced": "tuning knobs, safe to leave alone"}
    named = ("layout_mode", "word_size", "word_style", "word_gradient")

    def _fmt(val, typ, key=None):
        if key == "word_gradient":
            try:
                return _WORD_GRAD_NAMES[int(val)]
            except (TypeError, ValueError, IndexError):
                return _WORD_GRAD_NAMES[0]
        if key == "word_style":
            try:
                return _WORD_STYLE_NAMES[int(val)]
            except (TypeError, ValueError, IndexError):
                return _WORD_STYLE_NAMES[1]
        if key == "word_size":
            try:
                return _WORD_SIZE_NAMES[int(val)]
            except (TypeError, ValueError, IndexError):
                return _WORD_SIZE_NAMES[1]
        if key == "layout_mode":
            try:
                return _LAYOUT_NAMES[int(val)]
            except (TypeError, ValueError, IndexError):
                return _LAYOUT_NAMES[0]
        if typ == "bool":
            return "on" if val else "off"
        if typ == "float":
            return f"{float(val):.3f}".rstrip("0").rstrip(".")
        return str(val)

    def _same(a, b):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return a == b

    def _any_dirty():
        return any(
            not _same(draft[k], settings.get(k, d))
            for k, _l, _t, d, *_ in _SETTINGS_SCHEMA
        )

    def _bar(val, lo, hi, selected, n=12):
        span = float(hi) - float(lo)
        frac = 0.0 if span <= 0 else (float(val) - float(lo)) / span
        pos = int(round(max(0.0, min(1.0, frac)) * (n - 1)))
        on = f"bold {ACCENT}" if selected else "dim"
        bar = Text()
        bar.append("━" * pos, style=on)
        bar.append("●", style=on)
        bar.append("─" * (n - 1 - pos), style="dim")
        return bar

    def _marker(selected):
        return Text("›" if selected else " ", style=f"bold {ACCENT}")

    def _menu_table(keys):
        t = Table.grid(expand=True, padding=(0, 1))
        t.add_column(width=1)
        t.add_column(width=16, no_wrap=True)
        t.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        t.add_column(width=1)
        cur = cursor[level]
        for i, key in enumerate(keys):
            sel = i == cur
            if key == "advanced":
                label = "Advanced"
            else:
                label = _SETTINGS_BY_KEY[key][0]
            t.add_row(
                _marker(sel),
                Text(label, style=f"bold {ACCENT}" if sel else ""),
                Text(_SETTINGS_BLURBS.get(key, ""), style="dim"),
                Text("›", style=f"bold {ACCENT}" if sel else "dim"),
            )
        return t

    def _items_table(items):
        t = Table.grid(expand=True, padding=(0, 1))
        t.add_column(width=1)
        t.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        t.add_column(width=12)
        t.add_column(width=13, justify="right", no_wrap=True)
        cur = cursor["items"]
        for i, item in enumerate(items):
            key, label, typ, default, lo, hi, step = item
            sel = i == cur
            val = draft[key]
            hot = f"bold {ACCENT}" if sel else ""
            name = Text(label, style=hot)
            if not _same(val, settings.get(key, default)):
                name.append(" *", style="yellow")
            control = Text("")
            if typ == "bool":
                value = Text(
                    "● on" if val else "○ off",
                    style=(f"bold {ACCENT}" if sel else ACCENT) if val else "dim",
                )
            elif key in named:
                s = _fmt(val, typ, key=key)
                value = Text(f"‹ {s} ›" if sel else s, style=hot or "dim")
            else:
                control = _bar(val, lo, hi, sel)
                value = Text(_fmt(val, typ, key=key), style=hot or "dim")
            t.add_row(_marker(sel), name, control, value)
        return t

    def _render():
        console.clear()
        width = max(40, min(console.width - 2, 72))

        crumb = Text()
        parts = ["settings"]
        if level == "adv" or (level == "items" and back_to == "adv"):
            parts.append("advanced")
        if level == "items":
            parts.append(_SETTINGS_BY_KEY[cat_key][0].lower())
        for i, p in enumerate(parts):
            last = i == len(parts) - 1
            crumb.append(p, style=f"bold {ACCENT}" if last else "dim")
            if not last:
                crumb.append("  ›  ", style="dim")

        tip = ""
        if level == "top":
            body = _menu_table(_SETTINGS_TOP)
            tip = menu_tips.get(_SETTINGS_TOP[cursor["top"]], "")
            hint = "↑↓ move · enter open · q close"
        elif level == "adv":
            body = _menu_table(_SETTINGS_ADVANCED)
            hint = "↑↓ move · enter open · esc back · q close"
        else:
            items = _SETTINGS_BY_KEY[cat_key][1]
            body = _items_table(items)
            key, _label, typ, default, lo, hi, step = items[cursor["items"]]
            tip = tips.get(key, "")
            if not tip and typ != "bool" and lo is not None and hi is not None:
                tip = (
                    f"{lo} … {hi}  ·  step {step}  ·  "
                    f"default {_fmt(default, typ, key=key)}"
                )
            hint = "↑↓ move · ←→ change · r reset · enter save · esc back"

        status = Text(" ")
        if level == "items" and _any_dirty():
            status = Text("* unsaved · enter saves", style="yellow")

        panel = Panel(
            Group(
                crumb,
                Text(""),
                body,
                Text(""),
                Text(tip or " ", style="dim"),
                status,
            ),
            border_style=ACCENT,
            padding=(1, 3),
            width=width,
            title=" Sweep Lyrics ",
            title_align="left",
            subtitle=Text(f" {hint} ", style="dim italic"),
            subtitle_align="center",
        )
        console.print(Align.center(panel))

    def _nudge(item, direction):
        key, label, typ, default, lo, hi, step = item
        if typ == "bool":
            draft[key] = not draft[key]
            return
        delta = step if direction > 0 else -step
        if typ == "int":
            new = int(draft[key]) + int(delta)
            draft[key] = max(int(lo), min(int(hi), new))
        else:
            new = float(draft[key]) + float(delta)
            new = round(new / step) * step
            draft[key] = max(float(lo), min(float(hi), new))

    applied = False
    while True:
        _render()
        ch = poll_key(60)
        if ch is None:
            continue

        if ch in ("q", "Q"):
            applied = False
            break

        if level in ("top", "adv"):
            entries = _SETTINGS_TOP if level == "top" else _SETTINGS_ADVANCED
            n = len(entries)
            if ch == "esc":
                if level == "adv":
                    level = "top"
                    continue
                applied = False
                break
            if ch == "up":
                cursor[level] = (cursor[level] - 1) % n
            elif ch == "down":
                cursor[level] = (cursor[level] + 1) % n
            elif ch in ("enter", "right", " "):
                entry = entries[cursor[level]]
                if entry == "advanced":
                    level = "adv"
                else:
                    cat_key = entry
                    back_to = level
                    cursor["items"] = 0
                    level = "items"
            elif ch == "left" and level == "adv":
                level = "top"
        else:
            items = _SETTINGS_BY_KEY[cat_key][1]
            n_items = len(items)
            item = items[cursor["items"]]
            key, label, typ, default, lo, hi, step = item

            if ch == "esc":
                level = back_to
                continue
            if ch == "enter":
                settings.clear()
                settings.update(draft)
                apply_settings(settings)
                save_settings(settings)
                applied = True
                break
            if ch == "up":
                cursor["items"] = (cursor["items"] - 1) % n_items
            elif ch == "down":
                cursor["items"] = (cursor["items"] + 1) % n_items
            elif ch in ("r", "R"):
                draft[key] = default
            elif ch in ("left", "right", "+", "-", " "):
                direction = -1 if ch in ("left", "-") else 1
                _nudge(item, direction)

    live.start()
    return applied


# ------------------------------------------------------------------ render

def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


_VOWEL_GROUP_RE = re.compile(r"[aeiouyAEIOUY]+")
# Differentiated pause weight after punctuation — a sentence-ending mark
# (period, exclamation, question mark) gets a longer breath than a mid-
# phrase comma/semicolon, and a dash reads more like a comma-length
# beat than a full stop.
#
# All values here are set ABOVE _INTER_WORD_PAUSE (the plain-space
# default) — punctuation should always buy MORE breathing room than a
# bare word gap, never less. The previous numbers (0.35 for comma vs.
# 0.45 default) accidentally made a comma pause shorter than an
# ordinary space, which is backwards: a mid-phrase breath mark should
# read as a bigger beat than "just moving to the next word", not a
# smaller one.
_PAUSE_WEIGHT = {
    ",": 0.60, ";": 0.60, "—": 0.60, "-": 0.60,
    ":": 0.70,
    ".": 0.90, "!": 0.90, "?": 0.90, "…": 1.10,
}


_CMU_DICT = None


def _get_cmu_dict():
    """Lazy-loads the CMU Pronouncing Dictionary once (~130k English
    words + human-validated pronunciations) and caches it for the
    life of the process. Returns {} if the `cmudict` package isn't
    installed — callers treat that the same as a per-word miss."""
    global _CMU_DICT
    if _CMU_DICT is None:
        if HAVE_CMUDICT:
            try:
                _CMU_DICT = cmudict.dict()
            except Exception:
                _CMU_DICT = {}
        else:
            _CMU_DICT = {}
    return _CMU_DICT


def _cmu_syllables(word: str):
    """Real syllable count for an English word via CMU Dict, counting
    phonemes that carry a stress digit (0/1/2) in the first listed
    pronunciation. Returns None — not 0 — when the package is missing
    or the word (punctuation-stripped) isn't in the dictionary, so
    callers can tell 'no data' apart from 'zero syllables' and fall
    back to the vowel-group heuristic below. Indonesian lyrics and
    anything not in the ~130k-word list (artist names, slang, lrclib
    typos) will always miss here and fall through by design — this is
    a lookup layer on top of the heuristic, not a replacement for it."""
    d = _get_cmu_dict()
    if not d:
        return None
    key = re.sub(r"[^a-zA-Z']", "", word).lower()
    if not key or key not in d:
        return None
    phones = d[key][0]
    return sum(1 for p in phones if p[-1:].isdigit()) or None



# Indonesian syllabification is close to "one vowel letter = one
# syllable", the opposite of the English vowel-*cluster*-as-one
# heuristic below — e.g. "buaya" is bu-a-ya (3 syllables), not 1.
# True diphthongs (two vowel letters sung as one nucleus) are folded.
# Intentionally does NOT fold "ia"/"ua"/"eo" — those stay two nuclei
# in Indonesian singing (di-a, bu-a-ya, etc.).
_ID_DIPHTHONGS = ("ai", "au", "oi", "ei")
_ID_VOWEL_RE = re.compile(r"[aeiouAEIOU]")


def _estimate_syllables_id(word: str) -> int:
    """Per-vowel-letter syllable estimate for Bahasa Indonesia, used
    as the fallback for any word CMU doesn't recognize (i.e. almost
    always Indonesian lyrics, since CMU is English-only). Counts each
    vowel letter as its own syllable, then folds true diphthongs
    (ai/au/oi/ei) back down to one — 'saya' -> 2, 'harimau' -> 3,
    'santai' -> 2, 'survei' -> 2. Does NOT apply English silent-e;
    Indonesian has none. Leaves "ia"/"ua" alone so 'buaya' stays 3
    and 'dia' stays 2."""
    w = re.sub(r"[^a-zA-Z]", "", word).lower()
    if not w:
        return 1
    n = len(_ID_VOWEL_RE.findall(w))
    for d in _ID_DIPHTHONGS:
        n -= w.count(d)
    return max(1, n)


def _estimate_syllables(word: str) -> int:
    """Syllable count used to weight a word's share of the karaoke
    sweep. Tries the CMU Pronouncing Dictionary first (real
    human-validated pronunciations, English only); if that misses —
    which for a mostly-Indonesian lyric sheet is nearly every word —
    falls back to the Indonesian per-vowel-letter estimator above
    rather than the English vowel-cluster heuristic, since grouping
    adjacent vowel letters into a single syllable badly undercounts
    common Indonesian words (e.g. 'saya', 'buaya', 'dua')."""
    cmu = _cmu_syllables(word)
    if cmu is not None:
        return cmu
    return _estimate_syllables_id(word)


def _estimate_line_seconds(text: str, sec_per_syllable: float = None) -> float:
    """Plausible sung duration for a full lyric line, in seconds —
    used to cap the karaoke wipe when the real gap to the next
    timestamp (raw_span) is inflated by an instrumental break or held
    note. Sums per-word syllable estimates instead of just counting
    words: a flat per-word budget treats 'lagi' (2 syllables) and
    'Penuhilah' (4 syllables) as equally long to sing, which for
    syllable-heavy Indonesian lines undershoots badly — the wipe races
    through in a couple seconds, then the line just sits fully-lit
    doing nothing for the rest of its time on screen instead of
    tracking the actual gaps between words. Empty lines fall back to
    a minimal one-word budget.

    `sec_per_syllable` lets a caller pass a per-SONG calibrated pace
    (see `_calibrate_song_tempo`) instead of the global
    KARAOKE_SECONDS_PER_SYLLABLE constant — a fast rap track and a
    slow ballad don't sing at the same pace, and a fixed global number
    either overshoots one or undershoots the other."""
    sps = KARAOKE_SECONDS_PER_SYLLABLE if sec_per_syllable is None else sec_per_syllable
    words = (text or "").split()
    if not words:
        return max(1.2, KARAOKE_SECONDS_PER_WORD)
    syllables = sum(_estimate_syllables(w) for w in words)
    return max(1.2, syllables * sps)


def _calibrate_song_tempo(entries):
    """One-off per-song estimate of a plausible seconds-per-syllable
    singing pace, derived from the song's OWN line timestamps instead
    of relying purely on the fixed KARAOKE_SECONDS_PER_SYLLABLE
    constant — a fast rap track and a slow ballad don't sing at the
    same rate, so a single global number either overshoots one or
    undershoots the other.

    Computed once, right after a track's lyrics are loaded (cheap:
    pure arithmetic over timestamps already in hand, no audio
    analysis needed). For every consecutive line pair, takes the gap
    between their timestamps divided by the estimated syllable count
    of the earlier line, then returns the MEDIAN of those ratios —
    median (not mean) so a handful of instrumental breaks or
    stacked/near-duplicate timestamps (backing vocals sharing a
    line's start) don't skew the whole song's pace.

    Implausible ratios are filtered out before taking the median:
    - gap <= 0.3s: almost certainly a stacked/duplicate timestamp,
      not a real line-to-line gap.
    - gap > 15s: almost certainly an instrumental break or bridge,
      not the line being sung the whole time.
    - resulting ratio outside [0.05, 1.2] sec/syllable: outside any
      plausible singing tempo, so likely still break-contaminated
      even after the gap filter above.

    Returns None (meaning "fall back to the global constant") when
    there aren't enough clean data points (<6) to trust a median —
    e.g. very short songs, or lyrics sheets that are mostly breaks."""
    if not entries or len(entries) < 2:
        return None
    ratios = []
    for i in range(len(entries) - 1):
        line_start = entries[i][0]
        next_start = entries[i + 1][0]
        gap = next_start - line_start
        if gap <= 0.3 or gap > 15.0:
            continue
        syl = sum(_estimate_syllables(w) for w in (entries[i][1] or "").split())
        if syl <= 0:
            continue
        ratio = gap / syl
        if 0.05 <= ratio <= 1.2:
            ratios.append(ratio)
    if len(ratios) < 6:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def _char_weights(text: str):
    """Per-character weight array the same length as `text`, used to
    build a cumulative 'how far sung' curve for the karaoke sweep.
    Each word's syllable-estimated weight is spread evenly across its
    own letters; a small pause weight fills gaps and gets a bump right
    after punctuation (bigger for a sentence-ending mark than a comma,
    see _PAUSE_WEIGHT), so the sweep eases up at line breaks instead of
    plowing straight through them at the same rate as mid-word.

    Also adds a modest inter-word pause on every space (even without
    punctuation) — real singing often breathes between words
    ("Perjalanan……melintasi……kota"), and a flat sweep was ignoring that."""
    weights = [0.15] * len(text)  # baseline so gaps aren't zero-width
    for m in re.finditer(r"\S+", text):
        word = m.group()
        per_char = _estimate_syllables(word) / len(word)
        for i in range(m.start(), m.end()):
            weights[i] = per_char
        if m.end() < len(text):
            # punctuation-aware pause, or a default breath between words
            weights[m.end()] += _PAUSE_WEIGHT.get(word[-1], _INTER_WORD_PAUSE)
    return weights



_ACCENT_RGB_CACHE = {"rgb": None, "ts": 0.0}
KITTY_COLOR_REFRESH = 5.0  # seconds — how often the karaoke gradient
                            # re-polls Kitty's live theme colors, so a
                            # runtime theme switch (e.g. a Noctalia
                            # Material You reload) gets picked up without
                            # restarting the app, without shelling out to
                            # `kitty @` on every single frame.

# Cover-art-driven accent (hex string or None). Opt-in via settings
# (`cover_accent`); when off, karaoke follows the live Kitty theme
# color exactly like before this feature existed.
COVER_ACCENT_ENABLED = False
COVER_ACCENT_HEX = None
COVER_ACCENT_RGB = None  # cached (r,g,b) companion

# Minimal mode (ZEN_MODE internally) — toggled with 'z'. Collapses context lines and chrome
# so only the active lyric (+ optional translation) stays on screen.
ZEN_MODE = False

# Vocal-intensity pulse: recent peak RMS (0..1-ish) used to slightly
# lift karaoke accent brightness when the vocal is strong.
_VOCAL_INTENSITY = 0.0

_ANSI_NAME_TO_KITTY_INDEX = {
    "black": 0, "red": 1, "green": 2, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
    "bright_black": 8, "bright_red": 9, "bright_green": 10,
    "bright_yellow": 11, "bright_blue": 12, "bright_magenta": 13,
    "bright_cyan": 14, "bright_white": 15,
}


def _query_kitty_color(name):
    """Asks the running Kitty instance for the live hex value of a
    named ANSI color slot via `kitty @ get-colors` — e.g. 'bright_magenta'
    resolves to whatever colorN the user's *currently active* theme maps
    it to, rather than a generic fallback. Returns None on any failure
    (remote control not enabled, not actually running inside Kitty, no
    kitty binary, timeout, unrecognized name, etc.) so callers can fall
    back to Rich's built-in ANSI approximation."""
    idx = _ANSI_NAME_TO_KITTY_INDEX.get(name)
    if idx is None:
        return None
    try:
        out = subprocess.run(
            ["kitty", "@", "get-colors"],
            capture_output=True, text=True, timeout=1.0,
        )
        if out.returncode != 0:
            return None
        key = f"color{idx}"
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == key:
                return parts[1]  # e.g. "#e5c07b"
    except Exception:
        pass
    return None


def _hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip("#")
    return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))


def _get_accent_rgb():
    """Resolves accent to (r,g,b). Prefers cover-art color only when
    COVER_ACCENT_ENABLED is on and a sample exists; otherwise the live
    Kitty theme color for the ACCENT ANSI slot (same slot the song
    title's plain-ANSI style resolves to on screen), falling back to
    Rich's static approximation only if the live query fails outright
    (remote control off, not running in Kitty, etc.)."""
    if COVER_ACCENT_ENABLED and COVER_ACCENT_RGB is not None:
        return COVER_ACCENT_RGB
    now = time.time()
    cache = _ACCENT_RGB_CACHE
    if cache["rgb"] is not None and now - cache["ts"] < KITTY_COLOR_REFRESH:
        return cache["rgb"]
    hexval = _query_kitty_color(ACCENT)
    if hexval:
        rgb = _hex_to_rgb(hexval)
    else:
        tc = Color.parse(ACCENT).get_truecolor()
        rgb = (tc.red, tc.green, tc.blue)
    cache["rgb"] = rgb
    cache["ts"] = now
    return rgb


def _boost_accent_rgb(rgb, min_saturation=0.25, max_saturation=0.25, min_value=0.78,
                      intensity=0.0):
    """Material You-ish accent: keep the hue, clamp saturation, lift
    value for readability. `intensity` (0..1) from vocal RMS pushes
    saturation + value so the wipe visibly 'breathes' with the vocal."""
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    # base Material You band
    s = max(min_saturation, min(max_saturation, s * 0.75 + 0.18))
    v = max(v, min_value)
    # vocal pulse — stronger so chorus vs verse is obvious on-screen
    pulse = max(0.0, min(1.0, intensity))
    s = min(0.92, s + pulse * 0.32)
    v = min(0.98, v + pulse * 0.20)
    r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
    return (round(r2 * 255), round(g2 * 255), round(b2 * 255))


_AMBIENT_GLYPHS = " .·∘○◦°"  # sparse -> denser, used for the instrumental
                              # ambient pattern below


def render_ambient_pattern(pos, width, height, color):
    """Slow drifting wave pattern for tracks with no lyrics at all — no
    audio access needed, just `pos` (elapsed playback seconds) driving
    the phase, so it evolves smoothly and in sync with the track
    without depending on any external tool being installed. Two
    overlapping sine waves at different speeds/wavelengths give it a
    non-repetitive, slightly organic drift rather than an obvious
    single ripple. Returns `height` Rich Text rows, each `width`
    characters wide."""
    lines = []
    for row in range(height):
        t = Text()
        row_phase = row / max(1, height - 1)
        for col in range(width):
            x = col / max(1, width - 1)
            wave = (
                math.sin(x * 6.0 + pos * 0.6 + row_phase * 2.0)
                + math.sin(x * 3.0 - pos * 0.35 + row_phase * 4.0) * 0.6
            )
            # wave is roughly in [-1.6, 1.6] -> normalize to [0, 1]
            level = max(0.0, min(1.0, (wave + 1.6) / 3.2))
            idx = int(level * (len(_AMBIENT_GLYPHS) - 1))
            dim = idx <= 1
            style = f"dim {color}" if dim else color
            t.append(_AMBIENT_GLYPHS[idx], style=style)
        lines.append(t)
    return lines


KARAOKE_EASE = 0.35  # 0 = pure linear sweep (old behavior), 1 = full
                      # smoothstep ease. Real singing isn't a constant
                      # tempo through a line — it tends to land softer at
                      # the start and end of a phrase and move quicker
                      # through the middle. This blends the linear frac
                      # toward a smoothstep curve (slow-fast-slow) by that
                      # amount, applied on top of the existing per-word
                      # syllable weighting rather than replacing it — the
                      # weights still decide how much of the line each
                      # word gets, this only reshapes the pace *within*
                      # that timeline. Still a guess, same as before, just
                      # a better-shaped one — there's no real per-word
                      # timing data behind it.

# ------------------------------------------------ lightweight onset detect
# Pure-numpy spectral flux onset tracker. Optional hybrid for karaoke wipe:
# when live audio is available it nudges the cut point toward real vocal
# attacks instead of pure syllable-estimate timing. Zero hard dependency
# beyond numpy (already common); capture uses parec/pw-record if present.
# Completely defensive — any failure silently falls back to the existing
# syllable + ease path.

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

ONSET_ENABLED = True          # master switch; set False to disable entirely
ONSET_SR = 16000              # capture sample rate (16 kHz is enough for onset)
ONSET_HOP = 256               # hop size in samples (~16 ms at 16 kHz)
ONSET_WIN = 512               # STFT window
ONSET_HISTORY_SEC = 2.0       # how far back we keep onset timestamps
ONSET_MIN_INTERVAL = 0.06     # min seconds between two accepted onsets
ONSET_FLUX_PERCENTILE = 70    # adaptive threshold: only peaks above this %
ONSET_DEVICE = SHAZAM_DEVICE  # reuse the same monitor device as SongRec
ONSET_BLEND = 0.35            # 0 = ignore onsets, 1 = fully snap frac to onset map
                              # kept modest so drums that still leak through
                              # can't yank the wipe hard; monotonic clamp
                              # below also prevents backward jumps.
ONSET_SILENCE_RMS = 0.028     # below this → treat as pause between words;
                              # voiced-time clock freezes so the wipe holds
                              # instead of sweeping through the silence.
                              # Slightly higher than the old 0.02 so short
                              # gaps / soft instrumental bits count as
                              # silence more readily (more aggressive hold).


def _spectral_flux_onsets(samples, sr=None, hop=None, win=None,
                          min_interval=None, flux_percentile=None):
    """Pure-numpy spectral flux → list of onset times (seconds) relative
    to the start of `samples`.

    Algorithm:
      1. Frame the signal, apply Hann window, magnitude STFT (real FFT).
      2. Spectral flux = sum of positive bin-wise differences between
         consecutive frames (half-wave rectified).
      3. Adaptive threshold via percentile of the flux curve.
      4. Peak pick with a minimum inter-onset interval.

    Returns [] on any failure / too-short input so callers can no-op.
    Globals (ONSET_*) are read at call time so settings changes apply
    without restarting the process.
    """
    if sr is None:
        sr = ONSET_SR
    if hop is None:
        hop = ONSET_HOP
    if win is None:
        win = ONSET_WIN
    if min_interval is None:
        min_interval = ONSET_MIN_INTERVAL
    if flux_percentile is None:
        flux_percentile = ONSET_FLUX_PERCENTILE
    if not HAVE_NUMPY or samples is None or len(samples) < win * 2:
        return []
    try:
        x = np.asarray(samples, dtype=np.float64)
        if x.ndim > 1:
            x = x.mean(axis=1)
        # remove DC, light normalize
        x = x - np.mean(x)
        peak = np.max(np.abs(x)) + 1e-9
        x = x / peak

        n_frames = 1 + (len(x) - win) // hop
        if n_frames < 3:
            return []

        window = np.hanning(win)
        # magnitude spectrogram (only positive freqs)
        n_bins = win // 2 + 1
        specs = np.empty((n_frames, n_bins), dtype=np.float64)
        for i in range(n_frames):
            start = i * hop
            frame = x[start:start + win] * window
            specs[i] = np.abs(np.fft.rfft(frame))

        # band-limit flux to ~200 Hz – 4 kHz (vocal body). Kicks / sub
        # and harsh cymbals live mostly outside this, so they stop
        # yanking the karaoke wipe around.
        freqs = np.fft.rfftfreq(win, d=1.0 / sr)
        band = (freqs >= 200.0) & (freqs <= 4000.0)
        if not np.any(band):
            band = slice(None)

        # half-wave rectified spectral flux (band-limited)
        diff = np.diff(specs[:, band], axis=0)
        flux = np.sum(np.maximum(diff, 0.0), axis=1)
        if flux.size == 0:
            return []

        # light smoothing so isolated noise bins don't spike
        if len(flux) >= 3:
            flux = np.convolve(flux, np.ones(3) / 3.0, mode="same")

        # adaptive threshold: percentile of flux, with a floor from median
        thr = np.percentile(flux, flux_percentile)
        med = np.median(flux)
        thr = max(thr, med * 2.5)
        if thr <= 0:
            thr = np.mean(flux) * 1.5

        # peak pick — must be local max and clearly above neighbours
        onsets = []
        last_t = -1e9
        for i in range(2, len(flux) - 2):
            if (flux[i] >= thr
                    and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1]
                    and flux[i] >= flux[i - 2] and flux[i] >= flux[i + 2]):
                t = (i * hop + win // 2) / float(sr)
                if t - last_t >= min_interval:
                    onsets.append(t)
                    last_t = t
        return onsets
    except Exception:
        return []


class OnsetTracker:
    """Rolling real-time onset tracker.

    Keeps a short ring buffer of mono float PCM from the system monitor,
    runs spectral flux, stores wall-clock onset timestamps.

    Diagnostic fields (shown in debug overlay):
      .last_rms  — RMS of most recent chunk (0 ≈ silence / no data)
      .buf_sec   — seconds of audio currently buffered
      .backend   — 'parec' / 'parec-default' / 'pw-record' / None
    """

    def __init__(self, sr=ONSET_SR, history_sec=ONSET_HISTORY_SEC):
        self.sr = sr
        self.history_sec = history_sec
        self._buf = np.zeros(0, dtype=np.float64) if HAVE_NUMPY else None
        self._onsets_wall = []
        self._proc = None
        self._last_feed_wall = 0.0
        self._max_buf_samples = int(sr * 1.5)
        self.last_rms = 0.0
        self.buf_sec = 0.0
        self.backend = None

    @property
    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self, device=ONSET_DEVICE):
        """Spawn non-blocking raw capture. Prefer parec (Pulse compat —
        same path SongRec uses), fall back to pw-record."""
        if not HAVE_NUMPY or not ONSET_ENABLED:
            return False
        if self.alive:
            return True

        candidates = []
        # parec first: more reliable on PipeWire+pulse-compat with named
        # monitor sources. pw-record --target can attach to a silent node
        # and still report alive.
        if shutil.which("parec"):
            candidates.append(("parec", [
                "parec", "-d", device,
                "--rate", str(self.sr), "--channels=1",
                "--format=s16le", "--raw", "--latency-msec=30",
            ]))
            candidates.append(("parec-default", [
                "parec",
                "--rate", str(self.sr), "--channels=1",
                "--format=s16le", "--raw", "--latency-msec=30",
            ]))
        if shutil.which("pw-record"):
            candidates.append(("pw-record", [
                "pw-record", "--target", device,
                "--rate", str(self.sr), "--channels", "1",
                "--format", "s16", "--latency", "30", "-",
            ]))

        for name, cmd in candidates:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                time.sleep(0.08)
                if proc.poll() is not None:
                    continue
                try:
                    import fcntl
                    fd = proc.stdout.fileno()
                    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                except Exception:
                    pass
                self._proc = proc
                self.backend = name
                return True
            except Exception:
                continue
        self._proc = None
        self.backend = None
        return False

    def stop(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            self.backend = None

    def poll(self):
        """Non-blocking drain + onset detect. Safe every UI frame."""
        if not HAVE_NUMPY or not self.alive:
            return
        try:
            raw = b""
            fd = self._proc.stdout.fileno()
            while True:
                try:
                    chunk = os.read(fd, 8192)
                except BlockingIOError:
                    break
                except Exception:
                    break
                if not chunk:
                    break
                raw += chunk
            if not raw:
                return

            n = len(raw) // 2
            if n == 0:
                return
            samples = np.frombuffer(raw[:n * 2], dtype="<i2").astype(np.float64)
            samples /= 32768.0
            self.last_rms = float(np.sqrt(np.mean(samples * samples)))

            self._buf = np.concatenate([self._buf, samples])
            if len(self._buf) > self._max_buf_samples:
                self._buf = self._buf[-self._max_buf_samples:]
            self.buf_sec = len(self._buf) / float(self.sr)

            now = time.time()
            if now - self._last_feed_wall < 0.06:
                return
            self._last_feed_wall = now

            # skip flux on near-silence (avoids percentile-of-noise peaks)
            if self.last_rms < 0.001:
                buf_rms = (
                    float(np.sqrt(np.mean(self._buf[-min(len(self._buf), self.sr):] ** 2)))
                    if len(self._buf) else 0.0
                )
                if buf_rms < 0.002:
                    return

            rel_onsets = _spectral_flux_onsets(self._buf, sr=self.sr)
            buf_dur = len(self._buf) / float(self.sr)
            for t_rel in rel_onsets:
                t_wall = now - (buf_dur - t_rel)
                if not self._onsets_wall or (t_wall - self._onsets_wall[-1]) >= ONSET_MIN_INTERVAL:
                    self._onsets_wall.append(t_wall)

            cutoff = now - self.history_sec
            self._onsets_wall = [t for t in self._onsets_wall if t >= cutoff]
        except Exception:
            pass

    def recent_onsets(self, since_wall=0.0):
        return [t for t in self._onsets_wall if t >= since_wall]


def _onset_adjusted_frac(frac, line_start, wipe_span, sync_pos, tracker,
                         anchor_wall, anchor_sync, blend=ONSET_BLEND):
    """Hybrid: if the onset tracker saw attacks inside this line's window,
    remap `frac` so the wipe lands closer to those real onsets.

    `anchor_wall` / `anchor_sync` = (time.time(), sync_pos) captured when
    the current line first became active. Assumes ~1x playback without
    seeks — good enough for normal listening. Soft blend (0..1) so a
    noisy/silent capture can never freeze the wipe.

    The configured `blend` is treated as a CEILING, not a fixed rate:
    it's scaled down by how many onsets actually landed inside this
    line's window. A single onset is cheap to get from a stray drum
    hit or noise leaking into the capture — trusting it at full blend
    strength risks yanking the wipe on a false positive. Several
    onsets landing consistently inside the window is a much stronger
    signal the vocal is actually there, so lines with more onsets earn
    closer to the full configured blend.
    """
    if (tracker is None or wipe_span <= 0 or blend <= 0
            or anchor_wall is None or anchor_sync is None):
        return frac
    try:
        # map wall-clock onsets → approximate playback position
        onsets_wall = tracker.recent_onsets(since_wall=anchor_wall - 0.05)
        if len(onsets_wall) < 1:
            return frac
        onsets_pos = [
            anchor_sync + (t - anchor_wall)
            for t in onsets_wall
        ]
        # only keep onsets that fall inside this line's wipe window
        lo = line_start
        hi = line_start + wipe_span
        in_line = sorted(p for p in onsets_pos if lo <= p <= hi + 0.15)
        if not in_line:
            return frac

        # confidence scaling: 1 onset → 40% of configured blend,
        # 3+ onsets → full configured blend, linear in between.
        # (confidence=0 at n=1, confidence=1 at n>=3, so the 0.4 floor
        # actually lands on a single onset instead of on zero onsets)
        confidence = min(1.0, max(0.0, (len(in_line) - 1) / 2.0))
        effective_blend = blend * (0.4 + 0.6 * confidence)

        # build onset-based progress from attacks that have already passed
        passed = [p for p in in_line if p <= sync_pos]
        if not passed:
            return frac  # no onset yet → don't pull wipe backward
        last = passed[-1]
        onset_frac = max(0.0, min(1.0, (last - line_start) / wipe_span))
        # slight look-ahead toward the next onset
        future = [p for p in in_line if p > sync_pos]
        if future:
            nxt = future[0]
            gap = nxt - last
            if gap > 1e-3:
                t = max(0.0, min(1.0, (sync_pos - last) / gap))
                nxt_frac = max(0.0, min(1.0, (nxt - line_start) / wipe_span))
                onset_frac = onset_frac + (nxt_frac - onset_frac) * t * 0.5

        blended = frac * (1.0 - effective_blend) + onset_frac * effective_blend
        # MONOTONIC: never let drums / false onsets pull the wipe backward
        # relative to the syllable-estimate baseline.
        return max(frac, blended)
    except Exception:
        return frac


def _ease_wipe(t):
    """Smoothstep: 3t^2 - 2t^3. Symmetric ease-in/ease-out — derivative
    (i.e. sweep speed) is 0 at both ends and peaks at t=0.5, so the cut
    point crawls at the very start/end of a line and moves fastest
    through the middle."""
    return t * t * (3.0 - 2.0 * t)


def _snap_cut_off_whitespace(text, cut, n):
    """If the wipe cut lands on a space, don't just skip the underline
    for that frame — snap the highlight to the nearest real letter
    instead. Prefers snapping backward to the last letter of the word
    just finished; if there's nothing before it (start of text), snaps
    forward to the first letter of the next word. Keeps the underline
    glued to a letter through the whole inter-word gap instead of
    flickering off every time the sweep crosses whitespace."""
    if not text[cut].isspace():
        return cut
    j = cut - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j >= 0:
        return j
    j = cut + 1
    while j < n and text[j].isspace():
        j += 1
    return j if j < n else cut


# Never skip a word: if a frame jumped past word(s) (slow playerctl poll,
# very short first word, duplicate timestamps), step ONE word per frame so
# every word gets at least one rendered frame. State is per-caller so
# word-mode and wipe don't double-step each other.
_WORD_STEP = {}


def _lead_for(elapsed):
    """KARAOKE_LEAD, ramped in over the first 2*LEAD seconds of a line.

    A flat lead makes the wipe start already KARAOKE_LEAD seconds 'in' the
    moment a line becomes current, so short first words ('tak', 'aku', ...)
    are consumed before a single frame is drawn. Ramping the lead from 0
    keeps it continuous (reaches the full lead at elapsed = 2*LEAD) while
    letting the first word actually show up."""
    if KARAOKE_LEAD <= 0:
        return 0.0
    return KARAOKE_LEAD * min(1.0, max(0.0, elapsed) / (2.0 * KARAOKE_LEAD))


def _word_dur(ws, nxt, est, end=None):
    """Wipe duration for a word that starts at `ws`. Old behaviour capped it
    to ~2x the syllable estimate, which finished the wipe early on held /
    melisma words. If the gap to the next start is within KARAOKE_HOLD_MAX we
    trust the gap (a held note); longer gaps (breaks) stay capped."""
    if end is not None and end > ws:
        return max(end - ws, 0.05)  # real end time from the provider
    cap = max(est * 2.0, 0.5)
    if nxt is None:
        return cap
    gap = max(nxt - ws, 0.05)
    if KARAOKE_HOLD_MAX > cap and gap <= KARAOKE_HOLD_MAX:
        return gap
    return min(gap, cap)


def _stepped_idx(caller, key, cur):
    st = _WORD_STEP.get(caller)
    if st is None or st[0] != key:
        st = [key, -1]              # new line -> start before first word
        _WORD_STEP[caller] = st
    shown = st[1]
    if cur < shown:                 # seek backwards
        shown = cur
    elif cur > shown + 1:           # skipped frame(s) -> catch up 1 word
        shown += 1
    else:
        shown = cur
    st[1] = shown
    return shown, shown != cur      # (index to show, is_catching_up)


def _karaoke_words(text, word_timings, sync_pos, accent_hex, line_end=None):
    """Word-level wipe with per-letter interpolation inside the active word.

    Rich Sync only gives word START times, so the active word's duration is
    the gap to the next word's start (capped so long pauses don't crawl),
    and the underline follows the letter currently being sung. Returns None
    when `text` doesn't match the rich-sync words (e.g. romanized) so the
    caller can fall back to the syllable-estimate path."""
    spans, pos = [], 0
    for wstart, wtext in word_timings:
        i = text.find(wtext, pos)
        if i < 0:
            return None
        spans.append((wstart, i, i + len(wtext)))
        pos = i + len(wtext)

    cur = -1
    for k, (ws, _, _) in enumerate(spans):
        if sync_pos >= ws:
            cur = k
        else:
            break
    cur, _catching = _stepped_idx("wipe", (spans[0][0], text), cur)
    if cur < 0:
        return Text(text, style="bold white")

    ws, a, b = spans[cur]
    wlen = b - a
    nxt = spans[cur + 1][0] if cur + 1 < len(spans) else line_end
    est = max(0.15, _estimate_syllables(text[a:b]) * KARAOKE_SECONDS_PER_SYLLABLE)
    dur = _word_dur(ws, nxt, est)
    p = 0.0 if _catching else max(0.0, min(1.0, (sync_pos - ws) / dur))

    t = Text()
    if p >= 1.0:  # word finished: hold colored, no underline until next word
        if b >= len(text.rstrip()):
            return Text(text, style=f"bold {accent_hex}")
        t.append(text[:b], style=f"bold {accent_hex}")
        t.append(text[b:], style="bold white")
        return t
    k = a + min(wlen - 1, int(p * wlen))  # letter being sung right now
    t.append(text[:k], style=f"bold {accent_hex}")
    t.append(text[k], style=f"bold {accent_hex} underline")
    t.append(text[k + 1:], style="bold white")
    return t


# ---- Dux-style soft-edge wipe (port of lyrics_fill.frag + LyricsModel.smoothEdge) ----
KARAOKE_SMOOTH = os.environ.get("SWEEP_SMOOTH", "1") != "0"  # SWEEP_SMOOTH=0 -> old hard cut
KARAOKE_FEATHER = 2.5        # soft edge width, in terminal cells
KARAOKE_SWEEP_WINDOW = 0.24  # seconds; 7-tap triangular time smoothing, looks ahead half a window
KARAOKE_REST_RGB = (255, 255, 255)


def _fill_edge(segs, at, sweep, lead):
    """Edge position (0..1 of line width) at time `at`: furthest point reached by any started
    segment, interpolated linearly from its left x to its right x over its own duration."""
    edge = -lead
    for t, e, x0, x1 in segs:
        if t > at:
            break
        p = min(1.0, (at - t) / (e - t)) if sweep and e > t else 1.0
        frm = x0 if x0 > 0 else -lead
        edge = max(edge, frm + (x1 - frm) * p)
    return edge


def _smooth_edge(segs, at, lead, window, taps=7):
    """Triangular low-pass over a window centred on `at`: turns the jump across a short word
    (or a gap) into a glide, and starts half a window early since cue times are known ahead."""
    acc = tot = 0.0
    for i in range(taps):
        w = min(i + 1, taps - i)
        acc += w * _fill_edge(segs, at + window * (i / (taps - 1) - 0.5), True, lead)
        tot += w
    return acc / tot


def _karaoke_words_smooth(text, word_timings, sync_pos, accent_hex, line_end=None):
    """Same inputs/contract as _karaoke_words, but a per-cell soft-edge wipe: each cell's colour
    is mix(rest, accent, 1 - smoothstep(edge, edge + feather, x)) in true colour, like the Dux
    GLSL fill. Returns None when it can't be used so the caller falls back to _karaoke_words."""
    try:
        fill_rgb = _hex_to_rgb(accent_hex)
    except Exception:
        return None
    spans, pos = [], 0
    for _wt in word_timings:
        wstart, wtext = _wt
        i = text.find(wtext, pos)
        if i < 0:
            return None
        spans.append((wstart, i, i + len(wtext), getattr(_wt, "end", None), getattr(_wt, "syl", None)))
        pos = i + len(wtext)
    total = cell_len(text)
    if not spans or total <= 0:
        return None
    feather = max(0.5, KARAOKE_FEATHER) / total
    segs = []
    for k, (ws, a, b, we, sy) in enumerate(spans):
        nxt = spans[k + 1][0] if k + 1 < len(spans) else line_end
        est = max(0.15, _estimate_syllables(text[a:b]) * KARAOKE_SECONDS_PER_SYLLABLE)
        x0 = 0.0 if k == 0 else cell_len(text[:a]) / total
        x1 = cell_len(text[:b]) / total
        if k + 1 == len(spans) and b >= len(text.rstrip()):
            x1 = 1.0  # last word reaches the end of the line
        if sy:
            # one segment per syllable: real start/end, char range; the edge holds between them
            last_x = x0
            for si, (st0, st1, c0, c1) in enumerate(sy):
                snxt = sy[si + 1][0] if si + 1 < len(sy) else nxt
                if st1 is None:
                    st1 = st0 + _word_dur(st0, snxt, est / len(sy))
                st1 = max(st1, st0 + 0.02)
                sx1 = x1 if si + 1 == len(sy) else cell_len(text[:a + c1]) / total
                segs.append((st0, st1, last_x, sx1))
                last_x = sx1
        else:
            segs.append((ws, ws + _word_dur(ws, nxt, est, we), x0, x1))
    edge = _smooth_edge(segs, sync_pos, feather, KARAOKE_SWEEP_WINDOW)
    if edge <= -feather + 1e-6:
        return Text(text, style="bold white")
    if edge >= 1.0:
        return Text(text, style=f"bold {accent_hex}")
    t, col, run, run_style = Text(), 0, "", None
    for ch in text:
        w = cell_len(ch)
        x = (col + w / 2) / total
        col += w
        u = min(1.0, max(0.0, (x - edge) / feather))
        lit = 1.0 - u * u * (3.0 - 2.0 * u)  # 1 - smoothstep(edge, edge + feather, x)
        r, g, b = (round(KARAOKE_REST_RGB[j] + (fill_rgb[j] - KARAOKE_REST_RGB[j]) * lit) for j in range(3))
        style = f"bold #{r:02x}{g:02x}{b:02x}"
        if style != run_style and run:
            t.append(run, style=run_style)
            run = ""
        run_style = style
        run += ch
    if run:
        t.append(run, style=run_style)
    return t


# 5-row block font for per-word mode ('#' = filled pixel). Variable width.
_BIG_FONT = {
    "A": [" ### ", "#   #", "#####", "#   #", "#   #"],
    "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
    "C": [" ####", "#    ", "#    ", "#    ", " ####"],
    "D": ["#### ", "#   #", "#   #", "#   #", "#### "],
    "E": ["#####", "#    ", "#### ", "#    ", "#####"],
    "F": ["#####", "#    ", "#### ", "#    ", "#    "],
    "G": [" ####", "#    ", "#  ##", "#   #", " ####"],
    "H": ["#   #", "#   #", "#####", "#   #", "#   #"],
    "I": ["#####", "  #  ", "  #  ", "  #  ", "#####"],
    "J": ["#####", "   # ", "   # ", "#  # ", " ##  "],
    "K": ["#   #", "#  # ", "###  ", "#  # ", "#   #"],
    "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
    "Q": [" ### ", "#   #", "# # #", "#  # ", " ## #"],
    "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
    "S": [" ####", "#    ", " ### ", "    #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
    "U": ["#   #", "#   #", "#   #", "#   #", " ### "],
    "V": ["#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
    "X": ["#   #", " # # ", "  #  ", " # # ", "#   #"],
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
    "Z": ["#####", "   # ", "  #  ", " #   ", "#####"],
    "0": [" ### ", "#  ##", "# # #", "##  #", " ### "],
    "1": [" ##  ", "  #  ", "  #  ", "  #  ", "#####"],
    "2": [" ### ", "#   #", "  ## ", " #   ", "#####"],
    "3": ["#### ", "    #", " ### ", "    #", "#### "],
    "4": ["#  # ", "#  # ", "#####", "   # ", "   # "],
    "5": ["#####", "#    ", "#### ", "    #", "#### "],
    "6": [" ### ", "#    ", "#### ", "#   #", " ### "],
    "7": ["#####", "   # ", "  #  ", " #   ", " #   "],
    "8": [" ### ", "#   #", " ### ", "#   #", " ### "],
    "9": [" ### ", "#   #", " ####", "    #", " ### "],
    ".": [" ", " ", " ", " ", "#"],
    ",": ["  ", "  ", "  ", " #", "# "],
    "'": ["#", "#", " ", " ", " "],
    "!": ["#", "#", "#", " ", "#"],
    "?": [" ### ", "#   #", "  ## ", "     ", "  #  "],
    "-": ["   ", "   ", "###", "   ", "   "],
    "(": ["  #", " # ", " # ", " # ", "  #"],
    ")": ["#  ", " # ", " # ", " # ", "#  "],
}


_WORD_SIZE_NAMES = ("small", "medium", "large")
_WORD_STYLE_NAMES = ("dots", "solid", "text", "sextant", "scaled", "image", "ascii")
# dots per font pixel (dots/solid/sextant), kitty scale (scaled), cell rows (image)
_WORD_SIZE_DOTS = {
    0: (3, 5, 8),  # dots (braille, 2x4 dots per cell)
    1: (2, 3, 4),  # solid (half-blocks, 1x2 dots per cell)
    3: (2, 3, 5),  # sextant (2x3 dots per cell)
    4: (2, 3, 4),  # scaled (kitty OSC 66, scale factor)
    5: (4, 6, 9),  # image (kitty graphics, cell rows)
    6: (1, 2, 3),  # ascii (plain '#', see _ASCII_SCALE)
}
# ascii style: size step -> (cols per font pixel, rows per font pixel).
# Cells are ~1:2 (w:h), so 2 cols ≈ 1 row keeps the letters proportional.
_ASCII_SCALE = {0: (1, 1), 1: (2, 1), 2: (3, 1), 3: (4, 2)}


_WORD_GRAD_NAMES = ("off", "letter", "column", "wipe")
_WIPE_REST = (235, 235, 235)  # un-sung part of the word in wipe mode
# 'image' style tuning (kitty graphics)
_IMG_REST = (190, 195, 200)     # un-sung part in wipe mode (softer than _WIPE_REST)
_IMG_GLOW_RADIUS = 0.26         # blur radius, in cell heights (was 0.35)
_IMG_GLOW_GAIN = 0.95           # glow strength (was 1.6)
_IMG_FILL_LIGHTEN = 0.0         # mix toward white for the letter fill (was 0.25)
_IMG_SAT_MIN = 0.50             # minimum saturation for the accent colour


def _punch_rgb(c, smin=_IMG_SAT_MIN):
    """Pushes a colour to full brightness and at least `smin` saturation,
    keeping its hue. Near-grey colours are left alone (no invented hue)."""
    h, sat, v = colorsys.rgb_to_hsv(*(x / 255.0 for x in c))
    if sat < 0.12:
        return tuple(c)
    r, g, b = colorsys.hsv_to_rgb(h, max(sat, smin), 1.0)
    return (int(r * 255), int(g * 255), int(b * 255))


def _lerp_rgb(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _grad_end(rgb):
    """Second gradient colour: same accent, hue pushed ~50° round the wheel."""
    h, sat, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb((h + 50 / 360.0) % 1.0, max(sat, 0.45), max(v, 0.85))
    return (int(r * 255), int(g * 255), int(b * 255))


_WIPE_SOFT = os.environ.get("SWEEP_WORD_WIPE_SOFT", "1") != "0"
_WIPE_FEATHER_COLS = 2.5      # soft edge width for dots/solid/sextant/ascii, in columns
_WIPE_FEATHER_LETTERS = 1.2   # soft edge width for scaled, in letters


def _wipe_color(i, n, p, a, feather_units):
    """Wipe colour of cell i of n: mix(rest, a, 1 - smoothstep(edge, edge + f, x))."""
    p = max(0.0, min(1.0, p))
    if not _WIPE_SOFT or n <= 0:
        return a if i < p * n else _WIPE_REST
    f = max(feather_units / n, 1e-6)
    edge = -f + p * (1.0 + f)  # p=0 -> everything rest, p=1 -> everything lit
    x = (i + 0.5) / n
    u = min(1.0, max(0.0, (x - edge) / f))
    lit = 1.0 - u * u * (3.0 - 2.0 * u)
    return _lerp_rgb(_WIPE_REST, a, lit)


def _letter_colors(n, mode, p, a, b):
    """Per-letter colours for styles that can only colour whole letters."""
    out = []
    for i in range(n):
        if mode == 3:  # wipe
            out.append(_wipe_color(i, n, p, a, _WIPE_FEATHER_LETTERS))
        else:
            out.append(_lerp_rgb(a, b, i / (n - 1) if n > 1 else 0.0))
    return out


def _color_rows(rows, letters, mode, p, a, b):
    """Turns equal-width string rows into coloured Text rows.
    mode 1 letter · 2 column · 3 wipe. `letters[c]` = letter index at column c."""
    if not rows:
        return []
    cw = len(rows[0])
    n = (max(letters) + 1) if letters else 1
    cols = []
    for c in range(cw):
        if mode == 3:
            cols.append(_wipe_color(c, cw, p, a, _WIPE_FEATHER_COLS))
        elif mode == 1 and letters and c < len(letters):
            cols.append(_lerp_rgb(a, b, letters[c] / (n - 1) if n > 1 else 0.0))
        else:
            cols.append(_lerp_rgb(a, b, c / (cw - 1) if cw > 1 else 0.0))
    out = []
    for row in rows:
        t = Text()
        i = 0
        while i < cw:
            j = i
            while j < cw and cols[j] == cols[i]:
                j += 1
            r, g, bl = cols[i]
            t.append(row[i:j], style=f"bold rgb({r},{g},{bl})")
            i = j
        out.append(t)
    return out


def _sextant_char(b):
    """bits: TL=1 TR=2 ML=4 MR=8 BL=16 BR=32 → Unicode sextant glyph."""
    if b == 0:
        return " "
    if b == 63:
        return "█"
    if b == 21:
        return "▌"
    if b == 42:
        return "▐"
    return chr(0x1FB00 + b - 1 - (b > 21) - (b > 42))


def _ascii_fit(w, max_w, max_h, max_s, min_s=0):
    """Plain-ASCII ('#') block word on ONE line at the biggest scale <= max_s
    that fits. Returns (rows, letter index per column) or None."""
    for sc in range(max(0, min(3, int(max_s))), min_s - 1, -1):
        sx, sy = _ASCII_SCALE[sc]
        gap = max(1, sx // 2)
        grid = [[] for _ in range(5 * sy)]
        starts = []
        for ci, c in enumerate(w):
            g = _BIG_FONT[c]
            starts.append(len(grid[0]))
            for x in range(len(g[0])):
                for r in range(5):
                    ch = "#" if g[r][x] == "#" else " "
                    for k in range(sy):
                        grid[r * sy + k].extend(ch * sx)
            if ci < len(w) - 1:
                for row in grid:
                    row.extend(" " * gap)
        cw, ch_n = len(grid[0]), len(grid)
        if cw > max_w or ch_n > max_h:
            continue
        lets = [max(i for i, st in enumerate(starts) if st <= c) for c in range(cw)]
        return ["".join(r) for r in grid], lets
    return None


def _big_word_rows(word, max_w, max_h, max_s=5, style=0, letters_out=None):
    """Renders `word` as big block-font art, biggest dot scale <= max_s that
    fits max_w x max_h terminal cells.
      style 0 → braille dots  ·  1 → solid half-blocks (▀▄█)
      style 3 → sextants (2x3 dots per cell)
      style 6 → plain ASCII ('#'), works in any terminal/font
    Returns equal-width strings, or None (→ caller falls back to plain text)."""
    import unicodedata
    w = unicodedata.normalize("NFKD", word.upper())
    w = "".join(c for c in w if c in _BIG_FONT)
    if not w:
        return None
    if style == 6:
        res = _ascii_fit(w, max_w, max_h, max_s)
        if res is None:
            # too long for one line even at the smallest scale → stack 2-3 parts
            for parts in (2, 3):
                if parts > len(w) or parts * 5 + (parts - 1) > max_h:
                    break
                step = -(-len(w) // parts)
                chunks = [w[i:i + step] for i in range(0, len(w), step)]
                subs = None
                for sc in range(max(0, min(3, int(max_s))), -1, -1):  # same scale for every part
                    cand = [_ascii_fit(c, max_w, max_h, sc, sc) for c in chunks]
                    if all(x is not None for x in cand):
                        subs = cand
                        break
                if subs is None:
                    continue
                cw = max(len(x[0][0]) for x in subs)
                rows, lets = [], None
                for si, (r, lt) in enumerate(subs):
                    if si:
                        rows.append(" " * cw)
                    pad = (cw - len(r[0])) // 2
                    rows.extend(" " * pad + row + " " * (cw - pad - len(row)) for row in r)
                    if lets is None or len(lt) + 2 * pad > len(lets):
                        lets = [0] * pad + lt + [lt[-1]] * (cw - pad - len(lt))
                res = (rows, lets)
                break
        if res is None:
            return None
        if letters_out is not None:
            letters_out[:] = res[1]
        return res[0]
    lo = 1 if style == 1 else 2
    for sc in range(max(lo, int(max_s)), lo - 1, -1):
        sy = sc
        sx = round(sc * 4 / 3) if style == 3 else sc  # sextant dots are taller than wide
        H = 5 * sy
        cols, starts = [], []
        for ci, c in enumerate(w):
            g = _BIG_FONT[c]
            starts.append(len(cols))
            for x in range(len(g[0])):
                col = [1 if g[r][x] == "#" else 0 for r in range(5) for _ in range(sy)]
                cols.extend([col] * sx)
            if ci < len(w) - 1:
                cols.extend([[0] * H for _ in range(sx)])
        W = len(cols)
        if style == 1:
            cw, ch = W, (H + 1) // 2
        elif style == 3:
            cw, ch = (W + 1) // 2, (H + 2) // 3
        else:
            cw, ch = (W + 1) // 2, (H + 3) // 4
        if cw > max_w or ch > max_h:
            continue

        def dot(x, y):
            return cols[x][y] if x < W and y < H else 0

        rows = []
        for cy in range(ch):
            line = []
            if style == 1:
                for x in range(cw):
                    t, b = dot(x, cy * 2), dot(x, cy * 2 + 1)
                    line.append(" ▀▄█"[t | b << 1])
            elif style == 3:
                for cx in range(cw):
                    x, y = cx * 2, cy * 3
                    bits = (dot(x, y) | dot(x + 1, y) << 1
                            | dot(x, y + 1) << 2 | dot(x + 1, y + 1) << 3
                            | dot(x, y + 2) << 4 | dot(x + 1, y + 2) << 5)
                    line.append(_sextant_char(bits))
            else:
                for cx in range(cw):
                    x, y = cx * 2, cy * 4
                    bits = (dot(x, y) | dot(x, y + 1) << 1 | dot(x, y + 2) << 2
                            | dot(x + 1, y) << 3 | dot(x + 1, y + 1) << 4
                            | dot(x + 1, y + 2) << 5 | dot(x, y + 3) << 6
                            | dot(x + 1, y + 3) << 7)
                    line.append(chr(0x2800 + bits) if bits else " ")
            rows.append("".join(line))
        if letters_out is not None:
            dpc = 1 if style == 1 else 2  # dots per cell, horizontally
            letters_out[:] = [max(i for i, st in enumerate(starts) if st <= c * dpc)
                              for c in range(cw)]
        return rows
    return None


# ---------------------------------------------------------- kitty-only styles
_KITTY_VER_CACHE = []


def _kitty_version():
    """(major, minor) of the running kitty, or (0, 0) if unknown. Cached."""
    if not _KITTY_VER_CACHE:
        ver = (0, 0)
        try:
            out = subprocess.run(["kitty", "--version"], capture_output=True,
                                 text=True, timeout=2).stdout
            m = re.search(r"(\d+)\.(\d+)", out)
            if m:
                ver = (int(m.group(1)), int(m.group(2)))
        except Exception:
            pass
        _KITTY_VER_CACHE.append(ver)
    return _KITTY_VER_CACHE[0]


def _effective_word_style(style):
    """Falls back to 1 (solid) when the chosen style can't work here:
    scaled needs kitty >= 0.40 (OSC 66); image needs kitty + Pillow +
    truecolor + no tmux (unicode placeholders)."""
    if style == 4:
        if IN_KITTY and not os.environ.get("TMUX") and _kitty_version() >= (0, 40):
            return 4
        return 1
    if style == 5:
        if (IN_KITTY and HAVE_PIL and not os.environ.get("TMUX")
                and os.environ.get("COLORTERM") in ("truecolor", "24bit")
                and sys.stdout.isatty()):
            return 5
        return 1
    return style


def _raw_tty_write(data):
    """Writes straight to the tty fd — Live redirects sys.stdout, and these
    escape sequences must not go through Rich as printable text."""
    try:
        fd = sys.__stdout__.fileno()
        buf = data.encode("utf-8") if isinstance(data, str) else data
        while buf:
            n = os.write(fd, buf)
            buf = buf[n:]
    except Exception:
        pass


class _Osc66Row:
    """Bottom row of a kitty text-sizing (OSC 66) word block.

    The word is s cells tall, so rows 1..s-1 are plain spaces (handled by the
    caller) and this last row writes its spaces, jumps back up, draws the
    scaled text over them, then returns the cursor to where Rich expects it.
    That way every frame redraw keeps Rich's width accounting correct and
    the glyph is re-drawn after the spaces that would otherwise erase it.
    `colors` = one rgb per character (letters can each have their own)."""

    def __init__(self, text, scale, colors):
        self.text, self.scale, self.colors = text, scale, colors
        self.cells = scale * sum(cell_len(c) for c in text)

    def __rich_measure__(self, console, options):
        return Measurement(self.cells, self.cells)

    def __rich_console__(self, console, options):
        s, n = self.scale, self.cells
        if len(set(self.colors)) <= 1:
            r, g, b = self.colors[0]
            body = f"\x1b[1;38;2;{r};{g};{b}m\x1b]66;s={s};{self.text}\x1b\\"
        else:
            body = "".join(
                f"\x1b[1;38;2;{r};{g};{b}m\x1b]66;s={s};{ch}\x1b\\"
                for ch, (r, g, b) in zip(self.text, self.colors))
        seq = f"\x1b[{s - 1}A\x1b[{n}D{body}\x1b[{s - 1}B\x1b[0m"
        yield Segment(" " * n)
        yield Segment(seq, None, [(ControlType.BELL,)])


def _osc66_word_rows(word, max_w, max_h, size_idx, rgb, mode=0, p=0.0, rgb2=None):
    """Rows for the 'scaled' style: list of renderables, one per cell row,
    or None if nothing renderable / doesn't fit. Gradients are per letter."""
    text = "".join(c for c in word.upper()
                   if c.isprintable() and not c.isspace() and cell_len(c) == 1)
    if not text:
        return None
    colors = (_letter_colors(len(text), mode, p, rgb, rgb2 or rgb) if mode
              else [rgb] * len(text))
    top = _WORD_SIZE_DOTS[4][max(0, min(2, size_idx))]
    for sc in range(top, 1, -1):
        if sc * len(text) <= max_w and sc <= max_h:
            blank = Text(" " * (sc * len(text)))
            return [blank] * (sc - 1) + [_Osc66Row(text, sc, colors)]
    return None


# Kitty unicode-placeholder row/column diacritics (first 160 of kitty's list)
_PH_DIAC = [chr(int(h, 16)) for h in (
    "305 30D 30E 310 312 33D 33E 33F 346 34A 34B 34C 350 351 352 357 35B 363 364 365 366 367 368 369 36A 36B 36C 36D 36E 36F 483 484 485 486 487 592 593 594 595 597 598 599 59C 59D 59E 59F 5A0 5A1 5A8 5A9 5AB 5AC 5AF 5C4 610 611 612 613 614 615 616 617 657 658 659 65A 65B 65D 65E 6D6 6D7 6D8 6D9 6DA 6DB 6DC 6DF 6E0 6E1 6E2 6E4 6E7 6E8 6EB 6EC 730 732 733 735 736 73A 73D 73F 740 741 743 745 747 749 74A 7EB 7EC 7ED 7EE 7EF 7F0 7F1 7F3 816 817 818 819 81B 81C 81D 81E 81F 820 821 822 823 825 826 827 829 82A 82B 82C 82D 951 953 954 F82 F83 F86 F87 135D 135E 135F 17DD 193A 1A17 1A75 1A76 1A77 1A78 1A79 1A7A 1A7B 1A7C 1B6B 1B6D 1B6E 1B6F 1B70 1B71 1B72 1B73 1CD0 1CD1").split()]
_WORD_IMG_CACHE = {}   # key -> dict(id, png, cols, rows, lines, ts)
_WORD_IMG_NEXT_ID = [0x7A1000]
_WORD_FONT_PATH = []


def _term_cell_px():
    try:
        import fcntl, struct, termios
        r, c, xp, yp = struct.unpack(
            "HHHH", fcntl.ioctl(sys.__stdout__.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        if r and c and xp and yp:
            return max(4, xp // c), max(8, yp // r)
    except Exception:
        pass
    return 10, 20


def _word_font_path():
    if not _WORD_FONT_PATH:
        path = ""
        try:
            path = subprocess.run(["fc-match", "-f", "%{file}", "sans-serif:bold"],
                                  capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            pass
        if not path or not os.path.exists(path):
            for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                         "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                         "/usr/share/fonts/noto/NotoSans-Bold.ttf"):
                if os.path.exists(cand):
                    path = cand
                    break
        _WORD_FONT_PATH.append(path or "")
    return _WORD_FONT_PATH[0] or None


def _render_word_png(word, rgb, cols_max, rows_cap, rows_target, cw, ch,
                     imode=0, pq=0, rgb2=None):
    """Bold word with soft glow → PNG bytes sized to a whole number of cells.
    imode 0 flat · 1 left→right gradient rgb→rgb2 · 3 wipe (pq/10 of the
    width in rgb, the rest near-white)."""
    import io
    from PIL import ImageDraw, ImageFilter, ImageFont
    text = word.upper()
    pad = int(ch * 0.7)
    size = max(12, int(rows_target * ch * 0.66))
    path = _word_font_path()
    while True:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        l, t, r, b = font.getbbox(text)
        tw, th = r - l, b - t
        fits = (tw + 2 * pad <= cols_max * cw) and (th + 2 * pad <= rows_cap * ch)
        if fits or size <= 12:
            break
        size = int(size * 0.9)
    cols = min(cols_max, max(2, -(-(tw + 2 * pad) // cw)))
    rows = min(rows_cap, max(2, -(-(th + 2 * pad) // ch)))
    W, H = cols * cw, rows * ch
    x, y = (W - tw) // 2 - l, (H - th) // 2 - t
    x0 = x + l
    rgb2 = _punch_rgb(rgb2 or rgb)
    rgb = _punch_rgb(rgb)

    def lighten(c):
        return tuple(min(255, int(v + (255 - v) * _IMG_FILL_LIGHTEN)) for v in c)

    sw = max(1, size // 40)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).text((x, y), text, font=font, fill=255,
                              stroke_width=sw, stroke_fill=255)
    # Real ink extent (stroke included). font.getbbox() ignores the stroke, so
    # letter edges (e.g. the foot of an "A") poke outside it and used to get
    # the "not sung yet" colour even at 100% wipe.
    ink = mask.getbbox()
    ink_l, ink_w = (ink[0], max(1, ink[2] - ink[0])) if ink else (x0, max(1, tw))

    def strip(ca, cb):
        """W x H colour field for the chosen gradient mode."""
        px = []
        for xx in range(W):
            tt = (xx - ink_l) / float(ink_w)
            if imode == 3:
                px.append(ca if (pq >= 10 or tt < pq / 10.0) else cb)
            elif imode == 1:
                px.append(_lerp_rgb(ca, cb, max(0.0, min(1.0, tt))))
            else:
                px.append(ca)
        img = Image.new("RGB", (W, 1))
        img.putdata(px)
        return img.resize((W, H), Image.NEAREST).convert("RGBA")

    gm = Image.new("L", (W, H), 0)
    ImageDraw.Draw(gm).text((x, y), text, font=font, fill=255)
    glow_a = gm.filter(ImageFilter.GaussianBlur(max(2, int(ch * _IMG_GLOW_RADIUS))))
    glow_a = glow_a.point(lambda v: min(255, int(v * _IMG_GLOW_GAIN)))
    rest = _IMG_REST
    glow = strip(rgb, rest if imode == 3 else rgb2)
    glow.putalpha(glow_a)
    fill = strip(lighten(rgb), lighten(rest) if imode == 3 else lighten(rgb2))
    fill.putalpha(mask)
    buf = io.BytesIO()
    Image.alpha_composite(glow, fill).save(buf, "PNG")
    return buf.getvalue(), cols, rows


def _kitty_send_png(img_id, png, cols, rows):
    """Transmit as a virtual placement (U=1) so unicode placeholders show it."""
    import base64
    data = base64.standard_b64encode(png).decode()
    chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [""]
    out = []
    for i, part in enumerate(chunks):
        m = 1 if i < len(chunks) - 1 else 0
        if i == 0:
            out.append(f"\x1b_Ga=T,f=100,t=d,i={img_id},U=1,c={cols},r={rows},q=2,m={m};{part}\x1b\\")
        else:
            out.append(f"\x1b_Gm={m};{part}\x1b\\")
    _raw_tty_write("".join(out))


_IMG_SMOOTH = os.environ.get("SWEEP_IMG_SMOOTH", "1") != "0"
_IMG_WIPE_FEATHER_CH = 0.8   # soft edge width, in cell heights
_IMG_WIPE_STEP_PX = 2        # edge quantisation; every new step is one PNG sent to kitty
_IMG_WIPE_MAX_FRAMES = 120   # PNGs kept per word
_IMG_WIPE_MAX_WORDS = 8
_WORD_SMOOTH_CACHE = {}


def _wipe_frame_png(rest, lit, edge, feather):
    """rest/lit blended with 1 - smoothstep(edge, edge + feather, x) per pixel column."""
    import io
    W, H = rest.size
    row = bytearray(W)
    for x in range(W):
        u = (x + 0.5 - edge) / feather
        if u <= 0.0:
            row[x] = 255
        elif u >= 1.0:
            break
        else:
            row[x] = int((1.0 - u * u * (3.0 - 2.0 * u)) * 255 + 0.5)
    mask = Image.frombytes("L", (W, 1), bytes(row)).resize((W, H), Image.NEAREST)
    buf = io.BytesIO()
    Image.composite(lit, rest, mask).save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def _word_image_rows_smooth(word, rgb, max_w, max_h, size_idx, p):
    """Continuous wipe version of _word_image_rows(mode=3). None = use the old path."""
    import io
    cw, ch = _term_cell_px()
    rows_target = _WORD_SIZE_DOTS[5][max(0, min(2, size_idx))]
    cols_max = min(max_w, len(_PH_DIAC))
    rows_cap = max(2, min(max_h, len(_PH_DIAC)))
    rgb = tuple((c >> 3) << 3 for c in rgb)
    key = (word.upper(), rgb, rows_target, cols_max, rows_cap, cw, ch)
    now = time.time()
    ent = _WORD_SMOOTH_CACHE.get(key)
    try:
        if ent is None:
            rest_png, cols, rows = _render_word_png(word, rgb, cols_max, rows_cap,
                                                    rows_target, cw, ch, 3, 0, rgb)
            lit_png, _c, _r = _render_word_png(word, rgb, cols_max, rows_cap,
                                               rows_target, cw, ch, 3, 10, rgb)
            rest = Image.open(io.BytesIO(rest_png)).convert("RGBA")
            lit = Image.open(io.BytesIO(lit_png)).convert("RGBA")
            core = lit.getchannel("A").point(lambda v: 255 if v >= 250 else 0).getbbox()
            ink = (core[0], core[2]) if core else (0, rest.size[0])
            ids = []
            for _ in range(2):
                img_id = _WORD_IMG_NEXT_ID[0]
                _WORD_IMG_NEXT_ID[0] = 0x7A1000 + ((img_id - 0x7A1000 + 1) % 4000)
                ids.append(img_id)
            ph = chr(0x10EEEE)

            def lines_for(img_id):
                style = f"rgb({(img_id >> 16) & 255},{(img_id >> 8) & 255},{img_id & 255})"
                return [Text("".join(ph + _PH_DIAC[r] + _PH_DIAC[c] for c in range(cols)),
                             style=style) for r in range(rows)]

            ent = {"rest": rest, "lit": lit, "ink": ink, "cols": cols, "rows": rows,
                   "ids": ids, "lines": [lines_for(i) for i in ids], "flip": 0,
                   "frames": {}, "cur": None, "used": 0.0}
            _WORD_SMOOTH_CACHE[key] = ent
            if len(_WORD_SMOOTH_CACHE) > _IMG_WIPE_MAX_WORDS:
                old_key = min((k for k in _WORD_SMOOTH_CACHE if k != key),
                              key=lambda k: _WORD_SMOOTH_CACHE[k]["used"])
                old = _WORD_SMOOTH_CACHE.pop(old_key)
                for old_id in old["ids"]:
                    _raw_tty_write(f"\x1b_Ga=d,d=I,i={old_id},q=2\x1b\\")
        gap = now - ent["used"]
        ent["used"] = now
        feather = max(4.0, ch * _IMG_WIPE_FEATHER_CH)
        ink_l, ink_r = ent["ink"]
        p = max(0.0, min(1.0, p))
        edge = ink_l - feather + p * ((ink_r - ink_l) + feather)
        q = int(edge // _IMG_WIPE_STEP_PX)
        if q != ent["cur"] or gap > 0.6:  # a gap means kitty may have dropped its images
            png = ent["frames"].get(q)
            if png is None:
                png = _wipe_frame_png(ent["rest"], ent["lit"], q * _IMG_WIPE_STEP_PX, feather)
                if len(ent["frames"]) >= _IMG_WIPE_MAX_FRAMES:
                    ent["frames"].clear()
                ent["frames"][q] = png
            if q != ent["cur"]:
                ent["flip"] ^= 1
            _kitty_send_png(ent["ids"][ent["flip"]], png, ent["cols"], ent["rows"])
            ent["cur"] = q
        return list(ent["lines"][ent["flip"]])
    except Exception:
        return None


def _word_image_rows(word, rgb, max_w, max_h, size_idx, mode=0, p=0.0, rgb2=None):
    """Rows of kitty unicode placeholders that display a rendered image of
    `word`. Images are cached per word; an entry unused for >0.6s is re-sent
    (leaving the alt screen — settings, search… — drops kitty's image store)."""
    if mode == 3 and _IMG_SMOOTH:
        _smooth_rows = _word_image_rows_smooth(word, rgb, max_w, max_h, size_idx, p)
        if _smooth_rows is not None:
            return _smooth_rows
    cw, ch = _term_cell_px()
    rows_target = _WORD_SIZE_DOTS[5][max(0, min(2, size_idx))]
    cols_max = min(max_w, len(_PH_DIAC))
    rows_cap = max(2, min(max_h, len(_PH_DIAC)))
    rgb = tuple((c >> 3) << 3 for c in rgb)
    # image supports flat / gradient / wipe; letter+column both = gradient
    imode = 0 if not mode else (3 if mode == 3 else 1)
    pq = int(round(max(0.0, min(1.0, p)) * 10)) if imode == 3 else 0
    rgb2 = tuple((c >> 3) << 3 for c in (rgb2 or rgb)) if imode == 1 else rgb
    key = (word.upper(), rgb, rows_target, cols_max, rows_cap, cw, ch, imode, pq, rgb2)
    now = time.time()
    ent = _WORD_IMG_CACHE.get(key)
    try:
        if ent is None:
            png, cols, rows = _render_word_png(word, rgb, cols_max, rows_cap,
                                               rows_target, cw, ch, imode, pq, rgb2)
            img_id = _WORD_IMG_NEXT_ID[0]
            _WORD_IMG_NEXT_ID[0] = 0x7A1000 + ((img_id - 0x7A1000 + 1) % 4000)
            style = f"rgb({(img_id >> 16) & 255},{(img_id >> 8) & 255},{img_id & 255})"
            ph = chr(0x10EEEE)
            lines = [Text("".join(ph + _PH_DIAC[r] + _PH_DIAC[c] for c in range(cols)),
                          style=style) for r in range(rows)]
            ent = {"id": img_id, "png": png, "cols": cols, "rows": rows,
                   "lines": lines, "ts": 0.0}
            _WORD_IMG_CACHE[key] = ent
            if len(_WORD_IMG_CACHE) > 48:
                old_key = min((k for k in _WORD_IMG_CACHE if k != key),
                              key=lambda k: _WORD_IMG_CACHE[k]["ts"])
                old = _WORD_IMG_CACHE.pop(old_key)
                _raw_tty_write(f"\x1b_Ga=d,d=I,i={old['id']},q=2\x1b\\")
        if now - ent["ts"] > 0.6:
            _kitty_send_png(ent["id"], ent["png"], ent["cols"], ent["rows"])
        ent["ts"] = now
        return list(ent["lines"])
    except Exception:
        return None


def _syl_progress(syl, sync_pos, nxt, est):
    """Word progress (0..1, by characters) along the real syllable timing. It holds between
    syllables and on held notes, so the big-word wipe stops where the singer does."""
    total = syl[-1][3] or 1
    x = 0.0
    for k, (t0, t1, c0, c1) in enumerate(syl):
        if sync_pos < t0:
            break
        if t1 is None:
            snxt = syl[k + 1][0] if k + 1 < len(syl) else nxt
            t1 = t0 + _word_dur(t0, snxt, est / len(syl))
        t1 = max(t1, t0 + 0.02)
        if sync_pos >= t1:
            x = float(c1)
            continue
        x = c0 + (c1 - c0) * (sync_pos - t0) / (t1 - t0)
        break
    return max(0.0, min(1.0, x / total))


def _current_word(text, frac, word_timings=None, sync_pos=None, line_end=None):
    """Returns (word, index, total, progress) for the word being sung right now.
    progress is 0..1 through that word (drives per-word wipe).

    Uses real Enhanced-LRC word starts when available, otherwise the same
    syllable-weighted estimate karaoke_line() uses. None if line has no words."""
    words = text.split()
    if not words:
        return None
    if word_timings and sync_pos is not None:
        spans, pos, ok, ends, syls = [], 0, True, [], []
        for _wt in word_timings:
            wstart, wtext = _wt
            i = text.find(wtext, pos)
            if i < 0:
                ok = False
                break
            spans.append((wstart, wtext.strip()))
            ends.append(getattr(_wt, "end", None))
            syls.append(getattr(_wt, "syl", None))
            pos = i + len(wtext)
        if ok and spans:
            cur = 0
            for k, (ws, _) in enumerate(spans):
                if sync_pos >= ws:
                    cur = k
                else:
                    break
            cur, _catching = _stepped_idx("word", (spans[0][0], text), cur)
            ws, wtxt = spans[cur]
            nxt = spans[cur + 1][0] if cur + 1 < len(spans) else line_end
            est = max(0.15, _estimate_syllables(wtxt) * KARAOKE_SECONDS_PER_SYLLABLE)
            dur = _word_dur(ws, nxt, est, ends[cur])
            if syls[cur]:
                prog = 0.0 if _catching else _syl_progress(syls[cur], sync_pos, nxt, est)
            else:
                prog = 0.0 if _catching else max(0.0, min(1.0, (sync_pos - ws) / dur))
            return (wtxt or words[0], cur, len(spans), prog)
    weights = _char_weights(text)
    eased = frac + (_ease_wipe(frac) - frac) * KARAOKE_EASE
    target = eased * (sum(weights) or 1.0)
    cum, cut = 0.0, len(text) - 1
    for i, w in enumerate(weights):
        cum += w
        if cum >= target:
            cut = i
            break
    wspans = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    k = 0
    for ms, me in wspans:
        if me <= cut:
            k += 1
    k = min(k, len(words) - 1)
    k, _catching = _stepped_idx("word_est", text, k)
    ms, me = wspans[k]
    wweight = sum(weights[ms:me]) or 1.0
    prog = 0.0 if _catching else max(0.0, min(1.0, (target - sum(weights[:ms])) / wweight))
    return (words[k], k, len(words), prog)


def karaoke_line(text, frac, accent_hex, word_timings=None, sync_pos=None,
                 line_end=None):
    """Sweeps a line's color left-to-right.

    When `word_timings` (list of (word_start_seconds, word_text)) and
    `sync_pos` are both provided, the cut is driven by real word onsets
    from Enhanced LRC — no syllable guessing, no easing curve. Words
    whose start time has already passed turn accent; the current word
    is underlined at the cut.

    Otherwise falls back to the existing estimate: syllable-weighted
    character split + optional smoothstep ease (KARAOKE_EASE).

    Always a hard two-tone cut (accent | white), never a gradient band.
    """
    n = len(text)
    if n == 0:
        return Text("")

    # --- word-level path (Enhanced LRC) ---------------------------------
    if word_timings and sync_pos is not None and len(word_timings) > 0:
        res = (_karaoke_words_smooth(text, word_timings, sync_pos, accent_hex, line_end)
               if KARAOKE_SMOOTH else None)
        if res is None:
            res = _karaoke_words(text, word_timings, sync_pos, accent_hex, line_end)
        if res is not None:
            return res

    # --- estimated path (syllable weights + ease) -----------------------
    weights = _char_weights(text)
    eased_frac = frac + (_ease_wipe(frac) - frac) * KARAOKE_EASE
    total_w = sum(weights) or 1.0
    target = eased_frac * total_w
    cum = 0.0
    cut = n
    for i, w in enumerate(weights):
        cum += w
        if cum >= target:
            cut = i
            break
    cut = max(0, min(n, cut))

    # Soft forward-only snap: if the cut is already very near the end of
    # the current word (<15% weight left), jump to the word boundary so
    # the wipe doesn't linger on the last 1-2 letters. Threshold is tight
    # on purpose — a looser one caused visible maju-mundur when frac
    # oscillated around the cutoff between frames.
    if 0 < cut < n and not text[cut].isspace():
        word_end = cut
        while word_end < n and not text[word_end].isspace():
            word_end += 1
        word_start = cut
        while word_start > 0 and not text[word_start - 1].isspace():
            word_start -= 1
        word_w = sum(weights[word_start:word_end]) or 1.0
        remain_w = sum(weights[cut:word_end])
        if remain_w / word_w < 0.15:
            cut = word_end
            if cut < n and text[cut].isspace():
                cut += 1

    if cut <= 0:
        return Text(text, style="bold white")
    if cut >= n:
        return Text(text, style=f"bold {accent_hex}")

    hi = _snap_cut_off_whitespace(text, cut, n)
    t = Text()
    if hi <= cut:
        t.append(text[:hi], style=f"bold {accent_hex}")
        t.append(text[hi], style=f"bold {accent_hex} underline")
        t.append(text[hi + 1:cut], style=f"bold {accent_hex}")
        t.append(text[max(hi + 1, cut):], style="bold white")
    else:
        t.append(text[:cut], style=f"bold {accent_hex}")
        t.append(text[cut:hi], style="bold white")
        t.append(text[hi], style=f"bold {accent_hex} underline")
        t.append(text[hi + 1:], style="bold white")
    return t


def _bait_bounds(entries, idx, max_lines=8):
    """[start, end] of the stanza (bait) that contains entry `idx`.

    A boundary sits next to a blank LRC row, or where the gap between two
    lines is well above this track's usual spacing. A stanza longer than
    `max_lines` is split into even pages so it always fits the panel."""
    n = len(entries)
    if n == 0:
        return 0, -1
    idx = max(0, min(n - 1, idx))

    def blank(k):
        return not (entries[k][1] or "").strip()

    if blank(idx):  # instrumental gap: keep the last stanza on screen
        k = idx
        while k > 0 and blank(k):
            k -= 1
        idx = k
    gaps = sorted(
        entries[k + 1][0] - entries[k][0]
        for k in range(n - 1)
        if not blank(k) and not blank(k + 1) and entries[k + 1][0] > entries[k][0]
    )
    median = gaps[len(gaps) // 2] if gaps else 4.0
    gap_limit = max(6.0, median * 2.2)

    def boundary(k):  # between entry k and k + 1
        if blank(k) or blank(k + 1):
            return True
        return entries[k + 1][0] - entries[k][0] > gap_limit

    s = idx
    while s > 0 and not boundary(s - 1):
        s -= 1
    e = idx
    while e < n - 1 and not boundary(e):
        e += 1
    count = e - s + 1
    if count > max_lines:
        pages = -(-count // max_lines)
        size = -(-count // pages)
        page = (idx - s) // size
        s, e = s + page * size, min(e, s + (page + 1) * size - 1)
    return s, e


def render_frame(artist, title, entries, pos, duration, status, width, height,
                  zoom_hint=True, shazam_hint=False, shazam_active=False,
                  cover_offset=0, reidentify_soon=False, auto_paused=False,
                  plain_lyrics="", karaoke_enabled=True, sync_offset=0.0,
                  translations=None, translate_enabled=False,
                  romanized=None, romanize_enabled=False,
                  debug_karaoke=False, album="", word_timings=None,
                  onset_tracker=None, onset_anchor_wall=None,
                  onset_anchor_sync=None, voiced_acc=None,
                  song_sec_per_syllable=None, source_player=None,
                  genre=""):
    """Renders a panel that fills the whole terminal, content centered
    both horizontally and vertically inside it. If `cover_offset` is
    nonzero (a cover image is showing on the left via Kitty's graphics
    layer), the panel is narrowed and shifted right by that many
    columns so it doesn't get drawn over the image area."""
    panel_width = max(20, width - cover_offset)
    body_lines = []
    timeline_active = set()  # id()s of body_lines entries that get the bold rail segment

    # One shared line for both render paths below (with/without cover) —
    # auto_paused takes priority since it's the more actionable state.
    shazam_status_hint = None
    if auto_paused:
        shazam_status_hint = f"auto re-listen paused after {SHAZAM_AUTO_RELOAD_MAX_FAILURES} misses — press s"
    elif reidentify_soon:
        shazam_status_hint = "re-identifying soon…"

    # fill more of a tall terminal with actual lyric context instead of
    # leaving a fixed small block surrounded by empty space
    # border/padding/header/gaps/progress/hint, roughly; the top header
    # (shown only without cover art) needs two more rows or a full window overflows
    reserved = 15 if (not cover_offset and not ZEN_MODE and LAYOUT_MODE != 4) else 11
    available = max(0, height - reserved)
    context_lines = max(CONTEXT_LINES, min(MAX_CONTEXT_LINES, available // 2))
    # layout tweaks
    layout = LAYOUT_MODE  # 0 classic · 1 bait · 2 timeline · 3 hud · 4 dock
    if ZEN_MODE or WORD_MODE:
        # mini mode — only the active line (and maybe one neighbor)
        context_lines = 0
    elif layout == 1:  # bait — one stanza at a time (window comes from _bait_bounds)
        pass
    elif layout == 0:  # classic — the old timeline look: one extra pair of context lines
        context_lines = max(context_lines, min(MAX_CONTEXT_LINES, available // 2 + 1))
    elif layout == 2:  # timeline — same density as classic, the rail is its identity
        pass
    elif layout == 3:  # hud — prev / active / next in corner brackets
        context_lines = 1
    elif layout == 4:  # dock — lyrics own the upper area
        context_lines = max(CONTEXT_LINES, min(MAX_CONTEXT_LINES, available // 2))

    if not entries:
        if plain_lyrics:
            # No LRC timing for this track/version, but lrclib still had
            # lyrics text — show it un-synced rather than leaving the
            # screen effectively empty. No karaoke sweep or per-line
            # highlight is possible here (no timestamps to key off), so
            # it's just plain centered text that slowly scrolls with
            # playback position if it's too long to fit at once.
            max_plain = max(3, context_lines * 2 + 1)
            plines = plain_lyrics_window(plain_lyrics, pos, duration, max_plain)
            body_lines.append(Text("no synced LRC · plain lyrics", style="dim italic"))
            body_lines.append(Text(""))
            for l in plines:
                body_lines.append(Text(l, style="white"))
        else:
            # Instrumental / no lyrics at all — ambient pattern + meta
            info_accent_hex = "#{:02x}{:02x}{:02x}".format(
                *_boost_accent_rgb(_get_accent_rgb())
            )
            body_lines.append(Text(""))
            is_comp = looks_like_compilation(title, duration or 0, has_lyrics=False)
            if is_comp:
                body_lines.append(
                    Text("∿  no lyrics for this video title", style="dim italic", justify="center")
                )
                body_lines.append(Text(""))
                body_lines.append(
                    Text("compilation / mix detected", style=f"bold {info_accent_hex}", justify="center")
                )
                if shazam_hint:
                    body_lines.append(
                        Text("press  s  to identify the song playing now", style="bold white", justify="center")
                    )
                    body_lines.append(
                        Text("auto-identify runs when a mix is detected", style="dim", justify="center")
                    )
                else:
                    body_lines.append(
                        Text("install songrec for auto song identify", style="dim", justify="center")
                    )
            else:
                body_lines.append(
                    Text("∿  no lyrics found", style="dim italic", justify="center")
                )
                if shazam_hint:
                    body_lines.append(
                        Text("try  s  identify  ·  m  search", style="dim", justify="center")
                    )
            if album and not is_comp:
                body_lines.append(Text(album, style=f"bold {info_accent_hex}", justify="center"))
            mins, secs = divmod(int(duration or 0), 60)
            if duration:
                body_lines.append(Text(f"{mins}:{secs:02d}", style="dim", justify="center"))
            body_lines.append(Text(""))
            pattern_width = max(10, min(panel_width - 4, 60))
            pattern_height = max(3, min(available - 4, 8))
            for row_text in render_ambient_pattern(pos, pattern_width, pattern_height, info_accent_hex):
                row_text.justify = "center"
                body_lines.append(row_text)
    else:
        # sync_pos (not the raw playback `pos`) is what decides which line
        # counts as "current" — this is the global offset from '[' / ']',
        # letting the person nudge lyric timing to match a track's actual
        # vocal without touching the progress bar/time display below,
        # which still reflects real playback position.
        sync_pos = pos + sync_offset
        idx = current_index(entries, sync_pos)
        start = max(0, idx - context_lines)
        end = min(len(entries) - 1, idx + context_lines)
        if layout == 1 and not (ZEN_MODE or WORD_MODE):
            start, end = _bait_bounds(entries, idx)

        line_start = entries[idx][0] if idx >= 0 else 0
        next_line_start = entries[idx + 1][0] if idx + 1 < len(entries) else max(line_start + 4, duration or line_start + 4)
        current_line_text = entries[idx][1] if idx >= 0 else ""
        if romanize_enabled and romanized and 0 <= idx < len(romanized) and romanized[idx]:
            # Swap in the romanized line wherever the original would be
            # used — not just for display, but for the word-count
            # estimate below too. That estimate is what the karaoke wipe
            # timing is based on, and Japanese (and similar space-less
            # scripts) has no word boundaries for `.split()` to find, so
            # the whole line reads as a single "word" and the wipe fires
            # almost instantly. Romaji has real spaces, so this fixes the
            # timing as a side effect of fixing the display.
            current_line_text = romanized[idx]

        # The gap to the NEXT line's timestamp often includes instrumental
        # breaks / held notes / silence before the next lyric starts — not
        # just the time it takes to sing the current line. Blindly wiping
        # across that whole gap makes the animation crawl on well after the
        # vocal for this line has actually finished. Estimate a plausible
        # sung-line duration from word count instead, and cap the wipe to
        # whichever is shorter: that estimate, or the real gap (for short
        # gaps where the estimate would overshoot into the next line).
        #
        # raw_span can be zero or even negative for some lrclib files —
        # duplicate/near-duplicate timestamps on consecutive lines (backing
        # vocals, ad-libs sharing a timestamp with the main line, etc.).
        # That used to force wipe_span to 0, which meant frac stayed 0.0
        # for that entire line's time on screen — the line just sat there
        # plain white the whole time and never got a chance to wipe at
        # all. Now a bad/missing raw_span is ignored rather than treated
        # as "wipe instantly finishes" — the word-count estimate alone
        # decides the duration, so every line always gets a real wipe.
        raw_span = next_line_start - line_start
        est_words = max(1, len((current_line_text or "").split()))
        word_est_span = _estimate_line_seconds(current_line_text, song_sec_per_syllable)
        wipe_span = min(raw_span, word_est_span) if raw_span > 0 else word_est_span

        frac = max(0.0, min(1.0, (sync_pos + _lead_for(sync_pos - line_start) - line_start) / wipe_span))

        # Hold-on-silence: freeze wipe progress while monitor RMS is below
        # the silence floor. `voiced_acc` is a monotonic watermark of the
        # highest clock-frac reached while audio was clearly present.
        # Silence can only HOLD at that watermark — never pull the wipe
        # backward to a lower value (that was causing maju-mundur flicker
        # when RMS flickered around the threshold).
        if (karaoke_enabled and ONSET_ENABLED and onset_tracker is not None
                and onset_tracker.alive and onset_tracker.buf_sec > 0.12
                and onset_tracker.last_rms < ONSET_SILENCE_RMS
                and voiced_acc is not None and voiced_acc > 0.0):
            frac = min(frac, float(voiced_acc))

        # Optional hybrid: nudge toward real vocal onsets (only when enabled).
        # Gated to Playing only — the onset tracker keeps sampling the audio
        # monitor even while paused, and ambient noise/silence picked up
        # there was still registering as spurious "onsets", yanking `frac`
        # around every single frame. That's what looked like the lyric
        # line flickering/resetting on pause: the wipe fraction was being
        # constantly re-nudged off a frozen `sync_pos` by noise, not by
        # anything actually reloading or re-rendering from scratch.
        if (onset_tracker is not None and karaoke_enabled and ONSET_ENABLED
                and status == "Playing"):
            frac = _onset_adjusted_frac(
                frac, line_start, wipe_span, sync_pos,
                onset_tracker, onset_anchor_wall, onset_anchor_sync,
            )

        # ---- TEMP DIAGNOSTIC: pinpointing the still-unresolved pause
        # flicker. status_debug.log only records real player Playing/
        # Paused transitions, which doesn't explain a flicker that
        # persists with no such transition happening. This logs whenever
        # idx/frac/pos actually change while NOT Playing, so we can see
        # whether something is still moving the wipe every frame during
        # a genuine pause, or whether nothing here changes at all (which
        # would point at Live/terminal-level redraw instead of app state).
        # Safe to delete once the cause is found.
        if status != "Playing":
            _prev = getattr(render_frame, "_dbg_prev", None)
            _cur = (idx, round(frac, 4), round(pos, 3), len(entries), artist, title)
            if _cur != _prev:
                render_frame._dbg_prev = _cur
                try:
                    with open(CACHE_DIR / "frame_debug.log", "a") as _f:
                        _f.write(
                            f"{time.strftime('%H:%M:%S.%f')[:-3]} "
                            f"status={status!r} idx={idx} frac={frac:.4f} "
                            f"pos={pos:.3f} sync_pos={sync_pos:.3f} "
                            f"n_entries={len(entries)} "
                            f"artist={artist!r} title={title!r} "
                            f"vocal_intensity={_VOCAL_INTENSITY:.3f}\n"
                        )
                except Exception:
                    pass

        # Same boosted-from-Kitty-theme accent color for the active line
        # in both karaoke and non-karaoke mode, computed once here instead
        # of each mode resolving ACCENT through its own separate pipeline
        # (karaoke_line() used to boost saturation/value off the live
        # Kitty hue, while non-karaoke mode used the raw "bright_magenta"
        # ANSI name and let the terminal theme translate it directly —
        # two different paths that could land on visibly different
        # shades of the same nominal accent color).
        active_accent_hex = "#{:02x}{:02x}{:02x}".format(
            *_boost_accent_rgb(_get_accent_rgb(), intensity=_VOCAL_INTENSITY)
        )

        # word-level timings for the active line (Enhanced LRC), if any
        active_word_timings = None
        if word_timings and 0 <= idx < len(word_timings):
            active_word_timings = word_timings[idx]

        debug_line = None
        if debug_karaoke:
            # Raw numbers behind this frame's wipe calc for the active
            # line — dumped on-screen instead of guessed at blind, so a
            # "still stuck white" report can be matched against exactly
            # what raw_span/wipe_span/frac were doing on that line.
            wl_tag = f" words_ts={len(active_word_timings)}" if active_word_timings else " words_ts=none"
            # onset tracker status for the hybrid wipe path
            if onset_tracker is None:
                onset_tag = " onset=off"
            elif not onset_tracker.alive:
                onset_tag = " onset=dead"
            else:
                n_recent = len(onset_tracker.recent_onsets(
                    since_wall=(onset_anchor_wall or 0.0) - 0.05
                )) if onset_anchor_wall else len(onset_tracker._onsets_wall)
                va = f" voiced={voiced_acc:.2f}" if voiced_acc is not None else ""
                onset_tag = (
                    f" onset=live n={n_recent} blend={ONSET_BLEND:.2f}"
                    f" rms={onset_tracker.last_rms:.3f}"
                    f" buf={onset_tracker.buf_sec:.1f}s"
                    f" via={onset_tracker.backend or '?'}"
                    f"{va}"
                )
            _dbg_syl = sum(_estimate_syllables(w) for w in (current_line_text or "").split()) or 0
            _dbg_sps = song_sec_per_syllable if song_sec_per_syllable is not None else KARAOKE_SECONDS_PER_SYLLABLE
            _dbg_pace_tag = "song" if song_sec_per_syllable is not None else "default"
            debug_line = Text(
                f"[dbg] idx={idx} line_start={line_start:.2f} next={next_line_start:.2f} "
                f"raw_span={raw_span:.2f} words={est_words} syl={_dbg_syl} word_est={word_est_span:.2f} "
                f"wipe_span={wipe_span:.2f} sync_pos={sync_pos:.2f} frac={frac:.3f} "
                f"pace={_dbg_sps:.2f}s/syl({_dbg_pace_tag})"
                f"{wl_tag}{onset_tag}",
                style="dim yellow",
            )

        for i in range(start, end + 1):
            _, line_text = entries[i]
            if romanize_enabled and romanized and i < len(romanized) and romanized[i]:
                line_text = romanized[i]
            # skip blank LRC rows (timed empty lines) — they only produce
            # stray underline/bar artifacts under the real lyric
            if not (line_text or "").strip():
                continue
            line_text = line_text or " "
            if i == idx:
                if i > start and layout != 3:
                    body_lines.append(Text(""))
                if WORD_MODE:
                    cw = _current_word(
                        line_text, frac, active_word_timings, sync_pos,
                        line_end=(next_line_start
                                  if next_line_start and next_line_start > line_start
                                  else None))
                    if cw is not None:
                        w, wk, wn, wp = cw
                        # pure word only: drop , . ! ? " … etc. Keep inner ' and -,
                        # and ( ) — they mark backing vocals.
                        w = re.sub(r"^[^\w(]+|[^\w)]+$", "", w)
                        w = re.sub(r"[^\w'’\-()]", "", w).replace("_", "")
                        big, extra, letters = None, None, []
                        grad = max(0, min(3, WORD_GRADIENT))
                        ga = _hex_to_rgb(active_accent_hex)
                        gb = _grad_end(ga)
                        eff_style = _effective_word_style(WORD_STYLE)
                        sz_idx = max(0, min(2, WORD_SIZE))
                        max_cw, max_ch = max(10, panel_width - 8), max(3, available - 2)
                        if eff_style in (0, 1, 3, 6):
                            big = _big_word_rows(
                                w, max_cw, max_ch,
                                max_s=_WORD_SIZE_DOTS[eff_style][sz_idx],
                                style=eff_style, letters_out=letters)
                        elif eff_style == 4:
                            extra = _osc66_word_rows(
                                w, max_cw, max_ch, sz_idx, ga, grad, wp, gb)
                        elif eff_style == 5:
                            base = _boost_accent_rgb(_get_accent_rgb(), intensity=0.0)
                            extra = _word_image_rows(
                                w, base, max_cw, max_ch, sz_idx,
                                grad, wp, _grad_end(base))
                        body_lines.append(Text(""))
                        if extra:
                            body_lines.extend(extra)
                        elif big:
                            if grad:
                                body_lines.extend(_color_rows(big, letters, grad, wp, ga, gb))
                            else:
                                for row in big:
                                    body_lines.append(Text(row, style=f"bold {active_accent_hex}"))
                        else:
                            spaced = " ".join(w.upper())
                            if len(spaced) > max(10, panel_width - 8):
                                spaced = w.upper()
                            if grad and spaced:
                                li, lt = -1, []
                                for chh in spaced:
                                    if not chh.isspace():
                                        li += 1
                                    lt.append(max(li, 0))
                                body_lines.extend(_color_rows([spaced], lt, grad, wp, ga, gb))
                            else:
                                body_lines.append(Text(spaced, style=f"bold {active_accent_hex}"))
                        body_lines.append(Text(""))
                        if wn <= 16:
                            dots = Text()
                            for d in range(wn):
                                dots.append("● " if d == wk else "· ",
                                            style=active_accent_hex if d == wk else "dim")
                            body_lines.append(dots)
                elif karaoke_enabled:
                    kl = karaoke_line(
                        line_text, frac, active_accent_hex,
                        word_timings=active_word_timings,
                        sync_pos=sync_pos,
                        line_end=(next_line_start
                                  if next_line_start and next_line_start > line_start
                                  else None),
                    )
                    if layout == 2:  # timeline — rail is drawn when body is assembled
                        kl = kl if isinstance(kl, Text) else Text(str(kl))
                        timeline_active.add(id(kl))
                        body_lines.append(kl)
                    elif layout == 0:  # classic — accent block in front of the active line
                        rail = Text()
                        rail.append("▌ ", style=active_accent_hex)
                        rail.append_text(kl if isinstance(kl, Text) else Text(str(kl)))
                        body_lines.append(rail)
                    else:
                        body_lines.append(kl)
                else:
                    if layout == 2:
                        marked = Text(line_text, style=f"bold {active_accent_hex}")
                        timeline_active.add(id(marked))
                    else:
                        marked = Text()
                        marked.append("▌ ", style=active_accent_hex)
                        marked.append(line_text, style=f"bold {active_accent_hex}")
                    body_lines.append(marked)
                if translate_enabled and translations and i < len(translations) and translations[i]:
                    body_lines.append(Text(translations[i], style="dim italic"))
                if debug_line is not None:
                    body_lines.append(debug_line)
                if i < end and layout != 3:
                    body_lines.append(Text(""))
            else:
                distance = abs(i - idx)
                if layout == 3 and distance > 1:
                    continue  # hud: only ±1 context
                if layout == 1 and not (ZEN_MODE or WORD_MODE):
                    # bait: finished lines recede, upcoming ones stay readable
                    body_lines.append(Text(line_text, style="dim" if i < idx else "white"))
                elif distance == 1:
                    body_lines.append(Text(line_text, style="white"))
                elif distance == 2:
                    body_lines.append(Text(line_text, style="dim"))
                else:
                    body_lines.append(Text(line_text, style="rgb(70,70,85)"))

    hud_boxed = False
    if layout == 2 and entries and not (ZEN_MODE or WORD_MODE):
        # timeline: lyrics left-aligned, one continuous vertical rail on
        # their left; the active line's segment is bold + accent
        tl = Table.grid(padding=0)
        tl.add_column(width=2, no_wrap=True)
        tl.add_column()
        for t in body_lines:
            if isinstance(t, Text) and not t.plain:
                continue  # spacers render 0 rows in the other layouts too
            if id(t) in timeline_active:
                tl.add_row(Text("┃ ", style=f"bold {active_accent_hex}"), t)
            else:
                tl.add_row(Text("│ ", style=f"dim {ACCENT}"), t)
        body = Align.center(tl)
    elif layout == 3 and entries and not (ZEN_MODE or WORD_MODE):
        # hud: the 3-line readout sits in corner brackets only
        from rich import box as _box
        hud_boxed = True
        _hud_box = _box.Box(
            "┌  ┐\n"
            "    \n"
            "    \n"
            "    \n"
            "    \n"
            "    \n"
            "    \n"
            "└  ┘\n"
        )
        _hud_w = max(10, panel_width - 10)  # main panel border+padding, hud box edges
        for t in body_lines:
            if isinstance(t, Text):
                t.truncate(_hud_w, overflow="ellipsis")
        body = Align.center(Panel(
            Group(*[Align.center(t) for t in body_lines]),
            box=_hud_box, border_style=ACCENT, padding=(0, 0), expand=False,
        ))
    else:
        body = Group(*[Align.center(t) for t in body_lines])

    # ── progress (live head + pct chip) ───────────────────────────────
    playing = status == "Playing"
    status_glyph = "▶" if playing else "⏸"
    if duration and duration > 0:
        bar_len = min(42, max(20, panel_width // 2 - 6))
        frac_bar = max(0.0, min(1.0, pos / duration))
        filled = int(bar_len * frac_bar)
        head_chars = "●◉◎○"
        head = head_chars[int(pos * 4) % len(head_chars)] if playing else "○"
        bar = Text()
        if filled > 0:
            bar.append("━" * (filled - 1), style=f"bold {ACCENT}")
            bar.append(head, style=f"bold {ACCENT}")
        else:
            bar.append(head, style="dim")
            filled = 1
        bar.append("╌" * (bar_len - filled), style="dim")
        pct = int(frac_bar * 100)
        progress = Text(justify="center")
        progress.append(f"{status_glyph}  ", style=ACCENT if playing else "dim")
        progress.append_text(bar)
        progress.append(f"  {fmt_time(pos)} / {fmt_time(duration)}", style="dim")
        progress.append(f"  ·  {pct}%", style=f"dim {ACCENT}")
        if source_player and not shazam_active:
            progress.append(f"  ·  {_player_short_name(source_player)}", style="dim")
    else:
        progress = Text(justify="center")
        progress.append(f"{status_glyph}  {fmt_time(pos)}", style=ACCENT if playing else "dim")
        if source_player and not shazam_active:
            progress.append(f"  ·  {_player_short_name(source_player)}", style="dim")

    # ── key hints (built once, used by all layouts) ───────────────────
    # The full key list lives in the '?' help panel. The footer keeps only
    # the hint plus state that would otherwise be invisible.
    row1 = ["? help"]
    if abs(sync_offset) > 0.001:
        row1.append(f"sync:{sync_offset:+.2f}s")
    if shazam_active:
        row1.append("b exit id")
    row2 = []

    blocks = []
    if layout == 4:
        # ── BOTTOM DOCK ───────────────────────────────────────────────
        # Push the dock strip to the physical bottom of the panel with
        # enough blank rows between lyrics and dock so it doesn't float
        # up when there are few lyric lines on screen.
        dock_parts = []
        dock_w = min(panel_width - 6, 52)
        dock_parts.append(Align.center(Text("─" * dock_w, style=f"dim {ACCENT}")))
        meta = Text(justify="center")
        meta.append(title or "unknown", style=f"bold {ACCENT}")
        if artist:
            meta.append("  ·  ", style="dim")
            meta.append(artist, style=ARTIST_ACCENT)
        if shazam_active:
            meta.append("  · songrec", style="dim italic")
        dock_parts.append(meta)
        if shazam_status_hint:
            dock_parts.append(Text(shazam_status_hint, style=f"italic {WARN_ACCENT}", justify="center"))
        dock_parts.append(Align.center(progress))
        sep = "  ·  "
        if row1:
            dock_parts.append(Align.center(Text(sep.join(row1), style="dim")))
        if row2:
            dock_parts.append(Align.center(Text(sep.join(row2), style="dim")))

        if ZEN_MODE:
            dock_parts = []  # zen: lyrics only, no dock/progress/hints

        # panel content rows ≈ height - borders(2) - padding(2)
        usable = max(8, height - 4)
        body_h = max(1, sum(1 for t in body_lines if not (isinstance(t, Text) and not t.plain)))
        dock_h = len(dock_parts)
        free = max(1, usable - body_h - dock_h)

        if ZEN_MODE or WORD_MODE:
            above = free // 2
            below = free - above
            want_above = (usable - body_h) // 2
            if want_above >= 0 and free - want_above >= 0:
                above = want_above
                below = free - above
            blocks.extend([Text("")] * above)
            blocks.append(body)
            blocks.extend([Text("")] * below)
        else:
            blocks.append(body)
            blocks.extend([Text("")] * free)
        blocks.extend(dock_parts)
        inner = Group(*blocks)
        centered = Align.center(inner, vertical="top")
    else:
        # ── classic / bait / timeline / hud ─────────────────────────
        # Same bottom-lock idea as dock: header + lyrics on top, progress
        # + hints pinned to the bottom via flexible spacer rows.
        # Zen mode: drop the header chrome and vertically center the
        # active lyric in the remaining space so it sits dead-middle.
        header_parts = []
        if not cover_offset and not ZEN_MODE:
            header = Text(justify="center")
            header.append("✦ ", style=ACCENT)
            header.append(title or "unknown title", style=f"bold {ACCENT}")
            header_parts.append(header)
            if artist:
                header_parts.append(Text(artist, style=ARTIST_ACCENT, justify="center"))
            if shazam_active:
                header_parts.append(Text("identified · SongRec", style="dim italic", justify="center"))
                if shazam_status_hint:
                    header_parts.append(Text(shazam_status_hint, style=f"italic {WARN_ACCENT}", justify="center"))
            header_parts.append(Text(""))
            header_parts.append(Text("─" * min(32, max(12, panel_width // 4)), style=f"dim {ACCENT}", justify="center"))
            header_parts.append(Text(""))

        footer_parts = []
        footer_parts.append(Align.center(progress))
        if shazam_active and cover_offset:
            footer_parts.append(Text("songrec", style="dim italic", justify="center"))
        if row1 or row2:
            sep = "  ·  "
            if row1:
                footer_parts.append(Align.center(Text(sep.join(row1), style="dim")))
            if row2:
                footer_parts.append(Align.center(Text(sep.join(row2), style="dim")))

        if ZEN_MODE:
            footer_parts = []  # zen: lyrics only, no progress/hints

        usable = max(8, height - 4)
        body_h = max(1, sum(1 for t in body_lines if not (isinstance(t, Text) and not t.plain)))
        if hud_boxed:
            body_h += 2  # bracket rows
        header_h = len(header_parts)
        footer_h = len(footer_parts)
        free = max(1, usable - header_h - body_h - footer_h)

        bait_sub = layout == 1 and bool(entries) and not (ZEN_MODE or WORD_MODE)
        if ZEN_MODE or WORD_MODE or bait_sub:
            # split free space above/below so the lyric sits vertically
            # centered between the (empty) top and the footer
            above = free // 2
            below = free - above
            # center on the PANEL, not just on the leftover space between
            # a tall header and a short footer; fall back if it won't fit
            want_above = (usable - body_h) // 2 - header_h
            # bait: center in the visible empty area, not on the whole panel
            # (the footer eats the bottom rows, so panel-centering looks low)
            if not bait_sub and want_above >= 0 and free - want_above >= 0:
                above = want_above
                below = free - above
            blocks.extend(header_parts)  # empty in zen when cover shown
            blocks.extend([Text("")] * above)
            blocks.append(body)
            blocks.extend([Text("")] * below)
            blocks.extend(footer_parts)
        else:
            blocks.extend(header_parts)
            blocks.append(body)
            blocks.extend([Text("")] * free)
            blocks.extend(footer_parts)
        inner = Group(*blocks)
        centered = Align.center(inner, vertical="top")

    # thinner padding + subtler border feel
    panel = Panel(
        centered,
        border_style=ACCENT,
        padding=(1, 3),
        width=panel_width,
        height=height,
        title=" Sweep Lyrics ",
        title_align="left",
    )
    if not cover_offset:
        return panel

    # ── cover + caption column (aggressive 2-col) ─────────────────────
    caption_width = max(1, COVER_COLS)
    cover_rows = _cover_rows()

    def _truncate(s, max_lines=1):
        s = s or ""
        limit = caption_width * max_lines
        return s if len(s) <= limit else s[:limit - 1] + "…"

    if layout == 4:
        # dock: cover is at the bottom of the left column — leave the
        # upper area empty and put a thin caption just above the art.
        top_pad = max(0, height - cover_rows - 6)
        caption_lines = [Text("")] * top_pad
        caption_lines.append(Text(_truncate(title or "unknown", max_lines=2), style=f"bold {ACCENT}"))
        caption_lines.append(Text(_truncate(artist, max_lines=1), style=ARTIST_ACCENT))
        if genre:
            caption_lines.append(Text(_truncate(genre, max_lines=1), style=f"dim {ACCENT_DIM}"))
        caption_lines.append(Text("─" * caption_width, style=f"dim {ACCENT}"))
        caption_lines.extend([Text("")] * cover_rows)
    else:
        # classic: keep text stack glued under the art, and match the
        # same top offset Kitty uses so image + caption read as one card
        top = _cover_place_top(height)
        caption_lines = [Text("")] * (top + cover_rows)
        caption_lines.append(Text("─" * caption_width, style=f"dim {ACCENT}"))
        caption_lines.append(Text(_truncate(title or "unknown", max_lines=3), style=f"bold {ACCENT}"))
        caption_lines.append(Text(_truncate(artist, max_lines=2), style=ARTIST_ACCENT))
        if genre:
            caption_lines.append(Text(_truncate(genre, max_lines=1), style=f"dim {ACCENT_DIM}"))
        if shazam_active:
            caption_lines.append(Text("SongRec", style="dim italic"))
            if shazam_status_hint:
                caption_lines.append(
                    Text(_truncate(shazam_status_hint), style=f"italic {WARN_ACCENT}")
                )
        # pad remaining height so the left column doesn't collapse
        remain = max(0, height - len(caption_lines) - 2)
        if remain:
            caption_lines.extend([Text("")] * remain)
    left_col = Padding(Group(*caption_lines), (0, 0, 0, COVER_MARGIN_LEFT))

    grid = Table.grid(expand=False, padding=0)
    grid.add_column(width=cover_offset)
    grid.add_column()
    grid.add_row(left_col, panel)
    return grid


# -------------------------------------------------------------------- main

def _track_key(player):
    meta = get_metadata(player)
    if meta is None:
        return None, None
    meta = _enrich_from_plasma(meta)
    a, t = resolve_track_info(meta)
    return f"{a}|{t}".lower(), meta


def _do_shazam_listen(live, auto=False):
    """Pauses the Live display, shows a styled 'Listening...' panel,
    runs SongRec, then resumes Live. Returns the run_songrec() result
    dict, or None on failure/timeout/missing binary.

    auto=True is used when this fires because the identified song's
    duration was passed (see main loop) rather than a manual 's' press —
    only the printed message changes, the SongRec call itself is
    identical either way."""
    live.stop()
    listen_console = Console()
    listen_console.clear()

    if shutil.which("songrec") is None:
        listen_console.print(
            Align.center(Text("⚠  songrec not installed — try: paru -S songrec", style="yellow"))
        )
        time.sleep(2)
        live.start()
        return None

    header = "track ended · re-identifying…" if auto else "listening…"
    panel = Panel(
        Group(
            Text(header, style=f"bold {ACCENT}", justify="center"),
            Text(""),
            Text(f"≤ {SHAZAM_LISTEN_TIMEOUT}s  ·  Ctrl+C cancel", style="dim", justify="center"),
        ),
        border_style=ACCENT,
        padding=(1, 4),
        width=42,
        title=" songrec ",
        title_align="left",
    )
    listen_console.print(Align.center(panel))

    result = run_songrec()

    if result is None:
        listen_console.print()
        listen_console.print(Align.center(Text("no match · try again later", style="dim italic")))
        time.sleep(1.5)

    live.start()
    return result


def _apply_shazam_result(result, cover_path, cover_offset, offset_cache):
    """Applies a successful SongRec result: fetches the identified song's
    lyrics and resets Shazam position/duration tracking. Shared by the
    manual 's' press and the auto-reload path (main loop) so the two
    can't drift out of sync. Also the single choke point for everything
    that should happen exactly once per successful identify: logging it
    to history, firing a desktop notification, and looking up this
    track's saved sync offset. Returns a dict of the updated state —
    caller unpacks it into its own locals."""
    artist, title = result["artist"], result["title"]
    offset = result["offset"]

    log_shazam_history(artist, title)
    entries, plain_lyrics, lrc_duration, word_timings = load_lyrics_with_words(
        artist, title, "", 0
    )

    # lrclib's duration for the identified track can be bogus/mismatched
    # (wrong recording, mislabeled snippet, etc.) — if it's shorter than
    # how far SongRec says we already are into the song (or just too
    # short to be a real track), it can't be trusted as an auto-reload
    # threshold: using it as-is would put shazam_pos straight back over
    # duration next frame, which is what caused the endless "auto
    # re-listening" loop. Report it as implausible instead of a bogus
    # number, so the caller (main loop) counts this toward the failure
    # streak rather than treating it as a clean resolution — and so the
    # UI falls back to the last-synced-line grace period instead of
    # showing a nonsense progress bar like "1:00 / 0:30".
    implausible = bool(lrc_duration) and (lrc_duration < MIN_PLAUSIBLE_DURATION or lrc_duration <= offset)
    if implausible:
        lrc_duration = 0

    new_cover = fetch_cover(result.get("cover_url", "")) if COVER_ENABLED else None
    if new_cover:
        cover_path = new_cover
        show_cover(cover_path)
        cover_offset = COVER_GUTTER
        extract_cover_accent(cover_path)
    else:
        extract_cover_accent(None)

    notify_shazam_match(artist, title, cover_path)

    return {
        "artist": artist,
        "title": title,
        "entries": entries,
        "plain_lyrics": plain_lyrics,
        "album": "",
        "duration": lrc_duration,
        "shazam_active": True,
        "shazam_pos": offset,
        "cover_path": cover_path,
        "cover_offset": cover_offset,
        "implausible": implausible,
        "sync_offset": offset_for_track(offset_cache, artist, title),
        "word_timings": word_timings,
        "song_sec_per_syllable": _calibrate_song_tempo(entries),
    }


def _load_normal_track(player, cover_path, cover_offset, offset_cache):
    """Reloads the plain MPRIS title/artist/duration for `player` plus
    its lyrics + cover — the same fields the outer loop loads for a
    fresh track. Shared by 'b' (explicit back-to-normal) and 's'
    pressed while already in Shazam mode (which now toggles back
    instead of re-identifying, see main loop). Returns None if the
    player's metadata can't be read right now — caller should just
    leave the current state alone in that case."""
    cur_meta = get_metadata(player)
    if not cur_meta:
        return None
    cur_meta = _enrich_from_plasma(cur_meta)
    a, t = resolve_track_info(cur_meta)
    artist, title, duration = a, t, cur_meta["duration"]
    entries, plain_lyrics, _, word_timings = load_lyrics_with_words(
        artist, title, cur_meta["album"], duration
    )

    cover_url = _pick_cover_url(cur_meta)
    new_cover = fetch_cover(cover_url) if COVER_ENABLED else None
    if new_cover:
        cover_path = new_cover
        show_cover(cover_path)
        cover_offset = COVER_GUTTER
    elif COVER_ENABLED:
        clear_cover()
        cover_path = None
        cover_offset = 0

    return {
        "artist": artist,
        "title": title,
        "duration": duration,
        "entries": entries,
        "plain_lyrics": plain_lyrics,
        "album": cur_meta["album"],
        "cover_path": cover_path,
        "cover_offset": cover_offset,
        "cover_url": cover_url,
        "sync_offset": offset_for_track(offset_cache, artist, title),
        "word_timings": word_timings,
        "song_sec_per_syllable": _calibrate_song_tempo(entries),
    }


def _prompt_line(prompt: str = "  › ") -> str | None:
    """Read a full line with echo while the app is otherwise in cbreak
    mode. Temporarily enables ICANON+ECHO, then restores whatever
    stdin mode was active (usually cbreak from RawKeys). Empty line
    or Ctrl+C → None (cancel)."""
    if not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except Exception:
        saved = None
    try:
        if saved is not None:
            cooked = termios.tcgetattr(fd)
            # lflag is index 3
            cooked[3] = cooked[3] | termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG
            termios.tcsetattr(fd, termios.TCSADRAIN, cooked)
        # drain any pending single-key bytes from cbreak so they don't
        # pollute the first character of the line
        try:
            import select as _sel
            while _sel.select([fd], [], [], 0)[0]:
                try:
                    os.read(fd, 1)
                except Exception:
                    break
        except Exception:
            pass
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return None
        return line.rstrip("\r\n")
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        sys.stdout.flush()
        return None
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except Exception:
                try:
                    tty.setcbreak(fd)
                except Exception:
                    pass


def manual_lyrics_search(live, artist: str, title: str):
    """Interactive lrclib search — type a query, pick a result, return
    state dict or None on cancel. Restores cbreak when done so the
    main loop keeps working."""
    try:
        live.stop()
    except Exception:
        pass
    console = Console()
    console.clear()
    console.print(Text("  manual lyrics search", style=f"bold {ACCENT}"))
    console.print(Text(f"  current: {artist} — {title}", style="dim"))
    console.print()
    console.print("  type query · enter search · empty / ctrl+c cancel", style="dim")
    # pre-fill suggestion shown as hint only (user still types)
    hint = f"{artist} {title}".strip()
    if hint:
        console.print(Text(f"  hint: {hint}", style="dim italic"))
    console.print()

    query = _prompt_line("  › ")
    if query is None or not query.strip():
        try:
            live.start()
        except Exception:
            pass
        return None
    query = query.strip()

    console.print(Text("  searching…", style="dim"))
    results = []
    try:
        r = requests.get(f"{LRCLIB_API}/search", params={"q": query}, timeout=6)
        if r.ok:
            results = [x for x in (r.json() or []) if x.get("syncedLyrics") or x.get("plainLyrics")]
    except Exception:
        pass

    if not results:
        console.print(Text("  no results. enter to go back.", style=WARN_ACCENT))
        _prompt_line("  › ")
        try:
            live.start()
        except Exception:
            pass
        return None

    console.print()
    for i, res in enumerate(results[:8]):
        dur = res.get("duration") or 0
        synced = "LRC" if res.get("syncedLyrics") else "plain"
        console.print(
            f"  {i + 1}. {res.get('artistName', '?')} — {res.get('trackName', '?')}  "
            f"[{synced} · {dur}s]",
            style="white",
        )
    console.print()
    console.print("  pick 1-8 · empty cancel", style="dim")
    choice = _prompt_line("  › ")
    if choice is None or not choice.strip().isdigit():
        try:
            live.start()
        except Exception:
            pass
        return None
    idx = int(choice.strip()) - 1
    if idx < 0 or idx >= min(8, len(results)):
        try:
            live.start()
        except Exception:
            pass
        return None

    pick = results[idx]
    a = pick.get("artistName") or artist
    t = pick.get("trackName") or title
    entries, plain, lrc_dur, wt = load_lyrics_with_words(
        a, t, pick.get("albumName") or "", pick.get("duration") or 0,
    )
    if not entries and pick.get("syncedLyrics"):
        entries = parse_lrc(pick["syncedLyrics"])
        plain = plain or (pick.get("plainLyrics") or "")
        lrc_dur = pick.get("duration") or 0
        wt = None
    try:
        live.start()
    except Exception:
        pass
    return {
        "artist": a,
        "title": t,
        "entries": entries,
        "plain_lyrics": plain,
        "duration": lrc_dur,
        "word_timings": wt,
        "song_sec_per_syllable": _calibrate_song_tempo(entries),
    }


def smart_refetch_candidates(artist: str, title: str, duration: int, current_entries):
    """Return alternative lrclib results (different from what we likely
    already loaded) ranked by score — used when wipe behavior looks wrong."""
    try:
        r = requests.get(
            f"{LRCLIB_API}/search",
            params={"track_name": title, "artist_name": artist},
            timeout=5,
        )
        if not r.ok:
            return []
        results = [x for x in (r.json() or []) if x.get("syncedLyrics")]
        if not results:
            return []
        scored = sorted(
            results,
            key=lambda x: _score_result(x, duration, artist, title),
            reverse=True,
        )
        # drop the exact same synced text if we can detect it
        cur_sig = ""
        if current_entries:
            cur_sig = "|".join(f"{t:.2f}:{txt[:20]}" for t, txt in current_entries[:4])
        out = []
        for res in scored[:6]:
            synced = res.get("syncedLyrics") or ""
            ents = parse_lrc(synced)
            sig = "|".join(f"{t:.2f}:{txt[:20]}" for t, txt in ents[:4]) if ents else ""
            if sig and sig == cur_sig:
                continue
            out.append(res)
        return out
    except Exception:
        return []


def main():
    console = Console()
    last_key = None
    pending_key = None       # a cur_key that differs from last_key but hasn't
    pending_key_since = None # persisted long enough yet to trust as a real
                              # track change (see the debounce below) — guards
                              # against players (YT Music in particular) that
                              # briefly mutate the tab/media title on pause
                              # without actually changing track, which used
                              # to force a full lyrics reload on every blip.
    KEY_CHANGE_DEBOUNCE = 1.5  # seconds a new cur_key must persist before
                                # it's trusted as a genuine track change
    no_artist_since = None  # wall-clock time we first saw a real title w/ empty artist
    entries = []
    plain_lyrics = ""
    word_timings = None  # parallel to entries; from Enhanced LRC when available
    song_sec_per_syllable = None  # per-song calibrated karaoke pace; see
                                   # _calibrate_song_tempo — None = use
                                   # the global KARAOKE_SECONDS_PER_SYLLABLE
    album = ""
    artist = title = ""
    duration = 0
    cover_path = None
    # URL last used to populate cover_path — tracked separately from the
    # track key (which is artist+title text) so we can catch cover art
    # changing on its own, e.g. a mashup/compilation video whose MPRIS
    # title never changes across the whole video while artUrl still
    # updates per underlying song (some senders like KDE Plasma Browser
    # Integration do this). Without this, cover art would only ever
    # refresh on a full track-key change or a manual 's'/'b' press.
    last_cover_url = None

    # Shazam-mode state — reset whenever the underlying MPRIS track
    # actually changes (see "if key != last_key" below), NOT on every
    # re-entry into the Live loop, so it survives a player briefly
    # disappearing and coming back on the same track.
    shazam_active = False
    shazam_pos = 0.0
    # Wall-clock timestamp before which an auto-reload won't fire again —
    # init'd here too (not just on track-change below) since last_key
    # starts as None, so the very first loop iteration already counts as
    # a "track change" and would otherwise skip straight past this.
    next_auto_reload = 0.0
    # Consecutive auto-reload attempts that came back with no match —
    # capped at SHAZAM_AUTO_RELOAD_MAX_FAILURES so a silence/ad gap
    # between mashup songs doesn't retry SongRec forever.
    shazam_auto_fail_streak = 0
    # Set when the outer loop notices a track-key change while already
    # in Shazam mode but can't re-listen yet (no Live instance up) —
    # actioned right after entering the Live block below.
    pending_shazam_reidentify = False
    # Set on track load when title/duration looks like a YouTube mix /
    # compilation and no LRC matched — triggers SongRec once Live is up.
    pending_auto_shazam = False
    # shazam_pos at the moment of the last successful identify — used as
    # the floor for MIN_HOLD so a wrong-short duration can't rescan early.
    shazam_hold_floor = 0.0
    # Fingerprint of the underlying media when we last identified.
    # Used to tell "title blip on same video" apart from "user switched
    # to a different video/playlist mid-song".
    shazam_media_url = ""
    shazam_media_art = ""
    shazam_mpris_key = ""  # last MPRIS artist|title we accepted

    # The MPRIS player we're currently following — once set, stays sticky
    # across scans (see find_active_player's `preferred` param) instead of
    # potentially flipping to a different "Playing" player every check.
    locked_player = None

    # Karaoke wipe on/off, toggled with 'k' — deliberately NOT reset on
    # track change or shazam mode switch, since it's a display preference
    # for the session, not per-track state.
    karaoke_enabled = True
    debug_karaoke = False

    # Lightweight vocal-onset tracker (pure numpy spectral flux). Started
    # lazily on first frame; polled every UI tick. Anchor (wall, sync_pos)
    # is refreshed whenever the active lyric line changes so onset times
    # can be mapped back into playback coordinates.
    # Always construct if numpy is present; ONSET_ENABLED gates start/poll
    # so the settings toggle can enable it mid-session without restart.
    onset_tracker = OnsetTracker() if HAVE_NUMPY else None
    onset_anchor_wall = None
    onset_anchor_sync = None
    onset_anchor_idx = -999
    voiced_acc = 0.0  # seconds of above-silence audio in the current line


    # Per-track lyric-sync offset (seconds), nudged with '[' / ']', reset
    # with 'r'. Persisted to OFFSET_CACHE_FILE keyed by artist+title, so
    # it's looked up fresh on every track change (see "if key != last_key"
    # below and the Shazam apply/load-normal call sites) instead of
    # carrying over from whatever song played before it.
    offset_cache = load_offset_cache()
    settings = load_settings()
    apply_settings(settings)
    zoom_steps = restore_kitty_zoom(load_zoom_steps())
    global_offset = 0.0

    # Translation of the current line, toggled with 't'. translate_enabled
    # is a session preference like karaoke_enabled (not reset on track
    # change); `translations` is the actual per-track translated-lines
    # list and IS reset to None on every track change, since it applies to
    # a specific song's `entries` and gets lazily (re)fetched on 't' press.
    translate_enabled = False
    translations = None

    # Romanization of the current line (kanji/kana -> Latin script),
    # toggled with 'y'. Same split as translation: romanize_enabled is a
    # session preference, `romanized` is the per-track computed list,
    # reset to None on every track change and (re)computed lazily on 'y'.
    # Local conversion via pykakasi — no network round-trip needed, so
    # unlike translation there's no disk cache, it's just recomputed
    # per-track (fast) the first time it's toggled on.
    romanize_enabled = False
    romanized = None

    with RawKeys():
        while True:
            player, _status = find_active_player(locked_player)
            locked_player = player

            if not player:
                print("\x1b[2J\x1b[H", end="")
                print("waiting for a playing MPRIS player…  (q / esc to quit)")
                if poll_key(1.5) in ("q", "Q", "esc"):
                    raise KeyboardInterrupt
                continue

            key, meta = _track_key(player)
            if key is None:
                if poll_key(POLL_INTERVAL) in ("q", "Q", "esc"):
                    raise KeyboardInterrupt
                continue

            # Chrome sometimes blips metadata to title="YouTube" /
            # artist="" mid-playback. Don't treat that as a real track
            # change or we'll wipe good lyrics with an empty shell.
            if meta and key != last_key:
                _rt = clean_youtube_title(meta.get("title") or "").lower().strip()
                _ra = (meta.get("artist") or "").lower().strip()
                if _rt in _USELESS_TITLES and (not _ra or _ra in _USELESS_TITLES):
                    if last_key and (entries or plain_lyrics):
                        key = last_key  # ignore the blip
                elif not _ra and _rt not in _USELESS_TITLES:
                    # Real-looking title but artist hasn't landed yet
                    # (browser MPRIS often reports title a beat before
                    # artist on track change). Fetching now means an
                    # artist-less, title-only lrclib search — the exact
                    # path that used to return an unrelated song. Give
                    # it up to ~2s to fill in before we key off it;
                    # meanwhile keep showing whatever's on screen. Don't
                    # wait forever — some sources genuinely never carry
                    # an artist tag, and this shouldn't freeze on those.
                    now = time.monotonic()
                    if no_artist_since is None:
                        no_artist_since = now
                    if now - no_artist_since < 2.0 and last_key and (entries or plain_lyrics):
                        key = last_key
                    else:
                        no_artist_since = None
                else:
                    no_artist_since = None

            if key != last_key:
                if shazam_active:
                    # Already in Shazam mode — a new MPRIS track key (next
                    # video/song in a playlist or mashup, OR simply the
                    # outer loop re-acquiring the player after a transient
                    # "Stopped" blip) shouldn't yank us back to plain-title
                    # mode on its own. But it DOES mean the previously
                    # identified song is very likely wrong now — a
                    # SongRec object isn't available yet here (Live hasn't
                    # started), so just flag it and actually re-listen
                    # right after entering the Live block below. This used
                    # to just silently swallow the key change and leave
                    # the stale identified song on screen indefinitely —
                    # which, since Chromium's MPRIS often reports a
                    # transient "Stopped" that bounces us out to this
                    # exact branch, was actually the most common way
                    # lyrics got stuck on a previous song.
                    last_key = key
                    pending_key = None
                    pending_key_since = None
                    pending_shazam_reidentify = True
                    pending_auto_shazam = False
                else:
                    last_key = key
                    pending_key = None
                    pending_key_since = None
                    a, t = resolve_track_info(meta)
                    artist, title, duration = a, t, meta["duration"]
                    album = meta["album"]

                    entries, plain_lyrics, _, word_timings = load_lyrics_with_words(
                        artist, title, meta["album"], duration
                    )
                    song_sec_per_syllable = _calibrate_song_tempo(entries)

                    cover_url = _pick_cover_url(meta)
                    cover_path = fetch_cover(cover_url) if COVER_ENABLED else None
                    last_cover_url = cover_url
                    extract_cover_accent(cover_path)  # mood accent from art

                    global_offset = offset_for_track(offset_cache, artist, title)
                    translations = None
                    romanized = None

                    if COVER_ENABLED:
                        # TEMPORARY debug — logged to a file instead of a
                        # transient on-screen print (which was too easy to
                        # miss with a screenshot), so it can be reviewed
                        # after the fact with `cat`. Appends one entry per
                        # normal-mode track change.
                        try:
                            with open(CACHE_DIR / "cover_debug.log", "a") as f:
                                f.write(
                                    f"{time.strftime('%H:%M:%S')} "
                                    f"title={title!r} "
                                    f"artUrl={meta.get('art_url') or '(empty)'!r} "
                                    f"trackUrl={meta.get('track_url') or '(empty)'!r} "
                                    f"derived={cover_url or '(none)'!r} "
                                    f"cover_path={cover_path!r}\n"
                                )
                        except Exception:
                            pass

                    shazam_active = False
                    shazam_pos = 0.0
                    next_auto_reload = 0.0
                    shazam_auto_fail_streak = 0
                    # Auto-identify: long mix / compilation titles never
                    # match lrclib — jump straight into SongRec once the
                    # Live UI is up (needs songrec on PATH).
                    pending_auto_shazam = (
                        not entries
                        and not (plain_lyrics or "").strip()
                        and looks_like_compilation(title, duration, has_lyrics=False)
                    )

            # inner loop: keep redrawing full-screen while the same track is
            # loaded (Playing or Paused) — lyrics freeze in place on pause.
            # Full player/track-change scans are relatively expensive
            # (playerctl -l + per-player status), so they only run every
            # FULL_CHECK_INTERVAL; position/status polling in between is
            # cheap and runs every POLL_INTERVAL for a smooth wipe.
            with Live(refresh_per_second=20, screen=True, auto_refresh=False) as live:
                cur_player = player
                cur_status = _status or "Playing"
                last_logged_status = cur_status
                last_full_check = time.time()
                last_frame_time = time.time()
                pos_clock = SmoothClock()
                pos_clock_player = None
                last_pos_poll = 0.0
                last_status_poll = 0.0
                songrec_available = shutil.which("songrec") is not None

                # Placed once per fresh alt-screen buffer, not every frame —
                # Kitty's graphics layer keeps the image in place as long as
                # the panel (rendered narrower + shifted right below) never
                # draws over its cell region.
                if cover_path:
                    show_cover(cover_path)
                else:
                    clear_cover()
                cover_offset = COVER_GUTTER if cover_path else 0

                if pending_auto_shazam and songrec_available and not shazam_active:
                    pending_auto_shazam = False
                    result = _do_shazam_listen(live, auto=True)
                    last_frame_time = time.time()
                    next_auto_reload = time.time() + SHAZAM_AUTO_RELOAD_COOLDOWN
                    if result:
                        state = _apply_shazam_result(result, cover_path, cover_offset, offset_cache)
                        artist, title = state["artist"], state["title"]
                        entries = state["entries"]
                        plain_lyrics = state["plain_lyrics"]
                        album = state["album"]
                        duration = state["duration"]
                        shazam_active = state["shazam_active"]
                        shazam_pos = state["shazam_pos"]
                        shazam_hold_floor = shazam_pos
                        shazam_mpris_key = last_key or cur_key or shazam_mpris_key
                        cover_path = state["cover_path"]
                        cover_offset = state["cover_offset"]
                        shazam_auto_fail_streak = 1 if state["implausible"] else 0
                        global_offset = state["sync_offset"]
                        word_timings = state.get("word_timings")
                        song_sec_per_syllable = state.get("song_sec_per_syllable")
                        translations = None
                        romanized = None
                    # if no match, fall through — empty state + hint stays

                if pending_shazam_reidentify:
                    pending_shazam_reidentify = False
                    result = _do_shazam_listen(live, auto=True)
                    if result is None:
                        # Same reasoning as the full_check path: a real
                        # track-key change is a strong signal, worth one
                        # immediate extra try rather than leaving the
                        # previous (now wrong) song's lyrics on screen.
                        result = _do_shazam_listen(live, auto=True)
                    last_frame_time = time.time()
                    next_auto_reload = time.time() + SHAZAM_AUTO_RELOAD_COOLDOWN
                    if result:
                        state = _apply_shazam_result(result, cover_path, cover_offset, offset_cache)
                        artist, title = state["artist"], state["title"]
                        entries = state["entries"]
                        plain_lyrics = state["plain_lyrics"]
                        album = state["album"]
                        duration = state["duration"]
                        shazam_active = state["shazam_active"]
                        shazam_pos = state["shazam_pos"]
                        shazam_hold_floor = shazam_pos
                        shazam_mpris_key = last_key or cur_key or shazam_mpris_key
                        cover_path = state["cover_path"]
                        cover_offset = state["cover_offset"]
                        shazam_auto_fail_streak = 1 if state["implausible"] else 0
                        global_offset = state["sync_offset"]
                        word_timings = state.get("word_timings")
                        song_sec_per_syllable = state.get("song_sec_per_syllable")
                        translations = None
                        romanized = None
                    else:
                        # Both tries came back empty — don't leave the
                        # previous (now wrong) song's lyrics on screen.
                        artist = title = ""
                        entries = []
                        duration = 0
                        shazam_pos = 0.0
                        word_timings = None
                        song_sec_per_syllable = None
                        shazam_auto_fail_streak = 2

                last_cover_size = console.size

                while True:
                    now = time.time()
                    dt = now - last_frame_time
                    last_frame_time = now

                    if now - last_full_check >= FULL_CHECK_INTERVAL:
                        last_full_check = now
                        cur_player, cur_status = find_active_player(locked_player)
                        if cur_status != last_logged_status:
                            last_logged_status = cur_status
                            try:
                                with open(CACHE_DIR / "status_debug.log", "a") as f:
                                    f.write(
                                        f"{time.strftime('%H:%M:%S')} "
                                        f"source=full_check player={cur_player!r} "
                                        f"status={cur_status!r} shazam_active={shazam_active} "
                                        f"shazam_pos={shazam_pos:.1f}\n"
                                    )
                            except Exception:
                                pass
                        locked_player = cur_player
                        if not cur_player:
                            break

                        cur_key, cur_meta = _track_key(cur_player)
                        if cur_key is None:
                            break  # player has no track info at all -> back to outer loop

                        raw_cur_key = cur_key
                        _meta_title = clean_youtube_title(
                            (cur_meta or {}).get("title") or ""
                        ).lower().strip()
                        _meta_artist = ((cur_meta or {}).get("artist") or "").lower().strip()
                        is_placeholder_meta = (
                            _meta_title in _USELESS_TITLES
                            and (not _meta_artist or _meta_artist in _USELESS_TITLES)
                        )
                        if is_placeholder_meta and last_key is not None:
                            # This is the exact YT Music/Brave quirk: the tab
                            # title flaps between the real song ("Iqro'") and
                            # the generic idle title ("YouTube Music") while
                            # paused — both sides persist long enough to beat
                            # any reasonable debounce, so debouncing alone
                            # can't fix it. A placeholder title with no real
                            # artist is never trustworthy on its own — just
                            # keep whatever track we already had loaded.
                            cur_key = last_key
                        elif raw_cur_key != last_key and not shazam_active:
                            # Debounce: some players (YT Music via Brave in
                            # particular) briefly mutate the tab/media title
                            # around a pause/play toggle without the track
                            # actually changing. Reacting to that instantly
                            # used to force a full lyrics reload on every
                            # such blip — new `entries` array, different
                            # line offsets at the same frozen `pos` — which
                            # is what showed up as the display flickering/
                            # resetting while genuinely paused. Only trust
                            # a new key once it's stuck around for a bit.
                            now_key_check = time.time()
                            if pending_key != raw_cur_key:
                                pending_key = raw_cur_key
                                pending_key_since = now_key_check
                                cur_key = last_key
                            elif now_key_check - pending_key_since < KEY_CHANGE_DEBOUNCE:
                                cur_key = last_key
                            # else: persisted long enough — trust it as real
                        else:
                            pending_key = None
                            pending_key_since = None

                        if cur_key != last_key:
                            if shazam_active:
                                # Decide if the user switched to a *different*
                                # video/file, or Chrome just blipped the
                                # title on the same page.
                                new_url = (cur_meta or {}).get("track_url") or ""
                                new_art = (cur_meta or {}).get("art_url") or ""
                                new_title = clean_youtube_title(
                                    (cur_meta or {}).get("title") or ""
                                ).lower().strip()
                                useless = new_title in _USELESS_TITLES or not new_title

                                media_changed = False
                                if new_url and shazam_media_url and new_url != shazam_media_url:
                                    media_changed = True
                                elif new_art and shazam_media_art and new_art != shazam_media_art:
                                    # plasma-browser-integration often has
                                    # artUrl but empty track_url — art
                                    # change is a solid "new video" signal
                                    media_changed = True
                                elif not useless and shazam_mpris_key and cur_key != shazam_mpris_key:
                                    # No URL/art to compare (common on
                                    # Chrome). A real video switch changes
                                    # the MPRIS key substantially; mid-song
                                    # blips go to useless titles which we
                                    # already filter above.
                                    # Require the key to look different
                                    # enough: not a pure case/whitespace
                                    # tweak of the previous one.
                                    prev = shazam_mpris_key.lower().strip()
                                    nowk = (cur_key or "").lower().strip()
                                    if prev and nowk and prev != nowk:
                                        # same-video title noise is usually
                                        # tiny; a different video has a
                                        # clearly different key
                                        media_changed = True

                                if not media_changed:
                                    # Learn fingerprints if we didn't have
                                    # them yet; swallow the key change.
                                    if new_url and not shazam_media_url:
                                        shazam_media_url = new_url
                                    if new_art and not shazam_media_art:
                                        shazam_media_art = new_art
                                    if not useless and not shazam_mpris_key:
                                        shazam_mpris_key = cur_key
                                    last_key = cur_key
                                else:
                                    last_key = cur_key
                                    shazam_media_url = new_url or shazam_media_url
                                    shazam_media_art = new_art or ""
                                    shazam_mpris_key = cur_key if not useless else ""
                                    shazam_auto_fail_streak = 0
                                    result = _do_shazam_listen(live, auto=True)
                                    if result is None:
                                        result = _do_shazam_listen(live, auto=True)
                                    last_frame_time = time.time()
                                    dt = 0.0
                                    next_auto_reload = time.time() + SHAZAM_AUTO_RELOAD_COOLDOWN
                                    if result:
                                        state = _apply_shazam_result(result, cover_path, cover_offset, offset_cache)
                                        artist, title = state["artist"], state["title"]
                                        entries = state["entries"]
                                        plain_lyrics = state["plain_lyrics"]
                                        album = state["album"]
                                        duration = state["duration"]
                                        shazam_active = state["shazam_active"]
                                        shazam_pos = state["shazam_pos"]
                                        shazam_hold_floor = shazam_pos
                                        shazam_mpris_key = last_key or cur_key or shazam_mpris_key
                                        cover_path = state["cover_path"]
                                        cover_offset = state["cover_offset"]
                                        shazam_auto_fail_streak = 1 if state["implausible"] else 0
                                        global_offset = state["sync_offset"]
                                        word_timings = state.get("word_timings")
                                        song_sec_per_syllable = state.get("song_sec_per_syllable")
                                        translations = None
                                        romanized = None
                                    else:
                                        artist = title = ""
                                        entries = []
                                        duration = 0
                                        shazam_pos = 0.0
                                        word_timings = None
                                        song_sec_per_syllable = None
                                        shazam_auto_fail_streak = 2
                            else:
                                try:
                                    with open(CACHE_DIR / "frame_debug.log", "a") as _f:
                                        _f.write(
                                            f"{time.strftime('%H:%M:%S.%f')[:-3]} "
                                            f"RELOAD raw_cur_key={raw_cur_key!r} "
                                            f"cur_key={cur_key!r} last_key={last_key!r} "
                                            f"player={cur_player!r}\n"
                                        )
                                except Exception:
                                    pass
                                break  # track changed -> back to outer loop to load its info
                        elif not shazam_active and not is_placeholder_meta:
                            # Track key (artist+title text) hasn't changed,
                            # but for mashup/compilation videos it never
                            # does across the whole video — so check
                            # separately whether the underlying artUrl
                            # actually moved on to a new song's art. Cheap:
                            # just a string compare unless it actually
                            # differs, in which case fetch_cover does the
                            # real work (and its own file:///http(s) cache
                            # rules still apply on top of this).
                            #
                            # Skipped entirely when is_placeholder_meta is
                            # True: that's the same tab-title-flap tick
                            # (e.g. Brave/YT Music briefly reporting the
                            # generic "YouTube Music" title) that made us
                            # keep cur_key == last_key above. On that same
                            # tick, art_url in cur_meta is also blank/stale
                            # placeholder data — trusting it here used to
                            # clear the cover art permanently (last_cover_url
                            # got set to "" with no real art change to undo
                            # it), even though the guard above already
                            # stopped the lyrics from resetting.
                            #
                            # cur_meta here already went through
                            # _enrich_from_plasma inside _track_key, so
                            # art_url/track_url are filled in the same way
                            # the initial load fills them — no separate
                            # fallback needed at this check.
                            new_cover_url = _pick_cover_url(cur_meta)
                            if new_cover_url != last_cover_url:
                                last_cover_url = new_cover_url
                                new_cover = fetch_cover(new_cover_url) if COVER_ENABLED else None
                                if new_cover:
                                    cover_path = new_cover
                                    show_cover(cover_path)
                                    cover_offset = COVER_GUTTER
                                    extract_cover_accent(cover_path)
                                elif COVER_ENABLED:
                                    clear_cover()
                                    cover_path = None
                                    cover_offset = 0
                                    extract_cover_accent(None)
                    else:
                        s = None
                        if time.monotonic() - last_status_poll >= POS_POLL_INTERVAL:
                            last_status_poll = time.monotonic()
                            s = playerctl("status", player=cur_player)
                        if s:
                            cur_status = s
                        if cur_status != last_logged_status:
                            last_logged_status = cur_status
                            try:
                                with open(CACHE_DIR / "status_debug.log", "a") as f:
                                    f.write(
                                        f"{time.strftime('%H:%M:%S')} "
                                        f"source=poll player={cur_player!r} "
                                        f"status={cur_status!r} raw={s!r} "
                                        f"shazam_active={shazam_active} "
                                        f"shazam_pos={shazam_pos:.1f}\n"
                                    )
                            except Exception:
                                pass

                    reidentify_soon = False
                    auto_paused = False

                    if shazam_active:
                        if cur_status == "Playing":
                            shazam_pos += dt
                        pos = shazam_pos

                        # Threshold for "this song is over": take the
                        # *later* of (duration + end grace) and
                        # (last LRC line + line grace). Using max stops
                        # both "duration too short" early rescans and
                        # "last line early, long outro" premature cuts.
                        cands = []
                        if duration > 0:
                            cands.append(duration + SHAZAM_AUTO_RELOAD_END_GRACE)
                        if entries:
                            cands.append(entries[-1][0] + SHAZAM_AUTO_RELOAD_LINE_GRACE)
                        auto_reload_threshold = max(cands) if cands else None

                        # Also require a minimum hold after identify so a
                        # bogus short duration can't loop SongRec at 0:20.
                        held_long_enough = pos >= (shazam_hold_floor + SHAZAM_AUTO_RELOAD_MIN_HOLD)

                        over_threshold = (
                            auto_reload_threshold is not None
                            and pos >= auto_reload_threshold
                            and held_long_enough
                        )
                        streak_capped = shazam_auto_fail_streak >= SHAZAM_AUTO_RELOAD_MAX_FAILURES

                        if over_threshold and streak_capped:
                            # Repeated no-match attempts (silence/ad gap) —
                            # stop auto-retrying and let the hint below
                            # tell the user to press 's' manually instead.
                            auto_paused = True
                        elif (over_threshold and cur_status == "Playing"
                                and now >= next_auto_reload):
                            result = _do_shazam_listen(live, auto=True)
                            last_frame_time = time.time()
                            next_auto_reload = time.time() + SHAZAM_AUTO_RELOAD_COOLDOWN
                            if result:
                                state = _apply_shazam_result(result, cover_path, cover_offset, offset_cache)
                                artist, title = state["artist"], state["title"]
                                entries = state["entries"]
                                plain_lyrics = state["plain_lyrics"]
                                album = state["album"]
                                duration = state["duration"]
                                shazam_active = state["shazam_active"]
                                shazam_pos = state["shazam_pos"]
                                shazam_hold_floor = shazam_pos
                                shazam_mpris_key = last_key or cur_key or shazam_mpris_key
                                cover_path = state["cover_path"]
                                cover_offset = state["cover_offset"]
                                if state["implausible"]:
                                    shazam_auto_fail_streak += 1
                                else:
                                    shazam_auto_fail_streak = 0
                                global_offset = state["sync_offset"]
                                word_timings = state.get("word_timings")
                                song_sec_per_syllable = state.get("song_sec_per_syllable")
                                translations = None
                                romanized = None
                            else:
                                shazam_auto_fail_streak += 1
                            pos = shazam_pos
                        elif (auto_reload_threshold is not None and not streak_capped
                                and held_long_enough
                                and auto_reload_threshold - SHAZAM_AUTO_RELOAD_WARN_LEAD
                                    <= pos < auto_reload_threshold):
                            reidentify_soon = True
                    else:
                        _mono = time.monotonic()
                        if cur_player != pos_clock_player:
                            pos_clock_player = cur_player
                            pos_clock.reset()
                            last_pos_poll = 0.0
                        if _mono - last_pos_poll >= POS_POLL_INTERVAL:
                            last_pos_poll = _mono
                            pos_clock.feed(get_position(cur_player),
                                           cur_status == "Playing")
                        else:
                            _pl = (cur_status == "Playing"
                                   and pos_clock.last_raw is not None)
                            if _pl != pos_clock.playing:
                                # status flipped between polls: freeze/resume
                                # from the current estimate, no jump
                                pos_clock.base_pos = pos_clock.now_pos(_mono)
                                pos_clock.base_t = _mono
                                pos_clock.playing = _pl
                        pos = pos_clock.now_pos()

                    # Lightweight onset tracker: lazy-start + non-blocking poll
                    if onset_tracker is not None and karaoke_enabled and ONSET_ENABLED:
                        if not onset_tracker.alive:
                            onset_tracker.start()
                        onset_tracker.poll()
                        # smooth vocal intensity for accent pulse
                        # (slightly snappier than before so chorus hits land faster).
                        # Only fed from live RMS while actually Playing — while
                        # paused the monitor is still capturing ambient/room
                        # noise, and feeding that in kept the accent color
                        # twitching every frame instead of settling down.
                        global _VOCAL_INTENSITY
                        if cur_status == "Playing":
                            floor = max(ONSET_SILENCE_RMS * 3.0, 0.05)
                            target_i = min(1.0, onset_tracker.last_rms / floor)
                            _VOCAL_INTENSITY = _VOCAL_INTENSITY * 0.72 + target_i * 0.28
                        else:
                            _VOCAL_INTENSITY *= 0.85
                        # refresh clock anchor when the active lyric line changes
                        sync_pos_tmp = pos + global_offset
                        idx_tmp = current_index(entries, sync_pos_tmp) if entries else -1
                        if idx_tmp != onset_anchor_idx:
                            onset_anchor_idx = idx_tmp
                            onset_anchor_wall = time.time()
                            onset_anchor_sync = sync_pos_tmp
                            voiced_acc = 0.0  # held-frac watermark for silence
                        # while audio is clearly present, track the current
                        # clock frac as the hold watermark so a later pause
                        # freezes the wipe there instead of at 0
                        if (cur_status == "Playing"
                                and onset_tracker.last_rms >= ONSET_SILENCE_RMS
                                and entries and idx_tmp >= 0):
                            try:
                                ls = entries[idx_tmp][0]
                                ns = (entries[idx_tmp + 1][0]
                                      if idx_tmp + 1 < len(entries)
                                      else ls + 4)
                                raw = ns - ls
                                west = _estimate_line_seconds(entries[idx_tmp][1] or "", song_sec_per_syllable)
                                wsp = min(raw, west) if raw > 0 else west
                                cf = max(0.0, min(1.0, (sync_pos_tmp + _lead_for(sync_pos_tmp - ls) - ls) / wsp))
                                voiced_acc = max(voiced_acc, cf)
                            except Exception:
                                pass
                    elif onset_tracker is not None and not ONSET_ENABLED and onset_tracker.alive:
                        onset_tracker.stop()
                        _VOCAL_INTENSITY = 0.0

                    size = console.size
                    if cover_path and size != last_cover_size:
                        # Window resized (or font zoomed) — the image was
                        # placed for the old cell geometry, so it now reads
                        # as shifted/warped. Clear + re-place it for Kitty
                        # to recompute against the current grid.
                        clear_cover()
                        show_cover(cover_path, term_height=size.height)
                    last_cover_size = size
                    show_zoom_hint = IN_KITTY and shutil.which("kitty") is not None
                    # genre is cheap after the first lookup (in-memory cache)
                    current_genre = fetch_genre(artist, title) if (artist or title) else ""
                    frame = render_frame(
                        artist, title, entries, pos, duration,
                        cur_status or "Playing", size.width, size.height,
                        zoom_hint=show_zoom_hint,
                        shazam_hint=songrec_available,
                        shazam_active=shazam_active,
                        cover_offset=cover_offset,
                        reidentify_soon=reidentify_soon,
                        auto_paused=auto_paused,
                        plain_lyrics=plain_lyrics,
                        karaoke_enabled=karaoke_enabled,
                        sync_offset=global_offset,
                        translations=translations,
                        translate_enabled=translate_enabled,
                        romanized=romanized,
                        romanize_enabled=romanize_enabled,
                        debug_karaoke=debug_karaoke,
                        album=album,
                        word_timings=word_timings,
                        song_sec_per_syllable=song_sec_per_syllable,
                        onset_tracker=onset_tracker,
                        onset_anchor_wall=onset_anchor_wall,
                        onset_anchor_sync=onset_anchor_sync,
                        voiced_acc=voiced_acc,
                        source_player=cur_player,
                        genre=current_genre,
                    )
                    live.update(frame, refresh=True)

                    heal_kitty_zoom(zoom_steps)
                    ch = poll_key(POLL_INTERVAL)
                    if ch in ("+", "="):
                        zoom_steps = zoom_by(1, zoom_steps)
                    elif ch in ("-", "_"):
                        zoom_steps = zoom_by(-1, zoom_steps)
                    elif ch == " ":
                        # Toggle play/pause on the locked player
                        if cur_player:
                            playerctl("play-pause", player=cur_player)
                    elif ch in ("p", "P"):
                        # Cycle MPRIS player — only when >1 exist. Prefer
                        # Playing players in the rotation so we don't land
                        # on a dead/paused tab and show garbage metadata
                        # like title="YouTube".
                        listed = playerctl("-l") or ""
                        all_p = [x.strip() for x in listed.splitlines() if x.strip()]
                        if len(all_p) <= 1:
                            # nothing to cycle — stay put, no reload glitch
                            pass
                        else:
                            playing_p = []
                            other_p = []
                            for p in all_p:
                                st = playerctl("status", player=p)
                                if st == "Playing":
                                    playing_p.append(p)
                                else:
                                    other_p.append(p)
                            # rotate within Playing first; if only one
                            # Playing, include paused so user can still switch
                            pool = playing_p if len(playing_p) > 1 else (playing_p + other_p)
                            if not pool:
                                pool = all_p
                            try:
                                i = pool.index(cur_player) if cur_player in pool else -1
                            except ValueError:
                                i = -1
                            nxt = pool[(i + 1) % len(pool)]
                            if nxt != cur_player:
                                locked_player = nxt
                                last_key = None
                                break
                    elif ch in ("k", "K"):
                        karaoke_enabled = not karaoke_enabled
                    elif ch in ("d", "D"):
                        # Dumps the raw wipe-timing numbers (idx, raw_span,
                        # wipe_span, frac, ...) under the active line each
                        # frame — for tracking down cases where a specific
                        # line's karaoke color never advances, without
                        # guessing blind from a screenshot.
                        debug_karaoke = not debug_karaoke
                    elif ch in ("z", "Z"):
                        global ZEN_MODE
                        ZEN_MODE = not ZEN_MODE
                    elif ch in ("m", "M"):
                        # Manual lyrics search — override auto-fetch result
                        result = manual_lyrics_search(live, artist, title)
                        last_frame_time = time.time()
                        if result:
                            artist = result["artist"]
                            title = result["title"]
                            entries = result["entries"]
                            plain_lyrics = result["plain_lyrics"]
                            duration = result["duration"] or duration
                            word_timings = result.get("word_timings")
                            song_sec_per_syllable = result.get("song_sec_per_syllable")
                            global_offset = offset_for_track(offset_cache, artist, title)
                            translations = None
                            romanized = None
                            if cover_path and COVER_ENABLED:
                                show_cover(cover_path, term_height=console.size.height)
                    elif ch in ("f", "F") and entries:
                        # Smart re-fetch: try alternate lrclib matches
                        try:
                            live.stop()
                        except Exception:
                            pass
                        c = Console()
                        c.clear()
                        c.print(Text("  looking for alternate LRC…", style="dim"))
                        alts = smart_refetch_candidates(artist, title, duration, entries)
                        if not alts:
                            c.print(Text("  no alternatives found. enter to continue.", style=WARN_ACCENT))
                            _prompt_line("  › ")
                        else:
                            c.print(Text("  alternate matches:", style=f"bold {ACCENT}"))
                            c.print()
                            for i, res in enumerate(alts[:5]):
                                dur = res.get("duration") or 0
                                c.print(
                                    f"  {i + 1}. {res.get('artistName', '?')} — "
                                    f"{res.get('trackName', '?')}  [{dur}s]",
                                    style="white",
                                )
                            c.print()
                            c.print("  pick 1-5 · empty cancel", style="dim")
                            choice = _prompt_line("  › ")
                            if choice and choice.strip().isdigit():
                                idx = int(choice.strip()) - 1
                                if 0 <= idx < min(5, len(alts)):
                                    pick = alts[idx]
                                    a = pick.get("artistName") or artist
                                    t = pick.get("trackName") or title
                                    entries, plain_lyrics, lrc_d, word_timings = load_lyrics_with_words(
                                        a, t, pick.get("albumName") or "", pick.get("duration") or 0,
                                    )
                                    if not entries and pick.get("syncedLyrics"):
                                        entries = parse_lrc(pick["syncedLyrics"])
                                        plain_lyrics = plain_lyrics or (pick.get("plainLyrics") or "")
                                        lrc_d = pick.get("duration") or 0
                                        word_timings = None
                                    artist, title = a, t
                                    if lrc_d:
                                        duration = lrc_d
                                    song_sec_per_syllable = _calibrate_song_tempo(entries)
                                    translations = None
                                    romanized = None
                        try:
                            live.start()
                        except Exception:
                            pass
                        if cover_path and COVER_ENABLED:
                            show_cover(cover_path, term_height=console.size.height)
                        last_frame_time = time.time()
                    elif ch == "[":
                        # Lyrics running ahead of the vocal — pull sync
                        # earlier by lowering the offset. Persisted per
                        # track so this correction is remembered next
                        # time this song plays.
                        global_offset -= GLOBAL_OFFSET_STEP
                        save_offset_for_track(offset_cache, artist, title, global_offset)
                    elif ch == "]":
                        # Lyrics lagging behind the vocal — push sync
                        # later by raising the offset.
                        global_offset += GLOBAL_OFFSET_STEP
                        save_offset_for_track(offset_cache, artist, title, global_offset)
                    elif ch in ("r", "R"):
                        # Reset the sync offset back to 0 — separate from
                        # 'b' (which exits Shazam mode), this only clears
                        # the manual lyric-timing correction.
                        global_offset = 0.0
                        save_offset_for_track(offset_cache, artist, title, global_offset)
                    elif ch in ("t", "T") and entries:
                        # Toggle translation of the active line. Only
                        # fetches on the transition into "on" for a track
                        # that hasn't been translated yet this session —
                        # flipping it off/on again just reuses whatever's
                        # already in `translations` (or the on-disk cache
                        # from fetch_translation's own caching).
                        translate_enabled = not translate_enabled
                        if translate_enabled and translations is None:
                            translations = fetch_translation(artist, title, entries)
                    elif ch in ("y", "Y") and entries and HAVE_PYKAKASI:
                        # Toggle romanization of the active line — same
                        # lazy-compute-once pattern as translate above,
                        # just no disk cache since pykakasi conversion is
                        # local and fast enough to redo per-track.
                        romanize_enabled = not romanize_enabled
                        if romanize_enabled and romanized is None:
                            romanized = romanize_lyrics(entries)
                    elif ch in ("?", "/"):
                        # Like the history panel this blocks while open and
                        # deliberately does NOT reset last_frame_time, so
                        # Shazam-mode position absorbs the time spent here.
                        show_help(live, songrec_available)
                        if cover_path and COVER_ENABLED: show_cover(cover_path, term_height=console.size.height)
                    elif ch in ("h", "H") and songrec_available:
                        # Deliberately NOT resetting last_frame_time here.
                        # show_shazam_history() blocks for however long the
                        # person spends looking at the panel, and in
                        # Shazam mode `pos` is estimated by accumulating
                        # wall-clock dt each frame (there's no real MPRIS
                        # position for the sub-song within a mashup) — the
                        # song keeps playing for real the whole time we're
                        # blocked. Leaving last_frame_time untouched means
                        # the next frame's dt naturally includes that gap,
                        # so shazam_pos jumps back in sync instead of
                        # permanently lagging behind by however long the
                        # history view was open (resetting it here was the
                        # bug: it silently erased that elapsed time).
                        show_shazam_history(live)
                        if cover_path and COVER_ENABLED: show_cover(cover_path, term_height=console.size.height)
                    elif ch in ("s", "S"):
                        # Manual re-identify — always listens again, even
                        # while already in Shazam mode (e.g. the current
                        # identify was wrong, or you just want a fresh
                        # read). 'b' is the dedicated key for dropping
                        # back to plain MPRIS title/artist.
                        result = _do_shazam_listen(live)
                        last_frame_time = time.time()
                        next_auto_reload = time.time() + SHAZAM_AUTO_RELOAD_COOLDOWN
                        shazam_auto_fail_streak = 0
                        if result:
                            state = _apply_shazam_result(result, cover_path, cover_offset, offset_cache)
                            artist, title = state["artist"], state["title"]
                            entries = state["entries"]
                            plain_lyrics = state["plain_lyrics"]
                            album = state["album"]
                            duration = state["duration"]
                            shazam_active = state["shazam_active"]
                            shazam_pos = state["shazam_pos"]
                            shazam_hold_floor = shazam_pos
                            shazam_mpris_key = last_key or cur_key or shazam_mpris_key
                            cover_path = state["cover_path"]
                            cover_offset = state["cover_offset"]
                            global_offset = state["sync_offset"]
                            word_timings = state.get("word_timings")
                            song_sec_per_syllable = state.get("song_sec_per_syllable")
                            translations = None
                            romanized = None
                    elif ch in ("b", "B") and shazam_active:
                        # Drop back to plain MPRIS title/artist/duration —
                        # the only way out of Shazam mode now that 's' is
                        # purely "re-identify" again.
                        state = _load_normal_track(cur_player, cover_path, cover_offset, offset_cache)
                        if state:
                            artist, title, duration = state["artist"], state["title"], state["duration"]
                            entries = state["entries"]
                            plain_lyrics = state["plain_lyrics"]
                            album = state["album"]
                            cover_path = state["cover_path"]
                            cover_offset = state["cover_offset"]
                            last_cover_url = state["cover_url"]
                            global_offset = state["sync_offset"]
                            word_timings = state.get("word_timings")
                            song_sec_per_syllable = state.get("song_sec_per_syllable")
                            translations = None
                            romanized = None
                        shazam_active = False
                        shazam_pos = 0.0
                        next_auto_reload = 0.0
                        shazam_auto_fail_streak = 0
                        last_frame_time = time.time()
                    elif ch in ("q", "Q", "esc"):
                        # Quit (q / Esc) — reuse the exact Ctrl+C path (unwinds the
                        # Live screen, then __main__ does sys.exit(0))
                        raise KeyboardInterrupt
                    elif ch in ("o", "O"):
                        # Settings panel — apply/save on Enter, discard on q/esc
                        show_settings(live, settings)
                        # live.stop()/clear wipes Kitty's graphics layer, so
                        # re-place the cover art if we had one showing
                        # (dock layout puts it at the bottom)
                        if not COVER_ENABLED:
                            # artwork switched off in settings
                            cover_path = None
                            cover_offset = 0
                        else:
                            if cover_path is None and last_cover_url:
                                # switched back on: fetch art for the current track
                                cover_path = fetch_cover(last_cover_url)
                                extract_cover_accent(cover_path)
                            if cover_path:
                                show_cover(cover_path, term_height=console.size.height)
                                cover_offset = COVER_GUTTER
                        # if onset was just disabled, stop the capture process
                        if not ONSET_ENABLED and onset_tracker is not None:
                            onset_tracker.stop()
                        last_frame_time = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
