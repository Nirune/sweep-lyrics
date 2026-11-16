#!/usr/bin/env python3
"""Unit tests for sweep-lyrics animations.

Tests are pure Python — no terminal required — so they can run
automatically in CI or before commit.
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "src")

from rich.text import Text
from rich.cells import cell_len

from sweep_lyrics.animations.base import (
    AnimFrame,
    AnimState,
    LyricAnimation,
    WordSpan,
    default_syllables,
    resolve_word_spans,
)
from sweep_lyrics.animations.dot import (
    ARC_LEAD,
    ARC_SEC,
    BouncingDot,
    DOT_BOB_SUBROWS,
    DOT_GLYPH,
    DOT_HEIGHT_SUBROWS,
    DOT_LANE_ROWS,
    DOT_LANE_ROWS_MIN,
    SUBPIXELS_PER_ROW,
    arc_window,
    clamp_subrow,
    dot_column,
    dot_subrow,
    lane_rows_for,
    max_subrow,
    word_center,
)
from sweep_lyrics.animations.none import NoneAnimation
from sweep_lyrics.animations.wipe import Wipe


class TestSyllableCount(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(default_syllables(""), 1)

    def test_single(self):
        self.assertEqual(default_syllables("a"), 1)
        self.assertEqual(default_syllables("i"), 1)

    def test_simple(self):
        self.assertEqual(default_syllables("hello"), 2)  # hel-lo
        self.assertEqual(default_syllables("world"), 1)  # world (one vowel group)
        self.assertEqual(default_syllables("rhythm"), 1)  # y counts

    def test_multi(self):
        self.assertEqual(default_syllables("beautiful"), 3)  # beau-ti-ful
        self.assertEqual(default_syllables("development"), 4)  # de-ve-lop-ment


class TestWordSpans(unittest.TestCase):
    def test_empty_timings(self):
        self.assertEqual(resolve_word_spans("hello", None), ())

    def test_basic(self):
        text = "when the night falls"
        timings = [(0.0, "when"), (0.5, "the"), (1.0, "night"), (2.0, "falls")]
        spans = resolve_word_spans(
            text, timings, 3.0,
            syllable_fn=lambda w: 1, sec_per_syllable=0.5
        )
        self.assertEqual(len(spans), 4)
        self.assertEqual(spans[0].text, "when")
        self.assertEqual(spans[0].char_from, 0)
        self.assertEqual(spans[0].char_to, 4)

    def test_unmatched(self):
        # Romanized or mismatched text should return empty tuple
        text = "romanized line here"
        timings = [(0.0, "when"), (0.5, "the")]
        spans = resolve_word_spans(text, timings, 3.0)
        self.assertEqual(spans, ())

    def test_end_time_honored(self):
        class WordTS(tuple):
            def __new__(cls, s, t, e=None):
                obj = super().__new__(cls, (s, t))
                obj.end = e
                return obj

        text = "go now"
        timings = [WordTS(0.0, "go", 0.2), WordTS(0.5, "now", 1.0)]
        spans = resolve_word_spans(
            text, timings, 2.0,
            syllable_fn=lambda w: 1, sec_per_syllable=0.5
        )
        # First word: real end (0.2) is honored
        self.assertAlmostEqual(spans[0].end, 0.2, places=3)
        # Second word: real end (1.0) is honored
        self.assertAlmostEqual(spans[1].end, 1.0, places=3)


class TestDotLaneGeometry(unittest.TestCase):
    """The lane is 2 rows (1 on a short terminal) and glued to the lyric row."""

    def test_lane_height_is_two_rows(self):
        self.assertEqual(DOT_LANE_ROWS, 2)
        self.assertEqual(DOT_LANE_ROWS_MIN, 1)
        self.assertEqual(lane_rows_for(24), DOT_LANE_ROWS)
        self.assertEqual(lane_rows_for(None), DOT_LANE_ROWS)

    def test_short_terminal_shrinks_to_one_row(self):
        self.assertEqual(lane_rows_for(7), DOT_LANE_ROWS_MIN)
        self.assertEqual(lane_rows_for(4), DOT_LANE_ROWS_MIN)
        # exactly at the threshold the full lane is still kept
        self.assertEqual(lane_rows_for(8), DOT_LANE_ROWS)

    def test_clamp_stays_inside_the_lane(self):
        for rows in (DOT_LANE_ROWS_MIN, DOT_LANE_ROWS):
            self.assertEqual(clamp_subrow(-10.0, rows), 0)
            self.assertEqual(clamp_subrow(999.0, rows), max_subrow(rows))
            for i in range(-20, 40):
                y = clamp_subrow(i, rows)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(y, max_subrow(rows))

    def test_max_subrow_keeps_the_dot_inside(self):
        # 0 .. lane_rows * 4 - DOT_HEIGHT_SUBROWS, per the spec.
        for rows in (DOT_LANE_ROWS_MIN, DOT_LANE_ROWS):
            self.assertEqual(
                max_subrow(rows), rows * SUBPIXELS_PER_ROW - DOT_HEIGHT_SUBROWS)

    def test_arc_window_is_the_jump_before_the_next_word(self):
        start, end = arc_window(_two_word_state(word_idx=0, sync_pos=10.1))
        self.assertAlmostEqual(end, 10.5 - ARC_LEAD)
        self.assertAlmostEqual(start, end - ARC_SEC)

    def test_arc_never_starts_before_the_word(self):
        # A word shorter than the jump window: the arc is clipped to its onset.
        start, end = arc_window(_two_word_state(word_idx=0, sync_pos=10.0,
                                                next_onset=10.08))
        self.assertAlmostEqual(start, 10.0)
        self.assertAlmostEqual(end, 10.08 - ARC_LEAD)
        self.assertLess(end - start, ARC_SEC)

    def test_last_word_never_arcs(self):
        self.assertIsNone(arc_window(_two_word_state(word_idx=1, sync_pos=10.6)))
        self.assertIsNone(arc_window(_no_word_state()))


class TestDotRestAndArc(unittest.TestCase):
    """Rest on the bottom subrows; the lane top is only for the jump."""

    def test_rests_on_the_bottom_subrows_while_singing(self):
        rest = max_subrow(DOT_LANE_ROWS)
        start, _ = arc_window(_two_word_state(word_idx=0, sync_pos=10.1))
        for pos in (10.05, 10.1, start - 0.01):
            y = dot_subrow(_two_word_state(word_idx=0, sync_pos=pos))
            self.assertGreaterEqual(y, rest - DOT_BOB_SUBROWS)
            self.assertLessEqual(y, rest)
            # A full-cell glyph can only land on a terminal row: rest is the
            # lane's bottom row (immediately above the lyric line), never the
            # top row.
            self.assertEqual(y // SUBPIXELS_PER_ROW, DOT_LANE_ROWS - 1)

    def test_arc_reaches_the_lane_top_only_in_the_gap(self):
        start, end = arc_window(_two_word_state(word_idx=0, sync_pos=10.1))
        jump = dot_subrow(_two_word_state(word_idx=0, sync_pos=(start + end) / 2))
        self.assertEqual(jump, 0)  # top subrow == top row
        self.assertEqual(jump // SUBPIXELS_PER_ROW, 0)
        rest = dot_subrow(_two_word_state(word_idx=0, sync_pos=10.1))
        self.assertGreater(jump, -1)
        self.assertLess(jump, rest)

    def test_back_at_the_bottom_when_the_next_word_starts(self):
        landed = dot_subrow(_two_word_state(word_idx=1, sync_pos=10.5))
        self.assertEqual(landed // SUBPIXELS_PER_ROW, DOT_LANE_ROWS - 1)

    def test_short_lane_arc_shrinks_with_it(self):
        self.assertEqual(lane_rows_for(5), DOT_LANE_ROWS_MIN)
        start, end = arc_window(_two_word_state(word_idx=0, sync_pos=10.1))
        jump = dot_subrow(_two_word_state(word_idx=0, sync_pos=(start + end) / 2), 1)
        self.assertEqual(jump, 0)
        self.assertEqual(max_subrow(1), 3)
        # ...and still clamps inside the single row
        self.assertLessEqual(jump, 3)

    def test_lane_is_constant_across_line_states(self):
        # Height depends on the terminal, never on the current line.
        for height in (24, 40, 5):
            rows = lane_rows_for(height)
            for idx, pos in ((0, 10.1), (1, 10.6), (-1, 0.0)):
                dot = BouncingDot().lane(
                    _two_word_state(word_idx=idx, sync_pos=pos, height=height), rows)
                self.assertEqual(len(dot), rows)


class TestDotColumn(unittest.TestCase):
    """Dot x must come only from the renderer's word layout."""

    TEXT = "when the night"
    # spans: when 0..4, the 5..8, night 9..14 (in display cells)
    SPANS = (
        WordSpan(0.0, 0.4, "when", 0, 4),
        WordSpan(0.5, 0.9, "the", 5, 8),
        WordSpan(1.0, 1.6, "night", 9, 14),
    )

    def test_word_center(self):
        self.assertEqual(word_center((0, 4)), 2)
        self.assertEqual(word_center((9, 14)), 11)

    def test_rests_on_the_active_word(self):
        state = _mk_state(self.TEXT, self.SPANS, word_idx=1, sync_pos=0.6)
        self.assertEqual(dot_column(state), 6)  # centre of "the"

    def test_before_first_onset_parks_on_first_word(self):
        state = _mk_state(self.TEXT, self.SPANS, word_idx=-1, sync_pos=-0.5)
        self.assertEqual(dot_column(state), 2)  # centre of "when"

    def test_after_last_word_stays_on_last_word(self):
        state = _mk_state(self.TEXT, self.SPANS, word_idx=2, sync_pos=2.0)
        self.assertEqual(dot_column(state), 11)  # centre of "night"

    def test_jump_carries_it_to_the_next_word(self):
        # Window for "when": [0.5 - ARC_LEAD - ARC_SEC, 0.5 - ARC_LEAD].
        state = _mk_state(self.TEXT, self.SPANS, word_idx=0, sync_pos=0.34)
        start, end = arc_window(state)
        self.assertLess(start, 0.34)
        self.assertGreater(end, 0.34)
        self.assertEqual(dot_column(state), 4)  # halfway between 2 and 6

    def test_lands_before_the_next_word_lights_up(self):
        state = _mk_state(self.TEXT, self.SPANS, word_idx=0, sync_pos=0.45)
        self.assertEqual(dot_column(state), 6)  # already on "the"

    def test_no_words(self):
        state = _mk_state(self.TEXT, (), word_idx=-1, sync_pos=0.0)
        self.assertEqual(dot_column(state), 0)

    def test_uses_renderer_columns_not_char_indices(self):
        # A wide glyph before the word shifts its column while the code-point
        # index stays small; the dot must follow the *column*.
        text = "🎵 x"
        spans = (WordSpan(0.0, 1.0, "x", 2, 3),)
        state = _mk_state(text, spans, word_idx=0, sync_pos=0.0,
                          word_cols=((3, 4),), line_cols=4)
        self.assertEqual(dot_column(state), 3)  # cell centre of 3..4

    def test_lane_width_matches_line_width(self):
        # The alignment guarantee: a lane exactly as wide as the lyric row,
        # so the shared centring/rail prefix lines the columns up.
        state = _mk_state(self.TEXT, self.SPANS, word_idx=1, sync_pos=0.6,
                          time_to_next=0.4, next_onset=1.0)
        frame = BouncingDot().render(state)
        self.assertEqual(len(frame.above), DOT_LANE_ROWS)
        for row in frame.above:
            self.assertEqual(cell_len(row.plain), 14)  # == line width


