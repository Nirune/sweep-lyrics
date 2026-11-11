#!/usr/bin/env python3
"""lyrics_providers.py - word-synced lyrics for Sweep, same sources Dux uses.

Providers (default priority, same as Dux): Better Lyrics, Unison, LyricsPlus,
KuGou, LRCLIB, YouTube Music.  Request and response shapes were read from the
MIT-licensed dux-go lyrics package; the code here is an independent rewrite.

How a result is picked (modelled on the Dux README):
  * every enabled provider is asked at the same time, 12 s overall
  * word sync beats line sync beats plain text, then config priority decides
  * a word result wins the moment it lands (a better-ranked word result arriving later only updates the cache)
  * the first hit is held up to 0.4 s in case better sync shows up (later ones swap in live)
  * a "synced" result whose stamps are all zero counts as a miss

Output is enhanced LRC: [mm:ss.xx]<mm:ss.xx>word <mm:ss.xx>word<mm:ss.xx>
Syllables are merged into whole words, background vocals become "(...)".

Library use:
    import lyrics_providers
    res = lyrics_providers.fetch(title, artist, album, duration_seconds)
    if res and res.word_level: use(res.lrc)

CLI:
    lyrics_providers.py TITLE ARTIST [--album A] [--duration SEC] [--video-id ID]
        --probe        ask every provider on its own, show timing + result
        --raw          with --probe: dump the first bytes of each raw response
        --no-cache     skip the on-disk cache
        --lrc          print the whole LRC instead of the first lines
    lyrics_providers.py --list
    lyrics_providers.py --order betterlyrics,lyricsplus,lrclib   (unlisted = off)
    lyrics_providers.py --enable kugou | --disable kugou

Files:
    ~/.config/sweep-lyrics/lyrics-providers.json      order + enabled providers
    ~/.cache/sweep-lyrics/providers/           per-song result cache
    ~/.cache/sweep-lyrics/providers-debug.log  one line per provider per lookup
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.client
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

UA = "sweep-lyrics/2"
OVERALL_TIMEOUT = 12.0   # whole lookup
HOLD_SECONDS = 0.4       # how long the first hit waits for a better one (late winners swap in live)
DURATION_TOL = 3         # seconds, KuGou / LRCLIB candidate matching
MAX_BODY = 4 << 20
MISS_CODES = {400, 401, 404, 410, 422}  # Better Lyrics answers 401 for "not cached yet"
CONFIG_VERSION = 2

_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
CONFIG_PATH = _XDG_CONFIG / "sweep-lyrics" / "lyrics-providers.json"
CACHE_DIR = _XDG_CACHE / "sweep-lyrics" / "providers"
LOG_PATH = _XDG_CACHE / "sweep-lyrics" / "providers-debug.log"

RANK = {"word": 3, "line": 2, "plain": 1}
# Marks a <mm:ss.xx> tag as another syllable of the same word (zero width joiner, invisible).
# Sweep's parse_enhanced_lrc reads it; sources that never emit it are parsed as before.
SYL_JOIN = "\u200d"
RAW = False


class Miss(Exception):
    """Provider answered, but has nothing usable for this track."""


class Skip(Exception):
    """Provider cannot run here (missing dependency or input)."""


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class Unit:
    start: float
    end: float
    text: str
    bg: bool = False


@dataclass
class Word:
    start: float
    end: float
    text: str
    bg: bool = False
    syl: list = field(default_factory=list)  # (start, end, char_from, char_to) per syllable


@dataclass
class Line:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    timed: bool = True


@dataclass
class Parsed:
    lines: list[Line]
    plain: str = ""

    def level(self) -> str | None:
        ls = [ln for ln in self.lines if ln.text]
        if not ls:
            return "plain" if self.plain.strip() else None
        timed = [ln for ln in ls if ln.timed]
        if len(timed) * 2 < len(ls):
            return "plain"
        multi = sum(1 for ln in timed if sum(len(w.syl) for w in ln.words) > 1)
        with_words = sum(1 for ln in timed if ln.words)
        if multi and with_words * 2 >= len(timed):
            return "word"
        return "line"


@dataclass
class Result:
    lrc: str
    level: str
    source: str
    lines: int
    cached: bool = False

    @property
    def word_level(self) -> bool:
        return self.level == "word"

    @property
    def synced(self) -> bool:
        return self.level in ("word", "line")


@dataclass
class Req:
    title: str
    artist: str
    album: str = ""
    duration: float = 0.0
    video_id: str = ""

    @classmethod
    def make(cls, title, artist, album=None, duration=None, video_id=None) -> "Req":
        dur = float(duration or 0)
        if dur > 10000:  # caller passed milliseconds
            dur /= 1000.0
        artist = re.sub(r"\s*-\s*Topic$", "", (artist or "").strip(), flags=re.I)
        return cls((title or "").strip(), artist, (album or "").strip(), max(dur, 0.0), video_id or "")

    @property
    def seconds(self) -> int:
        return int(round(self.duration)) if self.duration > 0 else 0

    def query(self, t: str, a: str, al: str, d: str) -> dict[str, str]:
        q = {t: self.title, a: self.artist}
        if self.album:
            q[al] = self.album
        if self.seconds:
            q[d] = str(self.seconds)
        return q


# --------------------------------------------------------------------------- #
# text helpers and matching
# --------------------------------------------------------------------------- #
_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT = re.compile(r"\b(feat|ft|featuring)\b.*$", re.I)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&|/|\bfeat\.?\b|\bft\.?\b|\bx\b|\band\b)\s*", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = _PAREN.sub(" ", s)
    s = _FEAT.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _similar(a: str, b: str, ratio: float) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 3 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= ratio


def title_ok(want: str, got: str) -> bool:
    return _similar(norm(want), norm(got), 0.82)


def artist_ok(want: str, got: str) -> bool:
    if not want.strip():
        return True
    cand = norm(got)
    return any(_similar(norm(x), cand, 0.75) for x in _ARTIST_SPLIT.split(want) if x.strip())


def dur_ok(want_sec: int, got_sec) -> bool:
    try:
        got = float(got_sec)
    except (TypeError, ValueError):
        return True
    return not want_sec or not got or abs(want_sec - got) <= DURATION_TOL


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
POOL_IDLE_TTL = 60.0      # seconds an idle keep-alive connection may be reused
POOL_MAX_IDLE = 4         # idle connections kept per host
MAX_REDIRECTS = 4
_POOL: dict[tuple[str, str], list[tuple[http.client.HTTPConnection, float]]] = {}
_POOL_LOCK = threading.Lock()
_raw_lock = threading.Lock()


def _new_conn(scheme: str, netloc: str, timeout: float) -> http.client.HTTPConnection:
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(netloc, timeout=timeout)


def _take_conn(scheme: str, netloc: str, timeout: float):
    """(conn, reused). Reuses a fresh idle connection, else makes a new one."""
    now = time.monotonic()
    with _POOL_LOCK:
        idle = _POOL.get((scheme, netloc), [])
        while idle:
            conn, t = idle.pop()
            if now - t <= POOL_IDLE_TTL and conn.sock is not None:
                conn.timeout = timeout
                conn.sock.settimeout(timeout)
                return conn, True
            conn.close()
    return _new_conn(scheme, netloc, timeout), False


def _give_conn(scheme: str, netloc: str, conn: http.client.HTTPConnection) -> None:
    with _POOL_LOCK:
        idle = _POOL.setdefault((scheme, netloc), [])
        if len(idle) >= POOL_MAX_IDLE:
            conn.close()
        else:
            idle.append((conn, time.monotonic()))


def _request(scheme: str, netloc: str, path: str, timeout: float):
    """One GET over a pooled connection -> (status, headers, body)."""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    for attempt in (0, 1):
        conn, reused = _take_conn(scheme, netloc, timeout)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read(MAX_BODY + 1)
        except TimeoutError:
            conn.close()
            raise
        except (http.client.HTTPException, OSError):
            conn.close()
            if reused and attempt == 0:  # idle connection the server already dropped: retry fresh
                continue
            raise
        if resp.will_close or not resp.isclosed() or len(body) > MAX_BODY:
            conn.close()
        else:
            _give_conn(scheme, netloc, conn)
        return resp.status, resp.headers, body
    raise OSError("unreachable")


def http_get(url: str, params: dict, timeout: float) -> bytes:
    # spaces must go out as %20, KuGou finds nothing for "a - b" when they are '+'
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    cur = f"{url}?{qs}"
    origin = urllib.parse.urlsplit(cur)[:2]
    for _ in range(MAX_REDIRECTS + 1):
        sp = urllib.parse.urlsplit(cur)
        if sp[:2] != origin:
            raise urllib.error.HTTPError(cur, 0, "refusing cross-origin redirect", None, None)
        path = (sp.path or "/") + (f"?{sp.query}" if sp.query else "")
        status, hdrs, body = _request(sp.scheme, sp.netloc, path, timeout)
        if status in (301, 302, 303, 307, 308) and hdrs.get("Location"):
            cur = urllib.parse.urljoin(cur, hdrs["Location"])
            continue
        break
    else:
        raise urllib.error.HTTPError(cur, 310, "too many redirects", None, None)
    if not 200 <= status < 300:
        if status in MISS_CODES:
            raise Miss(f"HTTP {status}")
        raise urllib.error.HTTPError(cur, status, http.client.responses.get(status, ""), hdrs, None)
    if len(body) > MAX_BODY:
        raise ValueError("response too large")
    if RAW:
        with _raw_lock:
            print(f"--- GET {url}?{qs}\n{body[:1500]!r}\n", file=sys.stderr)
    return body


def warm() -> None:
    """Open TCP+TLS to every provider host in the background so the next lookup skips the handshakes.
    Call it as soon as a track change is first noticed; hosts that already hold a fresh idle connection are skipped."""
    def one(scheme: str, netloc: str) -> None:
        now = time.monotonic()
        with _POOL_LOCK:
            if any(now - t <= POOL_IDLE_TTL for _, t in _POOL.get((scheme, netloc), [])):
                return
        try:
            conn = _new_conn(scheme, netloc, 4.0)
            conn.connect()
        except Exception:
            return
        _give_conn(scheme, netloc, conn)

    seen = set()
    for u in WARM_URLS:
        sp = urllib.parse.urlsplit(u)
        if (sp.scheme, sp.netloc) not in seen:
            seen.add((sp.scheme, sp.netloc))
            threading.Thread(target=one, args=(sp.scheme, sp.netloc), daemon=True).start()


# --------------------------------------------------------------------------- #
def get_json(url: str, params: dict, timeout: float):
    return json.loads(http_get(url, params, timeout))


# --------------------------------------------------------------------------- #
# units -> words -> lines
# --------------------------------------------------------------------------- #
def merge_units(units: list[Unit]) -> list[Word]:
    """Join syllables into whole words. A word ends where whitespace sits."""
    words: list[Word] = []
    cur: Word | None = None
    boundary = True
    for u in units:
        t = u.text
        if not t.strip():
            if t:
                boundary = True
            continue
        piece = t.strip()
        if cur is None or boundary or t[0].isspace() or cur.bg != u.bg:
            cur = Word(u.start, u.end, piece, u.bg, [(u.start, u.end, 0, len(piece))])
            words.append(cur)
        else:
            c0 = len(cur.text)
            cur.text += piece
            cur.end = max(cur.end, u.end)
            cur.syl.append((u.start, u.end, c0, len(cur.text)))
        boundary = t[-1].isspace()
    return words


def _bg_words(group: list[Word]) -> list[Word]:
    """Background vocal words stay separate words (each keeps its own timing); the first gets
    "(" and the last ")" unless the source already wrote them. Syllable offsets follow along."""
    first, last = group[0], group[-1]
    if not first.text.startswith("("):
        first.text = "(" + first.text
        first.syl = [(s0, s1, 0 if k == 0 else c0 + 1, c1 + 1) for k, (s0, s1, c0, c1) in enumerate(first.syl)]
    if not last.text.endswith(")"):
        last.text += ")"
        if last.syl:
            s0, s1, c0, c1 = last.syl[-1]
            last.syl[-1] = (s0, s1, c0, c1 + 1)
    return group


def line_from_units(units: list[Unit], begin: float | None = None, end: float | None = None) -> Line | None:
    words = merge_units(units)
    if not words:
        return None
    blocks: list[list[Word]] = []
    group: list[Word] = []
    for w in words:
        if w.bg:
            group.append(w)
            continue
        if group:
            blocks.append(_bg_words(group))
            group = []
        blocks.append([w])
    if group:
        blocks.append(_bg_words(group))
    blocks.sort(key=lambda blk: blk[0].start)  # stable; a background phrase stays in one piece
    items = [w for blk in blocks for w in blk]
    for w in items:
        if w.end < w.start:
            w.end = w.start
    start = items[0].start if begin is None else min(begin, items[0].start)
    stop = max(max(w.end for w in items), end or 0.0)
    return Line(start, stop, " ".join(w.text for w in items), items)


# --------------------------------------------------------------------------- #
# parsers: LRC (+enhanced), TTML, KRC, LyricsPlus JSON
# --------------------------------------------------------------------------- #
_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_WTAG = re.compile(r"<(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?>")
_LRC_LINE = re.compile(r"^\s*((?:\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+)(.*)$")
_OFFSET = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]", re.I | re.M)


def _stamp_secs(m: re.Match) -> float:
    frac = m.group(3)
    return int(m.group(1)) * 60 + int(m.group(2)) + (int(frac) / 10 ** len(frac) if frac else 0.0)


def _enhanced_units(rest: str, line_start: float) -> list[Unit]:
    rest = rest.replace(SYL_JOIN, "")
    segs: list[tuple[float, str]] = []
    pos, cur = 0, line_start
    for m in _WTAG.finditer(rest):
        segs.append((cur, rest[pos:m.start()]))
        cur, pos = _stamp_secs(m), m.end()
    segs.append((cur, rest[pos:]))
    units: list[Unit] = []
    for ts, txt in segs:
        if not txt.strip():  # an end marker like <mm:ss.xx>, maybe followed by a space
            last = next((u for u in reversed(units) if u.text.strip()), None)
            if last is not None and last.end <= last.start:
                last.end = max(ts, last.start)
            if txt:
                units.append(Unit(ts, ts, " "))
            continue
        units.append(Unit(ts, ts, txt))
    real = [u for u in units if u.text.strip()]
    for a, b in zip(real, real[1:]):
        if a.end <= a.start:
            a.end = b.start
    return units


def parse_lrc(data: bytes | str) -> Parsed:
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    text = text.lstrip("\ufeff")
    off = _OFFSET.search(text)
    shift = -int(off.group(1)) / 1000.0 if off else 0.0
    lines: list[Line] = []
    plain: list[str] = []
    for raw in text.splitlines():
        m = _LRC_LINE.match(raw)
        if not m:
            s = raw.strip()
            if s and not re.match(r"^\[[a-z]{2,}:", s, re.I):
                plain.append(s)
            continue
        stamps = [max(_stamp_secs(s) + shift, 0.0) for s in _STAMP.finditer(m.group(1))]
        rest = m.group(2)
        for start in stamps:
            if _WTAG.search(rest):
                ln = line_from_units(_enhanced_units(rest, start), begin=start)
                if ln:
                    lines.append(ln)
                    continue
            body = _WTAG.sub("", rest).strip()
            if body:
                lines.append(Line(start, start, body))
    lines.sort(key=lambda ln: ln.start)
    for a, b in zip(lines, lines[1:]):
        if a.end <= a.start:
            a.end = b.start
    if lines:
        return Parsed(lines, "\n".join(ln.text for ln in lines))
    return Parsed([], "\n".join(plain))


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _attr(el: ET.Element, name: str) -> str | None:
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def ttml_time(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", s)
    if m:
        mult = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[m.group(2)]
        return float(m.group(1)) * mult
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _ttml_units(el: ET.Element, bg: bool, units: list[Unit]) -> None:
    for child in el:
        if _local(child.tag) == "span":
            is_bg = bg or (_attr(child, "role") or "") == "x-bg"
            begin, end = ttml_time(_attr(child, "begin")), ttml_time(_attr(child, "end"))
            if begin is not None:
                text = "".join(child.itertext()) if len(child) == 0 else (child.text or "")
                units.append(Unit(begin, end if end is not None else begin, text, is_bg))
                if len(child):
                    _ttml_units(child, is_bg, units)
            else:
                _ttml_units(child, is_bg, units)
        if child.tail and child.tail.strip() == "" and child.tail != "":
            units.append(Unit(0.0, 0.0, " ", bg))


def parse_ttml(data: bytes | str) -> Parsed:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"bad TTML: {e}") from None
    lines: list[Line] = []
    plain: list[str] = []
    for p in root.iter():
        if _local(p.tag) != "p":
            continue
        begin, end = ttml_time(_attr(p, "begin")), ttml_time(_attr(p, "end"))
        units: list[Unit] = []
        _ttml_units(p, False, units)
        ln = line_from_units(units, begin, end) if any(u.text.strip() for u in units) else None
        if ln:
            lines.append(ln)
            continue
        text = re.sub(r"\s+", " ", "".join(p.itertext())).strip()
        if not text:
            continue
        plain.append(text)
        lines.append(Line(begin or 0.0, end or begin or 0.0, text, timed=begin is not None))
    lines.sort(key=lambda ln: ln.start)
    return Parsed(lines, "\n".join(ln.text for ln in lines))


# KuGou KRC: "krc1" + zlib data XORed with a fixed 16 byte key. Key copied from public
# KRC decoders; verify with: rg -n -A4 krcKey ~/src/dux-go/lyrics
KRC_KEY = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])
_KRC_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_KRC_WORD = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")


def decode_krc(data: bytes) -> Parsed:
    if not data.startswith(b"krc1"):
        raise ValueError("not a krc1 payload")
    body = data[4:]
    key = KRC_KEY
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(body))
    text = zlib.decompress(xored).decode("utf-8-sig", "replace")
    return parse_krc(text)


def parse_krc(text: str) -> Parsed:
    lines: list[Line] = []
    for raw in text.splitlines():
        m = _KRC_LINE.match(raw.strip())
        if not m:
            continue
        ls, ll = int(m.group(1)) / 1000.0, int(m.group(2)) / 1000.0
        units = [
            Unit(ls + int(w.group(1)) / 1000.0, ls + (int(w.group(1)) + int(w.group(2))) / 1000.0, html.unescape(w.group(3)))
            for w in _KRC_WORD.finditer(m.group(3))
        ]
        # The word offsets already give the exact line end (they match the header
        # duration on every normal line). KuGou sometimes puts the whole song
        # length in the last line's duration field instead — e.g. [200172,241686]
        # on a 4:01 track — which would make finalize() reject an otherwise good
        # word-level result as "lyrics run 442s, track is 240s".
        end = max((u.end for u in units), default=0.0) or ls + ll
        ln = line_from_units(units, ls, end)
        if ln:
            lines.append(ln)
    lines.sort(key=lambda ln: ln.start)
    return Parsed(lines, "\n".join(ln.text for ln in lines))


def _ms(v) -> float:
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def parse_lyricsplus(payload: dict) -> Parsed:
    lines: list[Line] = []
    for item in payload.get("lyrics") or []:
        start = _ms(item.get("time"))
        end = start + _ms(item.get("duration"))
        units = [
            Unit(_ms(s.get("time")), _ms(s.get("time")) + _ms(s.get("duration")), s.get("text") or "", bool(s.get("isBackground")))
            for s in (item.get("syllabus") or [])
        ]
        ln = line_from_units(units, start, end) if any(u.text.strip() for u in units) else None
        if ln is None:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            ln = Line(start, end, text)
        lines.append(ln)
    lines.sort(key=lambda ln: ln.start)
    return Parsed(lines, "\n".join(ln.text for ln in lines))


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def stamp(t: float) -> str:
    cs = int(round(max(t, 0.0) * 100))
    m, rem = divmod(cs, 6000)
    return f"{m:02d}:{rem // 100:02d}.{rem % 100:02d}"


def _word_tags(w: Word) -> str:
    """<start>text, or one tag per syllable; continuation syllables carry SYL_JOIN."""
    if len(w.syl) < 2:
        return f"<{stamp(w.start)}>{w.text}"
    out: list[str] = []
    for k, (s0, s1, c0, c1) in enumerate(w.syl):
        out.append(f"<{stamp(s0)}>{SYL_JOIN if k else ''}{w.text[c0:c1]}")
        nxt_start = w.syl[k + 1][0] if k + 1 < len(w.syl) else None
        if nxt_start is not None and s1 > s0 and abs(s1 - nxt_start) > 0.005:
            out.append(f"<{stamp(s1)}>")  # a gap before the next syllable: keep the real end
    return "".join(out)


def to_lrc(parsed: Parsed, level: str) -> str:
    if level == "plain":
        return "\n".join(ln.text for ln in parsed.lines if ln.text) or parsed.plain
    out: list[str] = []
    for ln in sorted((x for x in parsed.lines if x.text and x.timed), key=lambda x: x.start):
        head = f"[{stamp(ln.start)}]"
        if level == "word" and sum(len(w.syl) for w in ln.words) > 1:
            parts: list[str] = []
            for i, w in enumerate(ln.words):
                nxt = ln.words[i + 1] if i + 1 < len(ln.words) else None
                tag = _word_tags(w)
                if nxt is None or (w.end > w.start and abs(w.end - nxt.start) > 0.005):
                    tag += f"<{stamp(w.end)}>" if w.end > w.start else ""
                parts.append(tag)
            out.append(head + " ".join(parts))
        else:
            out.append(f"{head}{ln.text}")
    return "\n".join(out)


def finalize(parsed: Parsed, req: Req) -> Parsed:
    level = parsed.level()
    if level is None:
        raise Miss("empty")
    if level in ("word", "line"):
        timed = [ln for ln in parsed.lines if ln.timed and ln.text]
        if len(timed) > 1 and max(x.start for x in timed) - min(x.start for x in timed) < 0.01:
            raise Miss("sync claimed but every stamp is zero")
        if req.seconds:
            last = max(max(x.end, x.start) for x in timed)
            if last > req.seconds + 20:
                raise Miss(f"lyrics run {last:.0f}s, track is {req.seconds}s")
    return parsed


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
def fetch_betterlyrics(req: Req) -> Parsed:
    payload = get_json(BETTERLYRICS_URL, req.query("s", "a", "al", "d"), 4)
    ttml = payload.get("ttml") if isinstance(payload, dict) else None
    if not ttml:
        raise Miss("no ttml")
    return parse_ttml(ttml)


def fetch_unison(req: Req) -> Parsed:
    payload = get_json(UNISON_URL, req.query("song", "artist", "album", "duration"), 8)
    data = (payload.get("data") if isinstance(payload, dict) else None) or {}
    body = data.get("lyrics") or ""
    if not body.strip():
        raise Miss("no lyrics")
    return parse_ttml(body) if data.get("format") == "ttml" else parse_lrc(body)


def fetch_lyricsplus(req: Req) -> Parsed:
    payload = get_json(LYRICSPLUS_URL, req.query("title", "artist", "album", "duration"), 4)
    if not isinstance(payload, dict) or not payload.get("lyrics"):
        raise Miss("no lyrics")
    return parse_lyricsplus(payload)


KUGOU_SONGS = "https://mobileservice.kugou.com/api/v3/search/song"
KUGOU_SEARCH = "https://lyrics.kugou.com/search"
KUGOU_DOWNLOAD = "https://lyrics.kugou.com/download"
_CREDIT = re.compile(r"^[^:：]{1,12}[:：]\s*\S.{0,40}$")
_TITLE_SEP = re.compile(r"\s-+\s")


def _kugou_search(req: Req, keyword: str, song_hash: str = "") -> dict:
    params = {"ver": 1, "man": "yes", "client": "pc", "keyword": keyword}
    if req.seconds:
        params["duration"] = req.seconds * 1000
    if song_hash:
        params["hash"] = song_hash
    payload = get_json(KUGOU_SEARCH, params, 4)
    best, best_gap = None, None
    for c in payload.get("candidates") or []:
        secs = (c.get("duration") or 0) / 1000.0
        if not (title_ok(req.title, c.get("song", "")) and artist_ok(req.artist, c.get("singer", ""))):
            continue
        if secs and not dur_ok(req.seconds, secs):
            continue
        gap = abs(secs - req.seconds) if secs and req.seconds else 0
        if best is None or gap < best_gap:
            best, best_gap = c, gap
    if best is None:
        raise Miss("no matching candidate")
    return best


def _kugou_candidate(req: Req) -> dict:
    keyword = f"{req.artist} - {req.title}" if req.artist else req.title
    try:
        songs = get_json(KUGOU_SONGS, {"format": "json", "keyword": keyword, "page": 1, "pagesize": 8, "showtype": 1}, 4)
        infos = ((songs.get("data") or {}).get("info")) or []
    except Miss:
        infos = []
    matched = [
        s for s in infos
        if s.get("hash") and dur_ok(req.seconds, s.get("duration"))
        and title_ok(req.title, s.get("songname", "")) and artist_ok(req.artist, s.get("singername", ""))
    ]
    matched.sort(key=lambda s: abs((s.get("duration") or 0) - req.seconds) if req.seconds else 0)
    for s in matched[:3]:
        try:
            return _kugou_search(req, keyword, s["hash"])
        except Miss:
            continue
    return _kugou_search(req, keyword)


def _kugou_download(cand: dict, fmt: str) -> bytes:
    params = {"ver": 1, "client": "pc", "charset": "utf8", "fmt": fmt, "id": cand["id"], "accesskey": cand["accesskey"]}
    content = get_json(KUGOU_DOWNLOAD, params, 4).get("content") or ""
    if not content:
        raise Miss("empty download")
    return base64.b64decode(content)


def _kugou_tidy(parsed: Parsed, req: Req) -> Parsed:
    lines = parsed.lines
    for ln in lines:
        ln.text = html.unescape(ln.text)
        for w in ln.words:
            w.text = html.unescape(w.text)
    if len(lines) == 1 and "纯音乐" in lines[0].text:
        raise Miss("instrumental")
    drop = 0
    while drop < min(len(lines), 10):
        text = lines[drop].text
        if _CREDIT.match(text) or (drop == 0 and _TITLE_SEP.search(text)):
            drop += 1
        else:
            break
    kept = lines[drop:]
    return Parsed(kept, "\n".join(x.text for x in kept))


def fetch_kugou(req: Req) -> Parsed:
    cand = _kugou_candidate(req)
    parsed = None
    try:
        parsed = decode_krc(_kugou_download(cand, "krc"))
    except Miss:
        raise
    except Exception:
        parsed = None  # krc failed to decode, fall back to plain LRC
    if parsed is None or not parsed.lines:
        parsed = parse_lrc(_kugou_download(cand, "lrc"))
    return _kugou_tidy(parsed, req)


LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"

BETTERLYRICS_URL = "https://lyrics-api.boidu.dev/getLyrics"
UNISON_URL = "https://unison.betterlyrics.org/lyrics"
LYRICSPLUS_URL = "https://lyricsplus.binimum.org/v2/lyrics/get"
WARM_URLS = (BETTERLYRICS_URL, UNISON_URL, LYRICSPLUS_URL, KUGOU_SONGS, KUGOU_SEARCH, LRCLIB_GET)


def _lrclib_parse(rec: dict) -> Parsed:
    if rec.get("instrumental"):
        raise Miss("instrumental")
    if rec.get("syncedLyrics"):
        return parse_lrc(rec["syncedLyrics"])
    if rec.get("plainLyrics"):
        return parse_lrc(rec["plainLyrics"])
    raise Miss("no lyrics")


def fetch_lrclib(req: Req) -> Parsed:
    params = {"track_name": req.title, "artist_name": req.artist}
    if req.album:
        params["album_name"] = req.album
    if req.seconds:
        params["duration"] = str(req.seconds)
    exact = None
    try:
        exact = get_json(LRCLIB_GET, params, 8)
    except Miss:
        pass
    if isinstance(exact, dict) and exact.get("syncedLyrics"):
        return _lrclib_parse(exact)
    best, best_key = None, None
    try:
        records = get_json(LRCLIB_SEARCH, {"track_name": req.title}, 8)
    except Miss:
        records = []
    for rec in records if isinstance(records, list) else []:
        if not (title_ok(req.title, rec.get("trackName", "")) and artist_ok(req.artist, rec.get("artistName", ""))):
            continue
        if not dur_ok(req.seconds, rec.get("duration")):
            continue
        if not (rec.get("syncedLyrics") or rec.get("plainLyrics")) or rec.get("instrumental"):
            continue
        gap = abs((rec.get("duration") or 0) - req.seconds) if req.seconds else 0
        key = (0 if rec.get("syncedLyrics") else 1, gap)
        if best is None or key < best_key:
            best, best_key = rec, key
    if best is not None and (best.get("syncedLyrics") or not exact):
        return _lrclib_parse(best)
    if isinstance(exact, dict):
        return _lrclib_parse(exact)
    raise Miss("no matching record")


def fetch_youtubemusic(req: Req) -> Parsed:
    if not req.video_id:
        raise Skip("no videoId")
    try:
        from ytmusicapi import YTMusic  # type: ignore
    except ImportError:
        raise Skip("python-ytmusicapi not installed") from None
    yt = YTMusic()
    browse_id = yt.get_watch_playlist(req.video_id).get("lyrics")
    if not browse_id:
        raise Miss("no lyrics for this video")
    data = yt.get_lyrics(browse_id, timestamps=True) or {}
    body = data.get("lyrics")
    if isinstance(body, list) and body:
        def field_of(x, name):
            return x.get(name) if isinstance(x, dict) else getattr(x, name, None)
        lines = []
        for x in body:
            text = (field_of(x, "text") or "").strip()
            start = (field_of(x, "start_time") or 0) / 1000.0
            end = (field_of(x, "end_time") or 0) / 1000.0
            if text:
                lines.append(Line(start, end, text))
        lines.sort(key=lambda ln: ln.start)
        return Parsed(lines, "\n".join(ln.text for ln in lines))
    if isinstance(body, str) and body.strip():
        return parse_lrc(body)
    raise Miss("empty")


@dataclass
class Provider:
    id: str
    name: str
    word_capable: bool
    fetch: Callable[[Req], Parsed]


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("betterlyrics", "Better Lyrics", True, fetch_betterlyrics),
    Provider("unison", "Unison", True, fetch_unison),
    Provider("lyricsplus", "LyricsPlus", True, fetch_lyricsplus),
    Provider("kugou", "KuGou", True, fetch_kugou),
    Provider("lrclib", "LRCLIB", False, fetch_lrclib),
    Provider("youtubemusic", "YouTube Music", False, fetch_youtubemusic),
)}
DEFAULT_ORDER = list(PROVIDERS)


# --------------------------------------------------------------------------- #
# config, cache, log
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    default = {"version": CONFIG_VERSION, "order": list(DEFAULT_ORDER), "disabled": []}
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        if cfg.get("version") == CONFIG_VERSION and isinstance(cfg.get("order"), list):
            return cfg
        CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".json.bak"))  # older layout, start fresh
    except FileNotFoundError:
        pass
    except Exception:
        pass
    save_config(default)
    return default


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    except OSError:
        pass


def enabled_providers() -> list[Provider]:
    cfg = load_config()
    off = set(cfg.get("disabled") or [])
    seen, out = set(), []
    for pid in cfg.get("order") or []:
        if pid in PROVIDERS and pid not in off and pid not in seen:
            seen.add(pid)
            out.append(PROVIDERS[pid])
    return out


def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 256 * 1024:
            tail = LOG_PATH.read_text().splitlines()[-150:]
            LOG_PATH.write_text("\n".join(tail) + "\n")
        with LOG_PATH.open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _cache_file(req: Req) -> Path:
    key = hashlib.sha1(f"{norm(req.title)}|{norm(req.artist)}".encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


def cache_get(req: Req):
    """Returns ('hit', Result), ('miss', None) or None."""
    try:
        entry = json.loads(_cache_file(req).read_text())
    except (OSError, ValueError):
        return None
    if time.time() > entry.get("expires", 0):
        return None
    if req.seconds and entry.get("duration") and abs(req.seconds - entry["duration"]) > DURATION_TOL + 2:
        return None
    if entry.get("miss"):
        return ("miss", None)
    return ("hit", Result(entry["lrc"], entry["level"], entry["source"], entry["lines"], cached=True))


def cache_put(req: Req, res: Result | None, miss_ttl: int = 600) -> None:
    if res is None:
        entry = {"miss": True, "expires": time.time() + miss_ttl, "duration": req.seconds}
    else:
        ttl = 30 * 86400 if res.level == "word" else 3600
        entry = {"lrc": res.lrc, "level": res.level, "source": res.source, "lines": res.lines,
                 "duration": req.seconds, "expires": time.time() + ttl}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(req).write_text(json.dumps(entry, ensure_ascii=False))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# the race
# --------------------------------------------------------------------------- #
def _run_one(idx: int, prov: Provider, req: Req, out: queue.Queue) -> None:
    t0 = time.monotonic()
    try:
        parsed = finalize(prov.fetch(req), req)
        out.put((idx, "hit", parsed, time.monotonic() - t0))
    except Miss as e:
        out.put((idx, "miss", str(e), time.monotonic() - t0))
    except Skip as e:
        out.put((idx, "skip", str(e), time.monotonic() - t0))
    except Exception as e:  # network errors, bad JSON, ...
        out.put((idx, "err", f"{type(e).__name__}: {e}", time.monotonic() - t0))


def _best(hits: dict[int, Parsed]) -> int | None:
    best = None
    for i, parsed in hits.items():
        key = (-RANK[parsed.level()], i)
        if best is None or key < best[0]:
            best = (key, i)
    return best[1] if best else None


def _settled(i: int, level: str, pending: list[int], provs: list[Provider]) -> bool:
    """True when nothing still out is worth waiting for.
    A word-level hit is shown at once: a higher-priority word result that lands later only upgrades the
    cache for the next play. A line/plain hit waits (HOLD_SECONDS) for providers that could beat it."""
    if level == "word":
        return True
    for j in pending:
        if level == "line":
            if provs[j].word_capable or j < i:
                return False
        else:
            return False
    return True


def race(req: Req, provs: list[Provider], overall: float = OVERALL_TIMEOUT):
    out: queue.Queue = queue.Queue()
    for i, p in enumerate(provs):
        threading.Thread(target=_run_one, args=(i, p, req, out), daemon=True).start()
    t_end = time.monotonic() + overall
    hits: dict[int, Parsed] = {}
    outcomes: dict[int, tuple[str, str | Parsed, float]] = {}
    hold_until: float | None = None
    winner: int | None = None
    while True:
        pending = [i for i in range(len(provs)) if i not in outcomes]
        now = time.monotonic()
        best = _best(hits)
        if best is not None:
            lvl = hits[best].level()
            if _settled(best, lvl, pending, provs):
                winner = best
                break
            if hold_until is None:
                hold_until = now + HOLD_SECONDS
            if now >= hold_until:
                winner = best
                break
        if not pending or now >= t_end:
            winner = best
            break
        wait = t_end - now
        if hold_until is not None:
            wait = min(wait, max(hold_until - now, 0.0))
        try:
            idx, kind, payload, dt = out.get(timeout=max(wait, 0.01))
        except queue.Empty:
            continue
        outcomes[idx] = (kind, payload, dt)
        if kind == "hit":
            hits[idx] = payload
    # drain whatever else arrived, for the log
    while True:
        try:
            idx, kind, payload, dt = out.get_nowait()
            outcomes.setdefault(idx, (kind, payload, dt))
            if kind == "hit":
                hits.setdefault(idx, payload)
        except queue.Empty:
            break
    return winner, hits, outcomes, out, t_end


def _beats(i: int, j: int | None, hits: dict[int, Parsed]) -> bool:
    if j is None:
        return True
    return (-RANK[hits[i].level()], i) < (-RANK[hits[j].level()], j)


def _late_finish(req: Req, provs: list[Provider], hits, outcomes, out, t_end: float, winner, use_cache: bool,
                 on_upgrade: Callable[[Result], None] | None = None) -> None:
    """Keep listening after the race returned; upgrade the cache if a better result lands."""
    current = winner
    while len(outcomes) < len(provs) and time.monotonic() < t_end:
        try:
            idx, kind, payload, dt = out.get(timeout=max(t_end - time.monotonic(), 0.01))
        except queue.Empty:
            break
        outcomes[idx] = (kind, payload, dt)
        log(f"{provs[idx].id}: late {_describe(kind, payload, dt)}")
        if kind != "hit":
            continue
        hits[idx] = payload
        best = _best(hits)
        if best is not None and best != current and _beats(best, current, hits):
            parsed = hits[best]
            level = parsed.level()
            res = Result(to_lrc(parsed, level), level, provs[best].id, len([ln for ln in parsed.lines if ln.text]))
            current = best
            log(f"late winner: {res.source} {res.level}, cached for next play ({req.title} / {req.artist})")
            if use_cache:
                cache_put(req, res)
            if on_upgrade is not None:
                try:
                    on_upgrade(res)
                    log(f"late winner: on_upgrade delivered ({res.source} {res.level})")
                except Exception as e:  # a broken UI callback must never kill the listener
                    log(f"late winner: on_upgrade failed: {type(e).__name__}: {e}")


def _describe(kind: str, payload, dt: float) -> str:
    if kind == "hit":
        lines = len([ln for ln in payload.lines if ln.text])
        return f"HIT {payload.level()} {lines} lines ({dt:.2f}s)"
    tag = {"miss": "miss", "skip": "skip", "err": "ERROR"}[kind]
    return f"{tag}: {payload} ({dt:.2f}s)"


def _refresh(req: Req, provs: list[Provider], overall: float, current_level: str,
             on_upgrade: Callable[[Result], None] | None = None) -> None:
    """A cached line/plain result shouldn't hide a word-level one for the whole
    TTL. Race the providers again in the background and upgrade the cache (plus
    the live UI, when a callback is given) if something better lands."""
    winner, hits, outcomes, out, t_end = race(req, provs, overall)
    for i, p in enumerate(provs):
        if i in outcomes:
            log(f"{p.id}: refresh {_describe(*outcomes[i])}")
    if len(outcomes) < len(provs):
        threading.Thread(target=_late_finish,
                         args=(req, provs, hits, outcomes, out, t_end, winner, True, on_upgrade),
                         daemon=True).start()
    if winner is None:
        return
    parsed = hits[winner]
    level = parsed.level()
    if RANK[level] <= RANK[current_level]:
        return
    res = Result(to_lrc(parsed, level), level, provs[winner].id,
                 len([ln for ln in parsed.lines if ln.text]))
    log(f"refresh winner: {res.source} {res.level} ({req.title} / {req.artist})")
    cache_put(req, res)
    if on_upgrade is not None:
        try:
            on_upgrade(res)
        except Exception as e:  # a broken UI callback must never kill the listener
            log(f"refresh winner: on_upgrade failed: {type(e).__name__}: {e}")


def fetch(title, artist, album=None, duration=None, video_id=None, *, use_cache: bool = True,
          overall_timeout: float = OVERALL_TIMEOUT,
          on_upgrade: Callable[[Result], None] | None = None) -> Result | None:
    req = Req.make(title, artist, album, duration, video_id)
    if not req.title:
        return None
    provs = enabled_providers()
    if not provs:
        return None
    if use_cache:
        cached = cache_get(req)
        if cached is not None:
            kind, res = cached
            if kind == "hit" and res is not None and res.level != "word":
                # Serve the line/plain hit now, but keep looking for word-level
                # in the background: cache_get alone would block it for the TTL.
                threading.Thread(target=_refresh,
                                 args=(req, provs, overall_timeout, res.level, on_upgrade),
                                 daemon=True).start()
            return res
    winner, hits, outcomes, out, t_end = race(req, provs, overall_timeout)
    for i, p in enumerate(provs):
        if i in outcomes:
            log(f"{p.id}: {_describe(*outcomes[i])}")
        else:
            log(f"{p.id}: still out, letting it finish in the background ({req.title} / {req.artist})")
    if len(outcomes) < len(provs):
        threading.Thread(target=_late_finish, args=(req, provs, hits, outcomes, out, t_end, winner, use_cache, on_upgrade),
                         daemon=True).start()
    if winner is None:
        has_err = any(o[0] == "err" for o in outcomes.values()) or len(outcomes) < len(provs)
        if use_cache:
            cache_put(req, None, miss_ttl=120 if has_err else 600)
        return None
    parsed = hits[winner]
    level = parsed.level()
    res = Result(to_lrc(parsed, level), level, provs[winner].id,
                 len([ln for ln in parsed.lines if ln.text]))
    log(f"winner: {res.source} {res.level} ({req.title} / {req.artist})")
    if use_cache:
        cache_put(req, res)
    return res


def video_id_from_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    if host == "youtu.be":
        return u.path.strip("/").split("/")[0]
    if host.endswith("youtube.com"):
        return (urllib.parse.parse_qs(u.query).get("v") or [""])[0]
    return ""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cmd_list() -> None:
    cfg = load_config()
    off = set(cfg.get("disabled") or [])
    print(f"config: {CONFIG_PATH}")
    for n, pid in enumerate(cfg["order"], 1):
        p = PROVIDERS.get(pid)
        if p:
            print(f"{n}. {p.id:<13} {p.name:<14} {'off' if pid in off else 'on ':<3} {'word' if p.word_capable else 'line'}")
    rest = [pid for pid in PROVIDERS if pid not in cfg["order"]]
    if rest:
        print("not in order (never used):", ", ".join(rest))


def _cmd_probe(req: Req) -> None:
    provs = list(PROVIDERS.values())
    results: dict[int, tuple] = {}
    out: queue.Queue = queue.Queue()
    if RAW:  # one at a time so the raw dumps do not interleave
        for i, p in enumerate(provs):
            _run_one(i, p, req, out)
    else:
        for i, p in enumerate(provs):
            threading.Thread(target=_run_one, args=(i, p, req, out), daemon=True).start()
    t_end = time.monotonic() + 20
    while len(results) < len(provs) and time.monotonic() < t_end:
        try:
            idx, kind, payload, dt = out.get(timeout=0.2)
            results[idx] = (kind, payload, dt)
        except queue.Empty:
            pass
    hits = {}
    for i, p in enumerate(provs):
        if i not in results:
            print(f"{p.name:<14} >20s  still running, gave up")
            continue
        kind, payload, dt = results[i]
        if kind == "hit":
            level = payload.level()
            n = len([ln for ln in payload.lines if ln.text])
            print(f"{p.name:<14} {dt:5.2f}s  HIT {level:<5} {n} lines")
            hits[i] = payload
        elif kind == "err":
            print(f"{p.name:<14} {dt:5.2f}s  ERR  {payload}")
        else:
            print(f"{p.name:<14} {dt:5.2f}s  {kind}" + (f"  ({payload})" if payload else ""))
    best = _best(hits)
    if best is not None:
        print(f"\nwould win: {provs[best].name} ({hits[best].level()})")
    else:
        print("\nno provider returned usable lyrics")


def main(argv: list[str] | None = None) -> int:
    global RAW
    ap = argparse.ArgumentParser(description="Sweep lyrics providers")
    ap.add_argument("title", nargs="?")
    ap.add_argument("artist", nargs="?", default="")
    ap.add_argument("--album", default="")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds")
    ap.add_argument("--video-id", default="")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--raw", action="store_true", help="with --probe: dump raw responses to stderr")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--lrc", action="store_true", help="print the full LRC")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--order", help="comma separated provider ids; unlisted ones are switched off")
    ap.add_argument("--enable")
    ap.add_argument("--disable")
    args = ap.parse_args(argv)
    RAW = args.raw

    if args.order or args.enable or args.disable:
        cfg = load_config()
        if args.order:
            ids = [x.strip().lower() for x in args.order.split(",") if x.strip()]
            bad = [x for x in ids if x not in PROVIDERS]
            if bad:
                print(f"unknown provider(s): {', '.join(bad)}; known: {', '.join(PROVIDERS)}", file=sys.stderr)
                return 2
            cfg["order"] = ids + [p for p in cfg["order"] if p not in ids]
            cfg["disabled"] = [p for p in PROVIDERS if p not in ids]
        for key, flag in (("enable", args.enable), ("disable", args.disable)):
            if not flag:
                continue
            pid = flag.strip().lower()
            if pid not in PROVIDERS:
                print(f"unknown provider: {pid}", file=sys.stderr)
                return 2
            off = set(cfg.get("disabled") or [])
            (off.discard if key == "enable" else off.add)(pid)
            if pid not in cfg["order"]:
                cfg["order"].append(pid)
            cfg["disabled"] = sorted(off)
        save_config(cfg)
        _cmd_list()
        return 0
    if args.list:
        _cmd_list()
        return 0
    if not args.title:
        ap.print_usage()
        return 2

    if args.probe:
        _cmd_probe(Req.make(args.title, args.artist, args.album, args.duration, args.video_id))
        return 0
    t0 = time.monotonic()
    res = fetch(args.title, args.artist, args.album, args.duration, args.video_id or None, use_cache=not args.no_cache)
    dt = time.monotonic() - t0
    if not res:
        print(f"no lyrics ({dt:.2f}s); details: {LOG_PATH}")
        return 1
    print(f"{res.source}  {res.level}  {res.lines} lines  {'cached' if res.cached else f'{dt:.2f}s'}")
    rows = res.lrc.splitlines()
    for row in rows if args.lrc else rows[:8]:
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
