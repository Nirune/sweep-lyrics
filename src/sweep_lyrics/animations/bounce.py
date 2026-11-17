"""Word bounce: the active word hops out of the lyric row into a 1-row lane.

Unlike :mod:`dot` (a separate glyph that travels between words), here the
*word itself* is the moving object. Over the active word's duration it:

* starts in the lane row (up), just above the lyric row, at the word's onset;
* eases down and *lands* back on the lyric row as the word is held, settling
  there right around the word's end.

Because text is a grid of cells, "bouncing" is a row-level hop, not a
sub-pixel motion: the word is drawn in exactly one of two places (the lane
row or the lyric row), and which one it occupies is a pure function of the
word's progress. A paused player therefore renders the identical frame on
every tick.

Positioning
-----------
Same contract as the dot lane: the renderer hands this module a layout
(``AnimState.line_cols`` / ``AnimState.word_cols``) and the lane is built
exactly ``line_cols`` cells wide, so a word column and a lane column are the
same column once the shared centring/rail prefix is applied. When the word is
in the lyric row, the lane above is a row of braille blanks; when the word is
in the lane, the lyric row carries that same word hollowed out to braille
blanks so it is never drawn twice and the columns stay aligned.
"""

from __future__ import annotations

from typing import Optional, Tuple

from rich.cells import cell_len
from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = [
    "WordBounce",
    "BOUNCE_LANE_ROWS",
    "LANE_BLANK",
    "bounce_progress",
    "bounce_position",
    "active_word_span",
]

# ---- Tunable constants ------------------------------------------------
BOUNCE_LANE_ROWS = 1    # the lane is always one terminal row, any terminal height
LANE_BLANK = "\u2800"   # braille blank: one cell wide, not whitespace (see dot.py)


# ---- pure geometry ----------------------------------------------------
def bounce_progress(state: AnimState) -> Optional[float]:
    """0..1 through the active word's hop, or None when there is no word.

    ``state.word_progress`` is the renderer's 0..1 progress of the sung word;
    the hop runs from the word's onset (0) to its end (1).
    """
    if state.word_idx < 0 or not state.words:
        return None
    idx = min(state.word_idx, state.word_count - 1)
    w = state.words[idx]
    if w.duration <= 0:
        return 0.0
    return max(0.0, min(1.0, (state.sync_pos - w.onset) / w.duration))


def bounce_position(p: Optional[float]) -> str:
    """Which of the two rows the word occupies, given its hop progress.

    ``'lane'``  -> the word is lifted into the lane row (up), during the first
                   half of its hop (rising + peak).
    ``'line'``  -> the word has landed back on the lyric row (down), during the
                   second half (settling into the hold).

    The switch sits at progress 0.5 so the word visibly rises, holds at the
    top for the sung syllable, then drops and settles — one clean arc.
    """
    if p is None:
        return "lane"
    return "line" if p >= 0.5 else "lane"


def active_word_span(state: AnimState) -> Optional[Tuple[int, int]]:
    """Display-cell ``(start, end)`` of the active word, or None."""
    if state.word_idx < 0 or not state.words:
        return None
    return state.cols_for(min(state.word_idx, state.word_count - 1))


class WordBounce(LyricAnimation):
    """The sung word hops between the lane row and the lyric row."""

    name = "bounce"
    label = "Word bounce"
    extra_rows_above = BOUNCE_LANE_ROWS
    # Interlude (intro / breaks / outro): one dot that hops inside the lane.
    interlude_mode = "single_dot"

    def lane_rows_for(self, height=None) -> int:
        """The lane is always one row, however short the terminal is."""
        return BOUNCE_LANE_ROWS

    def _width(self, state: AnimState) -> int:
        return max(1, state.line_cols or cell_len(state.line_text))

    def _styled_line(self, state: AnimState, word_in_line: bool) -> Text:
        """The lyric row: the active word drawn normally when it is *on* the
        line, otherwise hollowed out to braille blanks so the lane above can
        carry it without the word appearing twice."""
        text = state.line_text
        span = active_word_span(state)
        if span is None:
            return Text(text, style=f"bold {state.accent_hex}")
        a, b = span
        t = Text()
        if word_in_line:
            t.append(text[:a], style="bold white")
            t.append(text[a:b], style=f"bold {state.accent_hex}")
            t.append(text[b:], style="bold white")
        else:
            t.append(text[:a], style="bold white")
            t.append(LANE_BLANK * (b - a), style="dim")
            t.append(text[b:], style="bold white")
        return t

    def _lane_row(self, state: AnimState, word_in_lane: bool) -> Text:
        """The single lane row: the active word when it is up, else braille
        blanks so the lane still measures and centres like the lyric row."""
        width = self._width(state)
        span = active_word_span(state)
        if span is None or not word_in_lane:
            return Text(LANE_BLANK * width)
        a, b = span
        text = state.line_text
        chars = [LANE_BLANK] * width
        for i, ch in enumerate(text[a:b]):
            col = a + i
            if 0 <= col < width:
                chars[col] = ch
        return Text("".join(chars), style=f"bright bold {state.accent_hex}")

    # ---- frame ------------------------------------------------------
    def render(self, state: AnimState) -> AnimFrame:
        """Draw the lane row above the lyric line.

        The word occupies exactly one of the two rows (lane or line), chosen
        by :func:`bounce_position`; the other row keeps the word's columns as
        braille blanks so the layout never shifts.
        """
        p = bounce_progress(state)
        word_in_lane = bounce_position(p) == "lane"
        line = self._styled_line(state, word_in_line=not word_in_lane)
        above = [self._lane_row(state, word_in_lane=word_in_lane)]
        return self.frame(line, above)
