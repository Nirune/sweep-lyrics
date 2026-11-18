"""Bouncing dot animation: one glyph in a short lane above the lyric line.

A single ``●`` (DOT_GLYPH) lives in a fixed lane of ``DOT_LANE_ROWS`` terminal
rows, glued directly on top of the active lyric row:

* while a word is being sung the dot **rests on the bottom row of the lane**
  (immediately above that word) and bobs one subrow up and down;
* during the jump to the next word — the last ``ARC_SEC`` before it, ending
  ``ARC_LEAD`` early — the dot **arcs to the top row** and travels sideways
  from the current word's centre to the next one's, landing on the bottom row
  just before that word lights up.

The jump window is capped rather than starting at the current word's ``end``:
back-to-back Enhanced-LRC words end exactly where the next one starts, and
without the cap the dot would never leave the bottom row.

Vertical positions are expressed in braille subrows (``SUBPIXELS_PER_ROW`` per
terminal row), so the clamp is exact::

    0 .. DOT_LANE_ROWS * SUBPIXELS_PER_ROW - DOT_HEIGHT_SUBROWS

The rendered glyph is a single full-cell character, so on screen the subrow
value quantises to a terminal row: rest is the bottom row, and only the middle
of an arc reaches the top row. The lane is ``DOT_LANE_ROWS`` rows on a normal
terminal and shrinks to ``DOT_LANE_ROWS_MIN`` only when the body is too short to
keep the full lane — before any lyric row would have to be dropped.

The lane height depends only on the available body height, never on the current
line, so changing lines (or switching styles) cannot make the active line jump.

Positioning
-----------
This module never decides *where* a word is. The lyric renderer hands the
animation a layout — ``AnimState.line_cols`` (display width of the line) and
``AnimState.word_cols`` (display-cell start/end of every word) — and the lane
here is built exactly ``line_cols`` cells wide. Because the lane and the lyric
row share the same width, the shared centring/rail prefix aligns them column
for column, so a word column and a lane column are the same thing.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from rich.cells import cell_len
from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = [
    "BouncingDot",
    "DOT_GLYPH",
    "DOT_LANE_ROWS",
    "DOT_LANE_ROWS_MIN",
    "DOT_HEIGHT_SUBROWS",
    "SUBPIXELS_PER_ROW",
    "arc_window",
    "clamp_subrow",
    "dot_column",
    "dot_subrow",
    "jump_progress",
    "lane_rows_for",
    "max_subrow",
    "word_center",
]

# ---- Tunable constants ------------------------------------------------
DOT_GLYPH = "●"      # U+25CF. "⬤" (U+2B24) is fatter, but some fonts draw it
                     # double-width, which would shift the whole lane by a cell.

DOT_LANE_ROWS = 2    # lane height in terminal rows on a normal terminal
DOT_LANE_ROWS_MIN = 1  # short-terminal fallback (arc collapses to that row)
# The full lane is kept while this many body rows are available; below it the
# lane gives its extra row back to the lyrics first (requirement: shrink the
# lane before hiding lyric rows).
DOT_LANE_FULL_MIN_HEIGHT = 8

SUBPIXELS_PER_ROW = 4      # braille grid: 4 vertical subrows per terminal row
DOT_HEIGHT_SUBROWS = 1     # the dot occupies this many subrows (clamp reserve)
DOT_BOB_SUBROWS = 4        # bob amplitude across terminal rows while holding a word
BOB_HZ = 1.2               # bobs per second while holding a word
ARC_SEC = 0.24             # longest jump toward the next word (s)
ARC_LEAD = 0.04            # land this long before the next word's onset

# Empty lane cells are drawn as a braille blank rather than a space: rich's
# ``Lines.justify("center")`` calls ``rstrip()`` on every line before centring
# it, so a lane row that ended in spaces would be measured short and pushed to
# the right inside a centred table cell (the split layout). U+2800 is not
# whitespace and is one cell wide, so the lane measures and centres exactly
# like the lyric row it sits above. It also renders as an empty cell, so only
# the dot itself is visible.
LANE_BLANK = "\u2800"


# ---- pure geometry ----------------------------------------------------
def smoothstep(x: float) -> float:
    """Ease in/out on 0..1 (clamped)."""
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def lane_rows_for(height: Optional[int]) -> int:
    """Lane height in rows for a body of ``height`` available rows.

    Full lane normally; one row when the body is too short to give it the
    extra row without pushing lyric context off screen. Depends only on the
    height, so it is identical on every line of a given terminal size.
    """
    if height is not None and int(height) < DOT_LANE_FULL_MIN_HEIGHT:
        return DOT_LANE_ROWS_MIN
    return DOT_LANE_ROWS


def max_subrow(lane_rows: int = DOT_LANE_ROWS) -> int:
    """Largest valid subrow: the dot's bottom edge sits on the lane's bottom."""
    return max(0, int(lane_rows) * SUBPIXELS_PER_ROW - DOT_HEIGHT_SUBROWS)


def clamp_subrow(y: float, lane_rows: int = DOT_LANE_ROWS) -> int:
    """Clamp a vertical position to ``0 .. max_subrow(lane_rows)``."""
    return max(0, min(max_subrow(lane_rows), int(round(y))))


def word_center(cols: Tuple[int, int]) -> int:
    """Display-cell centre of a word span (start, end)."""
    return (cols[0] + cols[1]) // 2


def arc_window(state: AnimState) -> Optional[Tuple[float, float]]:
    """Absolute ``(start, end)`` of the jump toward the next word, or None.

    The jump is the tail of the active word's span: it starts at most
    ``ARC_SEC`` before the landing point and ends ``ARC_LEAD`` before the next
    onset, so the dot is back on the bottom of the lane the moment the next
    word lights up. Capping the length (instead of starting at the current
    word's ``end``) matters because back-to-back Enhanced-LRC words end exactly
    where the next one starts; without the cap the dot would never leave the
    bottom row. No window means the word is held (or it is the last word).
    """
    if not state.words or state.word_idx < 0:
        return None
    idx = min(state.word_idx, state.word_count - 1)
    if idx + 1 >= state.word_count:
        return None
    onset = state.words[idx].onset
    end = state.words[idx + 1].onset - ARC_LEAD
    start = max(onset, end - ARC_SEC)
    if end <= start:
        return None
    return start, end


def jump_progress(state: AnimState,
                  window: Optional[Tuple[float, float]] = None) -> Optional[float]:
    """0..1 through the current jump, or None when the dot is resting.

    Returns 0 at the start of the window, 1 from its end until the next onset
    (the ``ARC_LEAD`` settle), and None outside it.
    """
    window = arc_window(state) if window is None else window
    if window is None:
        return None
    start, end = window
    if state.sync_pos < start:
        return None
    if state.sync_pos >= end:
        return 1.0
    return (state.sync_pos - start) / (end - start)


def dot_column(state: AnimState) -> int:
    """Lane column of the dot: on the active word, or between two words.

    While resting the dot sits on the centre of the word being sung; during
    the jump it eases from that word's centre to the next one's, landing just
    before the next onset. Column values come only from ``state.cols_for``.
    """
    if state.word_count == 0:
        return 0
    idx = max(0, min(state.word_idx, state.word_count - 1))
    current = word_center(state.cols_for(idx))
    progress = jump_progress(state)
    if progress is None:
        return current
    target = min(idx + 1, state.word_count - 1)
    nxt = word_center(state.cols_for(target))
    return int(round(current + (nxt - current) * smoothstep(progress)))


def dot_subrow(state: AnimState, lane_rows: int = DOT_LANE_ROWS) -> int:
    """Vertical position in subrows: bottom while singing, arc during a jump.

    Returns a value in ``0 .. max_subrow(lane_rows)``. The bottom of that range
    is the lane's lowest subrow, i.e. directly above the active lyric line.
    """
    rest = max_subrow(lane_rows)
    progress = jump_progress(state)
    if progress is not None:
        # Parabola-like arc: rest at both ends, lane top at the midpoint.
        return clamp_subrow(rest * (1.0 - math.sin(math.pi * progress)),
                            lane_rows)
    if not state.words or state.word_idx < 0:
        return rest
    idx = min(state.word_idx, state.word_count - 1)
    elapsed = max(0.0, state.sync_pos - state.words[idx].onset)
    bob = 0.5 - 0.5 * math.cos(2.0 * math.pi * BOB_HZ * elapsed)
    return clamp_subrow(rest - DOT_BOB_SUBROWS * bob, lane_rows)


def dot_style(phase: float, accent_hex: str) -> str:
    """Constant accent style for the dot.

    The dot used to breathe between grey and the accent (``phase``); it now
    keeps one colour so it reads as a solid accent mark rather than something
    that fades in and out. ``phase`` is kept for callers/tests.
    """
    return f"bold {accent_hex}"


class BouncingDot(LyricAnimation):
    """One dot that rests above the sung word and jumps between words."""

    name = "dot"
    label = "Bouncing dot"
    # Nominal reservation; ``lane_rows_for(state.height)`` can shrink it to
    # DOT_LANE_ROWS_MIN on a short terminal and ``render`` returns exactly that
    # many rows (no padding), so the app's centring uses the same number.
    extra_rows_above = DOT_LANE_ROWS
    # Interlude (intro / breaks / outro): starts with one dot hopping in the
    # centre of the lane (like the lyric dot), then splits into three dots side
    # by side like the wipe countdown. One transition per gap session.
    interlude_mode = "single_to_three_dots"

    def lane_rows(self, state: AnimState) -> int:
        """Rows this frame's lane occupies (2 normally, 1 when very short)."""
        return lane_rows_for(state.height)

    def lane_rows_for(self, height: Optional[int] = None) -> int:
        """Rows the lane occupies for a body of ``height`` rows.

        The app's layout code asks the active style for this before rendering,
        so the reserved rows and the drawn rows cannot disagree on a short
        terminal (where the lane shrinks to one row).
        """
        return lane_rows_for(height)

    def _geometry(self, state: AnimState, lane_rows: int) -> Tuple[int, int]:
        """``(column, subrow)`` for this frame; column 0 when there is no word."""
        width = max(1, state.line_cols or cell_len(state.line_text))
        if not state.words or state.word_count == 0:
            return 0, dot_subrow(state, lane_rows)
        col = max(0, min(width - 1, dot_column(state)))
        return col, dot_subrow(state, lane_rows)

    # ---- frame ------------------------------------------------------
    def _styled_line(self, state: AnimState) -> Text:
        """Karaoke colour progress for the lyric row.

        Every word up to and including the active one has had its onset, so
        it keeps the accent colour like a progress marker; the words after it
        stay white until their turn. Before the first onset (or when the line
        has no word data) the whole row is plain white.
        """
        text = state.line_text
        if not state.words or state.word_idx < 0:
            return self.plain(text)
        active = min(state.word_idx, len(state.words) - 1)
        end = state.words[active].char_to
        line = Text()
        line.append(text[:end], style=f"bold {state.accent_hex}")
        line.append(text[end:], style="bold white")
        return line

    def render(self, state: AnimState) -> AnimFrame:
        """Render the lane rows above the lyric line.

        The line itself is pointer-free (no wipe): words up to the active one
        are accent, the rest stay white. The dot style is a pointer, not a
        sweep.
        """
        lane_rows = self.lane_rows(state)
        return AnimFrame(self._styled_line(state),
                         self.lane(state, lane_rows))

    def lane(self, state: AnimState, lane_rows: Optional[int] = None) -> list[Text]:
        """The fixed-height lane, exactly as wide as the lyric row."""
        lane_rows = self.lane_rows(state) if lane_rows is None else lane_rows
        lane_rows = max(DOT_LANE_ROWS_MIN, min(DOT_LANE_ROWS, int(lane_rows)))
        width = max(1, state.line_cols or cell_len(state.line_text))
        blank = LANE_BLANK * width
        rows = [Text(blank) for _ in range(lane_rows)]

        if not state.words or state.word_count == 0:
            return rows  # no word data: keep the lane, draw nothing

        col, y = self._geometry(state, lane_rows)
        row = max(0, min(lane_rows - 1, y // SUBPIXELS_PER_ROW))
        peak = max_subrow(lane_rows)
        phase = 0.0 if peak <= 0 else 1.0 - (y / peak)

        chars = [LANE_BLANK] * width
        chars[col] = DOT_GLYPH
        rows[row] = Text("".join(chars),
                         style=dot_style(phase, state.accent_hex))
        return rows

    # ---- debug hook (SWEEP_DEBUG=1) ---------------------------------
    def position(self, state: AnimState) -> Tuple[int, int]:
        """(lane column, lane row) for the current frame."""
        lane_rows = self.lane_rows(state)
        width = max(1, state.line_cols or cell_len(state.line_text))
        if not state.words or state.word_count == 0:
            return 0, max(0, min(lane_rows - 1,
                                 dot_subrow(state, lane_rows) // SUBPIXELS_PER_ROW))
        col, y = self._geometry(state, lane_rows)
        return col, max(0, min(lane_rows - 1, y // SUBPIXELS_PER_ROW))
