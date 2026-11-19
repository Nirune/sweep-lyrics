"""Underline: a rule grows under the line as it is sung.

The sung part of the row is drawn in the accent colour *and* underlined, so
the progress reads as a rule that grows beneath the words while the colour
change keeps it legible at a glance. Everything still to be sung stays plain
white. Word gaps stay blank, so each word carries its own segment instead of
one solid bar running the width of the line.

The edge comes from :func:`app.karaoke_lit` — the very same per-cell curve
the karaoke wipe draws — so the rule advances with the wipe's syllable
weights, easing and soft-edge feathering instead of stepping from word to
word. On sources with real word timings the leading cells feather from white
into the accent and the rule switches on where that feather passes halfway;
on line-level sources it follows the wipe's eased, syllable-weighted cut.

No lane rows are reserved: the underline is a text style, not an extra row,
so picking this style never changes the layout height.
"""

from __future__ import annotations

from rich.text import Text

from .base import AnimFrame, AnimState, LyricAnimation

__all__ = [
    "Underline",
    "UNDERLINE_AT",
    "cell_style",
]

# ---- Tunable constants ------------------------------------------------
UNDERLINE_AT = 0.5   # lit fraction at which a cell gets the rule


def cell_style(lit: float, fill, rest, rule: bool = True) -> str:
    """Style for one cell: colour blended by ``lit``, underlined past half.

    ``fill`` is the accent RGB and ``rest`` the unsung white, matching the
    wipe's own mix so the two styles share one gradient. ``rule=False``
    keeps the blended colour but skips the underline, which is how word
    gaps stay blank so each word gets its own segment.
    """
    lit = max(0.0, min(1.0, lit))
    r, g, b = (round(rest[j] + (fill[j] - rest[j]) * lit) for j in range(3))
    style = f"bold #{r:02x}{g:02x}{b:02x}"
    if rule and lit >= UNDERLINE_AT:
        style += " underline"
    return style


class Underline(LyricAnimation):
    """Accent + underline grows under the words as they are sung."""

    name = "underline"
    label = "Underline"
    extra_rows_above = 0
    # Interlude (intro / breaks / outro): the Apple Music countdown — three
    # dots that light up as the gap's remaining seconds run out.
    interlude_mode = "three_dots"

    def render(self, state: AnimState) -> AnimFrame:
        try:
            from .. import app
        except ImportError:  # direct-exec ("sweep-lyrics.py") / flat layout
            import app  # type: ignore

        text = state.line_text
        accent = state.accent_hex
        if not text:
            return self.frame(Text(""))

        line_end = state.line_end if state.line_end > state.line_start else None
        lit = app.karaoke_lit(
            text, state.frac, state.word_timings, state.sync_pos, line_end,
        )
        try:
            fill = app._hex_to_rgb(accent)
        except Exception:
            fill = (255, 255, 255)
        rest = app.KARAOKE_REST_RGB

        line = Text()
        run, run_style = "", None
        for ch, value in zip(text, lit):
            style = cell_style(value, fill, rest, rule=not ch.isspace())
            if style != run_style and run:
                line.append(run, style=run_style)
                run = ""
            run_style = style
            run += ch
        if run:
            line.append(run, style=run_style)
        return self.frame(line)