class TestBouncingDot(unittest.TestCase):
    def test_config(self):
        dot = BouncingDot()
        self.assertEqual(dot.name, "dot")
        self.assertEqual(dot.label, "Bouncing dot")
        self.assertEqual(dot.extra_rows_above, DOT_LANE_ROWS)
        # The layout asks the style for the lane height before rendering.
        self.assertEqual(dot.lane_rows_for(24), DOT_LANE_ROWS)
        self.assertEqual(dot.lane_rows_for(5), DOT_LANE_ROWS_MIN)
        self.assertEqual(dot.lane_rows_for(None), DOT_LANE_ROWS)

    def test_render_with_word(self):
        state = _two_word_state(word_idx=0, sync_pos=10.1)
        frame = BouncingDot().render(state)

        self.assertEqual(frame.line.plain, "hello world")
        self.assertEqual(len(frame.above), DOT_LANE_ROWS)
        # Exactly one dot, on the active word ("hello", cols 0..5), resting on
        # the lane row that touches the lyric line.
        dot_cells = [(c, r) for r, row in enumerate(frame.above)
                     for c, ch in enumerate(row.plain) if ch == DOT_GLYPH]
        self.assertEqual(len(dot_cells), 1)
        col, row = dot_cells[0]
        self.assertTrue(0 <= col <= 4, f"dot escaped the active word: {col}")
        self.assertEqual(row, DOT_LANE_ROWS - 1)

    def test_one_dot_row_even_at_the_lane_top(self):
        start, end = arc_window(_two_word_state(word_idx=0, sync_pos=10.1))
        state = _two_word_state(word_idx=0, sync_pos=(start + end) / 2)
        frame = BouncingDot().render(state)
        rows = [r for r, row in enumerate(frame.above) if DOT_GLYPH in row.plain]
        self.assertEqual(rows, [0])

    def test_lane_has_no_trailing_ascii_space(self):
        # rich centres a table cell with Lines.justify("center"), which calls
        # rstrip() on the line first; a lane ending in spaces is measured short
        # and shifts right in the split layout. Empty cells must not be " ".
        state = _two_word_state(word_idx=0, sync_pos=10.1)
        for row in BouncingDot().render(state).above:
            plain = row.plain
            self.assertEqual(plain, plain.rstrip(), f"lane would be shifted: {plain!r}")
            self.assertEqual(cell_len(plain), 11)

    def test_no_trailing_ascii_space_before_dot(self):
        # The dot sits mid-row: blanks after it must not be stripped either.
        state = _two_word_state(word_idx=0, sync_pos=10.1)
        for row in BouncingDot().render(state).above:
            self.assertNotIn(" ", row.plain)

    def test_render_no_words(self):
        state = _no_word_state()
        frame = BouncingDot().render(state)
        self.assertEqual(len(frame.above), DOT_LANE_ROWS)  # lane still reserves rows
        for row in frame.above:
            self.assertNotIn(DOT_GLYPH, row.plain)

    def test_short_terminal_renders_one_row(self):
        frame = BouncingDot().render(_two_word_state(word_idx=0, sync_pos=10.1,
                                                     height=5))
        self.assertEqual(len(frame.above), DOT_LANE_ROWS_MIN)
        self.assertEqual([r for r, row in enumerate(frame.above)
                          if DOT_GLYPH in row.plain], [0])

    def test_position_matches_the_rendered_glyph(self):
        state = _two_word_state(word_idx=0, sync_pos=10.1)
        dot = BouncingDot()
        col, row = dot.position(state)
        frame = dot.render(state)
        self.assertEqual(frame.above[row].plain[col], DOT_GLYPH)

    def test_position_matches_during_the_arc(self):
        start, end = arc_window(_two_word_state(word_idx=0, sync_pos=10.1))
        state = _two_word_state(word_idx=0, sync_pos=(start + end) / 2)
        dot = BouncingDot()
        col, row = dot.position(state)
        frame = dot.render(state)
        self.assertEqual(row, 0)
        self.assertEqual(frame.above[row].plain[col], DOT_GLYPH)

    def test_position_without_words(self):
        dot = BouncingDot()
        col, row = dot.position(_no_word_state())
        self.assertEqual(col, 0)
        self.assertEqual(row, DOT_LANE_ROWS - 1)


