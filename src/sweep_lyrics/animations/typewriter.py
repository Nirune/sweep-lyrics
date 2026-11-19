"""Typewriter: characters appear one at a time, as they are sung.

The lyric row starts empty and fills in from the left. Every character is
revealed at its own interpolated moment inside the word that contains it, so
the reveal tracks the vocal instead of jumping one whole word at a time.
A blinking caret sits on the next character to appear.

Untyped characters are drawn as braille blanks (``HIDDEN``) rather than
spaces: a braille blank is one cell wide and *not* whitespace, so rich's
``justify("center")`` measures the row at its full width and the line does
not slide sideways as it fills in. Real spaces stay spaces, which keeps the
word gaps visible while the words themselves are still blank.

Timing
------
``typed_boundary`` is a pure function of ``AnimState``: for the word being
sung it maps ``sync_pos`` across the word's ``[onset, end)`` window onto that
word's character range. A paused player therefore renders the identical
frame on every tick — the caret blinks off ``sync_pos`` too, so it freezes
instead of drifting while paused.

No lane rows are reserved: everything happens inside the lyric row itself.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = [
    "Typewriter",
    "CARET",
    "CARET_HZ",
    "CARET_ON",
    "HIDDEN",
    "caret_on",
    "typed_boundary",
]

# ---- Tunable constants ------------------------------------------------
CARET = "\u258c"          # U+258C left half block: a thick I-beam. The rails
                          # that prefix a lyric row are a thin "|", so the
                          # caret never reads as that marker.
CARET_HZ = 1.6            # blink cycles per second
CARET_ON = 0.6            # fraction of each cycle the caret is visible
HIDDEN = "\u2800"         # U+2800 braille blank: one cell, not whitespace


def typed_boundary(state: AnimState) -> int:
    """Index into ``state.line_text`` up to which characters are typed.

    Returns 0 before the first onset, the middle of the active word while it
    is being sung (the character being sung counts as already visible), and
    ``len(text)`` once the last word is done — which also reveals any
    trailing punctuation the word spans do not cover.
    """
    text = state.line_text
    total = len(text)
    if not state.words:
        return 0

    typed = 0
    last = -1
    for k, w in enumerate(state.words):
        if state.sync_pos < w.onset:
            break
        last = k
        n = max(0, w.char_to - w.char_from)
        dur = w.duration
        if n == 0 or dur <= 0:
            typed = w.char_to
            continue
        frac = (state.sync_pos - w.onset) / dur
        if frac >= 1.0:
            typed = w.char_to
            continue
        # +1: the character being sung is visible the moment its turn starts
        typed = w.char_from + min(n, int(frac * n) + 1)
        return max(0, min(typed, total))

    if last >= 0 and last == len(state.words) - 1:
        return total  # last word finished: show whatever trails the line
    return max(0, min(typed, total))


def caret_on(sync_pos: float) -> bool:
    """Whether the caret is in its visible half of the blink cycle."""
    return (float(sync_pos) * CARET_HZ) % 1.0 < CARET_ON


def _hidden(ch: str) -> str:
    """Placeholder for an untyped character, keeping its cell width."""
    if ch.isspace():
        return ch
    width = cell_len(ch)
    return HIDDEN * width if width > 0 else ""


class Typewriter(LyricAnimation):
    """The active line types itself in, character by character."""

    name = "typewriter"
    label = "Typewriter"
    extra_rows_above = 0
    # Interlude (intro / breaks / outro): the Apple Music countdown — three
    # dots that light up as the gap's remaining seconds run out.
    interlude_mode = "three_dots"

    def render(self, state: AnimState) -> AnimFrame:
        text = state.line_text
        accent = f"bold {state.accent_hex}"
        if not text:
            return self.frame(Text(""))
        if not state.words:
            # No word data: nothing to type against, so show the whole line.
            return self.frame(Text(text, style=accent))

        typed = typed_boundary(state)
        line = Text()
        line.append(text[:typed], style=accent)
        if typed >= len(text):
            return self.frame(line)

        # The caret rides the next visible character; blank runs of spaces
        # between words are skipped so it never blinks in a word gap.
        caret_col = typed
        while caret_col < len(text) and text[caret_col].isspace():
            caret_col += 1
        show_caret = caret_col < len(text) and caret_on(state.sync_pos)

        for i in range(typed, len(text)):
            ch = text[i]
            if show_caret and i == caret_col:
                line.append(CARET, style=accent)
                extra = cell_len(ch) - 1
                if extra > 0:
                    line.append(HIDDEN * extra)
            else:
                line.append(_hidden(ch))
        return self.frame(line)
