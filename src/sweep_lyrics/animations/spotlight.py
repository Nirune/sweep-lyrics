"""Spotlight: a bright window around the active word, rest dimmed.

The active word and its immediate neighbours are drawn in the accent colour
and full brightness; words further away fade toward dim. The fade is a simple
linear ramp based on cell distance from the active word, so the "spotlight"
feels like a soft pool of light moving across the line.

No lane rows are reserved: this is a text style effect, so switching to it
never changes the layout height.
"""

from __future__ import annotations

from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = [
    "Spotlight",
    "SPOTLIGHT_RADIUS",
    "DIM_RGB",
]

# ---- Tunable constants ------------------------------------------------
SPOTLIGHT_RADIUS = 2  # cells of full brightness on each side of active word
DIM_RGB = (80, 80, 100)  # muted blue-grey for words outside the spotlight


def _blend(fg: tuple, bg: tuple, t: float) -> tuple:
    """Linear blend between fg (t=1) and bg (t=0)."""
    t = max(0.0, min(1.0, t))
    return tuple(int(fg[i] * t + bg[i] * (1.0 - t)) for i in range(3))


def _cell_brightness(
    cell_idx: int,
    active_start: int,
    active_end: int,
    radius: int = SPOTLIGHT_RADIUS,
) -> float:
    """Brightness factor (0–1) for a cell given the active word range.

    Cells inside the active word get 1.0. Cells within `radius` cells of the
    active word get a linear falloff. Cells beyond that get 0.0.
    """
    if active_start <= cell_idx < active_end:
        return 1.0

    # Distance from nearest edge of active word
    if cell_idx < active_start:
        dist = active_start - cell_idx
    else:
        dist = cell_idx - active_end + 1

    if dist <= radius:
        return 1.0 - (dist / (radius + 1))
    return 0.0


class Spotlight(LyricAnimation):
    """Bright window follows the active word, rest fades to dim."""

    name = "spotlight"
    label = "Spotlight"
    extra_rows_above = 0
    interlude_mode = "three_dots"

    def render(self, state: AnimState) -> AnimFrame:
        try:
            from .. import app
        except ImportError:
            import app  # type: ignore

        text = state.line_text
        accent = state.accent_hex
        if not text:
            return self.frame(Text(""))

        # Parse accent colour
        try:
            accent_rgb = app._hex_to_rgb(accent)
        except Exception:
            accent_rgb = (255, 255, 255)

        # Determine active word position
        word_idx = state.word_idx
        if 0 <= word_idx < len(state.words):
            w = state.words[word_idx]
            active_start, active_end = w.char_from, w.char_to
        else:
            # No active word (before first / after last) — dim everything
            active_start, active_end = len(text) + 100, len(text) + 100  # past end

        # Build styled line
        line = Text()
        run, run_style = "", None
        for i, ch in enumerate(text):
            bright = _cell_brightness(i, active_start, active_end, SPOTLIGHT_RADIUS)
            rgb = _blend(accent_rgb, DIM_RGB, bright)
            style = f"bold #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

            if style != run_style and run:
                line.append(run, style=run_style)
                run = ""
            run_style = style
            run += ch

        if run:
            line.append(run, style=run_style)

        return self.frame(line)