def _two_word_state(*, word_idx, sync_pos, height=24, next_onset=10.5):
    """Two sung words: 10.0..10.3 "hello", ``next_onset``..+0.3 "world"."""
    words = (
        WordSpan(10.0, 10.3, "hello", 0, 5),
        WordSpan(next_onset, next_onset + 0.3, "world", 6, 11),
    )
    has_next = 0 <= word_idx < len(words) - 1
    return AnimState(
        pos=sync_pos, sync_pos=sync_pos, line_idx=0, line_text="hello world",
        line_start=10.0, line_end=12.0, wipe_span=2.0, frac=0.5,
        accent_hex="#00e5ff", words=words if word_idx >= 0 else (),
        word_idx=word_idx, word_progress=0.5,
        next_onset=words[word_idx + 1].onset if has_next else None,
        time_to_next=(words[word_idx + 1].onset - sync_pos) if has_next else None,
        width=80, height=height, line_cols=11,
        word_cols=((0, 5), (6, 11)),
    )


def _no_word_state(height=24):
    return AnimState(
        pos=10.0, sync_pos=10.0, line_idx=0, line_text="no words here",
        line_start=10.0, line_end=12.0, wipe_span=2.0, frac=0.5,
        accent_hex="#00e5ff", words=(), word_idx=-1,
        next_onset=None, time_to_next=None, width=80, height=height,
    )


