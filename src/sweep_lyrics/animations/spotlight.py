"""Spotlight: active word glows bright, rest fades.

Only the active word is drawn in the accent colour at full brightness.
Everything else fades to dim. The active word's leading edge feathers smoothly
based on ``word_progress``, giving the same soft-edge feel as the main wipe
without the per-cell feathering of ``karaoke_lit``.

No lane rows are reserved: this is a text style effect, so switching to it
never changes the layout height.
"""

from __future__ import annotations

from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = [
    "Spotlight",
    "DIM_RGB",
]

# ---- Tunable constants ------------------------------------------------
DIM_RGB = (80, 80, 100)  # muted blue-grey for unlit cells


def _blend(fg: tuple, bg: tuple, t: float) -> tuple:
    """Linear blend between fg (t=1) and bg (t=0)."""
    t = max(0.0, min(1.0, t))
    return tuple(int(fg[i] * t + bg[i] * (1.0 - t)) for i in range(3))


class Spotlight(LyricAnimation):
    """Active word glows bright, rest dims."""

    name = "spotlight"
    label = "Spotlight"
    extra_rows_above = 0
    interlude_mode = "three_dots"

    def render(self, state: AnimState) -> AnimFrame:
        text = state.line_text
        accent = state.accent_hex
        if not text:
            return self.frame(Text(""))

        # Parse accent colour
        try:
            from .. import app
        except ImportError:
            import app  # type: ignore

        try:
            accent_rgb = app._hex_to_rgb(accent)
        except Exception:
            accent_rgb = (255, 255, 255)

        # Determine active word range
        word_idx = state.word_idx
        if 0 <= word_idx < len(state.words):
            w = state.words[word_idx]
            active_start, active_end = w.char_from, w.char_to
            active_progress = state.word_progress
        else:
            active_start, active_end = -1, -1
            active_progress = 0.0

        # Build styled line
        line = Text()
        run, run_style = "", None
        for i, ch in enumerate(text):
            # Active word: use word_progress to blend
            if active_start <= i < active_end:
                value = active_progress
            else:
                value = 0.0

            rgb = _blend(accent_rgb, DIM_RGB, value)
            style = f"bold #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

            if style != run_style and run:
                line.append(run, style=run_style)
                run = ""
            run_style = style
            run += ch

        if run:
            line.append(run, style=run_style)

        return self.frame(line)
