"""Base types for switchable lyric animations.

One playback clock drives every animation. Each frame the caller builds a
single :class:`AnimState` from that clock and hands the *same* state to
whichever animation is active, so styles can't drift apart in timing.

An animation returns an :class:`AnimFrame`: the styled lyric line plus an
optional block of rows drawn *above* it (the "dot lane" for a bouncing dot).
The number of extra rows is declared per animation via
``extra_rows_above`` and enforced by :meth:`LyricAnimation.frame`, so
switching styles at runtime never changes the layout height.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from rich.text import Text

__all__ = [
    "WordSpan",
    "AnimState",
    "AnimFrame",
    "LyricAnimation",
    "default_syllables",
    "resolve_word_spans",
]


# --------------------------------------------------------------------------- #
# word resolution
# --------------------------------------------------------------------------- #
_VOWEL_GROUP = re.compile(r"[aeiouyAEIOUY]+")


def default_syllables(text: str) -> int:
    """Rough syllable count by vowel groups.

    Only a fallback: the app injects its own (more accurate) estimator via
    ``syllable_fn`` so numbers stay identical to the existing wipe path.
    """
    if not (text or "").strip():
        return 1
    return max(1, len(_VOWEL_GROUP.findall(text)))


@dataclass(frozen=True)
class WordSpan:
    """A sung word resolved to both a time window and a character range."""

    onset: float
    end: float
    text: str
    char_from: int
    char_to: int

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.onset)


def _span_end(
    onset: float,
    next_onset: Optional[float],
    est: float,
    real_end: Optional[float],
    hold_max: float,
) -> float:
    """Absolute end time for a word, mirroring the wipe's ``_word_dur``:
    prefer a real provider end, otherwise the gap to the next onset (a held
    note), capped by 2x the syllable estimate for genuine pauses."""
    if real_end is not None and real_end > onset:
        return max(real_end, onset + 0.05)
    cap = max(est * 2.0, 0.5)
    if next_onset is None:
        return onset + cap
    gap = max(next_onset - onset, 0.05)
    if hold_max > cap and gap <= hold_max:
        return onset + gap
    return onset + min(gap, cap)


def resolve_word_spans(
    text: str,
    word_timings: Optional[Sequence],
    line_end: Optional[float] = None,
    *,
    syllable_fn=default_syllables,
    sec_per_syllable: float = 0.22,
    hold_max: float = 3.0,
) -> Tuple[WordSpan, ...]:
    """Turn Enhanced-LRC word timings into character-anchored spans.

    Matching mirrors the wipe path: each timing's text is located
    left-to-right inside ``text``. If any word can't be found (romanized or
    edited line, adlib stripping) the whole line returns an empty tuple, so
    callers can fall back to a line-level estimate instead of guessing.

    ``word_timings`` entries are ``(onsets_seconds, word_text)`` and may also
    carry ``.end`` / ``.syl`` attributes (see ``app.WordTS``).
    """
    if not word_timings:
        return ()

    spans: List[WordSpan] = []
    cursor = 0
    for wt in word_timings:
        try:
            onset, wtext = wt[0], wt[1]
        except (TypeError, IndexError, KeyError):
            return ()
        idx = text.find(wtext, cursor)
        if idx < 0:
            return ()
        spans.append(
            WordSpan(
                onset=float(onset),
                end=float(onset),  # filled in below once the next onset is known
                text=wtext,
                char_from=idx,
                char_to=idx + len(wtext),
            )
        )
        cursor = idx + len(wtext)

    if not spans:
        return ()

    resolved: List[WordSpan] = []
    for k, span in enumerate(spans):
        if k + 1 < len(spans):
            next_onset: Optional[float] = spans[k + 1].onset
        else:
            next_onset = line_end
        est = max(0.15, syllable_fn(span.text) * sec_per_syllable)
        real_end = getattr(word_timings[k], "end", None)
        resolved.append(
            WordSpan(
                onset=span.onset,
                end=_span_end(span.onset, next_onset, est, real_end, hold_max),
                text=span.text,
                char_from=span.char_from,
                char_to=span.char_to,
            )
        )
    return tuple(resolved)


# --------------------------------------------------------------------------- #
# per-frame state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AnimState:
    """Everything an animation needs to draw the active line.

    Built once per frame from a single interpolated playback clock, then
    passed unchanged to whichever :class:`LyricAnimation` is active.
    """

    # clock
    pos: float                 # interpolated playback position (s)
    sync_pos: float            # pos + user sync offset (s)

    # active line
    line_idx: int
    line_text: str             # text as shown (already romanized / adlib-cleaned)
    line_start: float
    line_end: float            # next line's start (or an estimate)
    wipe_span: float           # duration the wipe maps frac onto
    frac: float                # 0..1 line progress used by the wipe
    accent_hex: str            # "#rrggbb" for the highlighted part

    # word-level data (empty when the line has no Enhanced-LRC words)
    words: Tuple[WordSpan, ...] = ()
    word_timings: Optional[Sequence] = None

    # active-word cursor (word_idx = -1 before the first word starts)
    word_idx: int = -1
    word_progress: float = 0.0
    next_onset: Optional[float] = None
    time_to_next: float = 0.0

    # geometry
    width: int = 0
    height: int = 0

    # layout: display-cell columns, filled in by the line renderer so an
    # animation never has to re-derive where a word sits on screen. Both are
    # relative to the start of ``line_text`` and count *cells* (not code
    # points), so wide glyphs and emoji line up with the drawn row.
    line_cols: int = 0
    word_cols: Tuple[Tuple[int, int], ...] = ()

    # ---- convenience ------------------------------------------------
    @property
    def word_count(self) -> int:
        return len(self.words)

    def cols_for(self, idx: int) -> Tuple[int, int]:
        """Display-cell ``(start, end)`` of word ``idx`` inside the line.

        Prefers the renderer-supplied ``word_cols``; only when a caller built
        the state by hand (e.g. a test) does it fall back to the code-point
        character range, which is a plain index map — never centring maths.
        """
        if 0 <= idx < len(self.word_cols):
            return self.word_cols[idx]
        if 0 <= idx < len(self.words):
            w = self.words[idx]
            return (w.char_from, w.char_to)
        return (0, 0)

    def active_word(self) -> Optional[WordSpan]:
        if 0 <= self.word_idx < len(self.words):
            return self.words[self.word_idx]
        return None

    def next_word(self) -> Optional[WordSpan]:
        if 0 <= self.word_idx + 1 < len(self.words):
            return self.words[self.word_idx + 1]
        return None


@dataclass
class AnimFrame:
    """What one animation produces for one frame.

    ``above`` holds rows drawn directly on top of ``line`` (the last entry
    sits immediately above it). ``line`` is the styled lyric itself.
    """

    line: Text
    above: List[Text] = field(default_factory=list)

    def pad_above(self, rows: int) -> "AnimFrame":
        """Force ``above`` to exactly ``rows`` entries.

        Extra rows are added at the *top* so the visible lane stays glued to
        the lyric line; surplus rows are dropped. This is what keeps the
        layout from jumping when styles with different lane heights are
        swapped at runtime.
        """
        if rows <= 0:
            return AnimFrame(self.line, [])
        above = list(self.above[-rows:]) if len(self.above) > rows else list(self.above)
        while len(above) < rows:
            above.insert(0, Text(""))
        return AnimFrame(self.line, above)


# --------------------------------------------------------------------------- #
# animation interface
# --------------------------------------------------------------------------- #
class LyricAnimation:
    """Base class for a switchable lyric style.

    Subclasses set ``name`` / ``label`` and implement :meth:`render`. Adding
    a new style is one file plus one :data:`animations.REGISTRY` entry — the
    engine never needs to know about it.
    """

    name: str = "base"
    label: str = "Base"
    extra_rows_above: int = 0  # rows reserved above the line (e.g. a dot lane)

    def render(self, state: AnimState) -> AnimFrame:
        """Draw one frame. Must be pure with respect to ``state``."""
        raise NotImplementedError

    # ---- helpers ----------------------------------------------------
    def frame(self, line: Text, above: Optional[Sequence[Text]] = None) -> AnimFrame:
        """Wrap ``line`` (and optional lane rows) in a correctly sized frame."""
        return AnimFrame(line, list(above or ())).pad_above(self.extra_rows_above)

    @staticmethod
    def plain(text: str, style: str = "bold white") -> Text:
        return Text(text, style=style)