def _mk_state(text, spans, *, word_idx, sync_pos, word_cols=None,
              line_cols=None, time_to_next=0.0, next_onset=None, width=80):
    """Build an AnimState the way the renderer would, layout included."""
    if word_cols is None:
        word_cols = tuple((w.char_from, w.char_to) for w in spans)
    if line_cols is None:
        line_cols = sum(1 for _ in text)
    return AnimState(
        pos=sync_pos, sync_pos=sync_pos, line_idx=0, line_text=text,
        line_start=0.0, line_end=3.0, wipe_span=3.0, frac=0.5,
        accent_hex="#00e5ff", words=tuple(spans), word_idx=word_idx,
        word_progress=0.5, next_onset=next_onset, time_to_next=time_to_next,
        width=width, height=24, line_cols=line_cols, word_cols=tuple(word_cols),
    )


class TestLineLayout(unittest.TestCase):
    """The renderer exposes word columns in display cells, not code points."""

    def test_ascii(self):
        from sweep_lyrics import app

        text = "when the night"
        spans = (WordSpan(0.0, 1.0, "when", 0, 4), WordSpan(1.0, 2.0, "the", 5, 8))
        line_cols, word_cols = app.line_layout(text, spans)
        self.assertEqual(line_cols, 14)
        self.assertEqual(word_cols, ((0, 4), (5, 8)))

    def test_wide_and_emoji(self):
        from sweep_lyrics import app

        # "🎵 x": the emoji is two cells wide, so "x" starts at column 3.
        text = "🎵 x"
        spans = (WordSpan(0.0, 1.0, "x", 2, 3),)
        line_cols, word_cols = app.line_layout(text, spans)
        self.assertEqual(line_cols, 4)
        self.assertEqual(word_cols, ((3, 4),))

    def test_empty(self):
        from sweep_lyrics import app

        self.assertEqual(app.line_layout("", ()), (0, ()))


