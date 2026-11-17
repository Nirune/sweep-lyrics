"""Karaoke wipe: the original left-to-right colour sweep.

Behaviour is intentionally unchanged. The tuned helpers still live in
:mod:`sweep_lyrics.app` (word-level soft edge, syllable fallback, onset
hybrid); this class is only the adapter that feeds them from the shared
:class:`AnimState`. The import is deferred so ``app`` can import the
registry without a circular import at module load.
"""

from __future__ import annotations

from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = ["Wipe"]


class Wipe(LyricAnimation):
    name = "wipe"
    label = "Karaoke wipe"
    extra_rows_above = 0
    # Interlude (intro / breaks / outro): the Apple Music countdown — three
    # dots that light up as the gap's remaining seconds run out. Lane keeps
    # the default 3-row height.
    interlude_mode = "three_dots"

    def render(self, state: AnimState) -> AnimFrame:
        try:
            from .. import app
        except ImportError:  # direct-exec ("sweep-lyrics.py") / flat layout
            import app  # type: ignore

        line_end = state.line_end if state.line_end > state.line_start else None
        line = app.karaoke_line(
            state.line_text,
            state.frac,
            state.accent_hex,
            word_timings=state.word_timings,
            sync_pos=state.sync_pos,
            line_end=line_end,
        )
        if not isinstance(line, Text):
            line = Text(str(line))
        return self.frame(line)
