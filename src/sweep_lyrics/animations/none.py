"""Static lyric style: no sweep, no marker — just the active line in the
accent colour. Useful as an accessibility-friendly option and as the
reference implementation for the animation interface."""

from __future__ import annotations

from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = ["NoneAnimation"]


class NoneAnimation(LyricAnimation):
    name = "none"
    label = "Static"
    extra_rows_above = 0
    # Interlude (intro / breaks / outro): the Apple Music countdown — three
    # dots that light up as the gap's remaining seconds run out. Lane keeps
    # the default 3-row height.
    interlude_mode = "three_dots"

    def render(self, state: AnimState) -> AnimFrame:
        return self.frame(Text(state.line_text, style=f"bold {state.accent_hex}"))