class TestAnimLaneRows(unittest.TestCase):
    """The layout reserves the lane rows the active style actually draws."""

    def setUp(self):
        from sweep_lyrics import app
        self.app = app
        self._word_mode = app.WORD_MODE
        self._anim = app.ANIMATION

    def tearDown(self):
        self.app.WORD_MODE = self._word_mode
        self.app.set_animation(self._anim)

    def test_dot_reserves_two_rows_and_one_when_short(self):
        self.app.WORD_MODE = False
        self.app.set_animation("dot")
        self.assertEqual(self.app._anim_lane_rows(24), DOT_LANE_ROWS)
        self.assertEqual(self.app._anim_lane_rows(5), DOT_LANE_ROWS_MIN)

    def test_styles_without_a_lane_reserve_nothing(self):
        self.app.WORD_MODE = False
        for name in ("wipe", "none"):
            self.app.set_animation(name)
            self.assertEqual(self.app._anim_lane_rows(24), 0)

    def test_per_word_mode_reserves_no_lane(self):
        # Cycling animations in per-word mode must not move the big word.
        self.app.WORD_MODE = True
        for name in ("wipe", "dot", "none"):
            self.app.set_animation(name)
            self.assertEqual(self.app._anim_lane_rows(24), 0)


class TestNoneAnimation(unittest.TestCase):
    def test_config(self):
        anim = NoneAnimation()
        self.assertEqual(anim.name, "none")
        self.assertEqual(anim.label, "Static")
        self.assertEqual(anim.extra_rows_above, 0)

    def test_render(self):
        state = AnimState(
            pos=10.0, sync_pos=10.0, line_idx=0, line_text="test",
            line_start=10.0, line_end=12.0, wipe_span=2.0, frac=0.5,
            accent_hex="#00e5ff", words=(), word_idx=-1,
            next_onset=None, time_to_next=None, width=80, height=24,
        )
        anim = NoneAnimation()
        frame = anim.render(state)

        self.assertEqual(frame.line.plain, "test")
        self.assertEqual(len(frame.above), 0)


class TestWipeAnimation(unittest.TestCase):
    def test_config(self):
        anim = Wipe()
        self.assertEqual(anim.name, "wipe")
        self.assertEqual(anim.extra_rows_above, 0)


class TestAnimFrame(unittest.TestCase):
    def test_pad_above(self):
        frame = AnimFrame(
            line=Text("hello"),
            above=[Text("row1"), Text("row2"), Text("row3")]
        )
        # Pad to 2 rows (drop first)
        padded = frame.pad_above(2)
        self.assertEqual(len(padded.above), 2)
        self.assertEqual(padded.above[0].plain, "row2")
        self.assertEqual(padded.above[1].plain, "row3")

        # Pad to 5 rows (add 2 blank)
        padded = frame.pad_above(5)
        self.assertEqual(len(padded.above), 5)
        self.assertEqual(padded.above[0].plain, "")
        self.assertEqual(padded.above[1].plain, "")
        self.assertEqual(padded.above[2].plain, "row1")

    def test_pad_above_zero(self):
        frame = AnimFrame(line=Text("hello"), above=[Text("row1")])
        padded = frame.pad_above(0)
        self.assertEqual(len(padded.above), 0)


if __name__ == "__main__":
    # Run with: python -m unittest -v
    unittest.main(verbosity=2)
