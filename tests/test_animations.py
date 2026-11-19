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

from sweep_lyrics.animations.bounce import (
    BOUNCE_LANE_ROWS,
    LANE_BLANK as BOUNCE_BLANK,
    WordBounce,
    active_word_span,
    bounce_position,
    bounce_progress,
)
from sweep_lyrics.animations.base import (
    AnimFrame,
    AnimState,
    LyricAnimation,
    WordSpan,
    default_syllables,
    resolve_word_spans,
    synthesize_word_spans,
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
from sweep_lyrics.animations.typewriter import (
    CARET,
    HIDDEN,
    Typewriter,
    caret_on,
    typed_boundary,
)
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


class TestSynthesizedWordSpans(unittest.TestCase):
    """Line-level-only sources still get per-word onsets for pointer anims."""

    def test_empty_text(self):
        self.assertEqual(synthesize_word_spans("", 0.0, 3.0), ())
        self.assertEqual(synthesize_word_spans("   ", 0.0, 3.0), ())

    def test_one_span_per_word(self):
        text = "aku mau kamu"
        spans = synthesize_word_spans(text, 10.0, 13.0)
        self.assertEqual(len(spans), 3)
        self.assertEqual([s.text for s in spans], ["aku", "mau", "kamu"])
        self.assertEqual([(s.char_from, s.char_to) for s in spans],
                         [(0, 3), (4, 7), (8, 12)])

    def test_spans_are_contiguous_and_inside_the_line(self):
        text = "jangan bimbang karena aku cuma satu"
        spans = synthesize_word_spans(text, 5.0, 9.0)
        self.assertAlmostEqual(spans[0].onset, 5.0, places=6)
        for a, b in zip(spans, spans[1:]):
            self.assertAlmostEqual(a.end, b.onset, places=6)
        self.assertLessEqual(spans[-1].end, 9.0 + 1e-9)

    def test_longer_word_gets_more_time(self):
        spans = synthesize_word_spans(
            "di hatimu", 0.0, 2.0, syllable_fn=lambda w: len(w), sec_per_syllable=1.0
        )
        self.assertGreater(spans[1].duration, spans[0].duration)

    def test_break_is_capped_by_the_syllable_estimate(self):
        # 30 s gap after a 2-word line: words must not crawl for 30 s.
        spans = synthesize_word_spans(
            "aku dia", 0.0, 30.0,
            syllable_fn=lambda w: 2, sec_per_syllable=0.5,
        )
        self.assertLessEqual(spans[-1].end, 2.0 + 1e-9)

    def test_short_gap_keeps_the_real_window(self):
        spans = synthesize_word_spans(
            "aku dia", 4.0, 5.0,
            syllable_fn=lambda w: 2, sec_per_syllable=5.0,
        )
        self.assertAlmostEqual(spans[-1].end, 5.0, places=6)

    def test_unknown_end_falls_back_to_the_estimate(self):
        spans = synthesize_word_spans("aku dia", 7.0, 7.0, syllable_fn=lambda w: 2)
        self.assertGreater(spans[-1].end, 7.0)

    def test_no_duplicate_char_ranges(self):
        text = "a b c d"
        spans = synthesize_word_spans(text, 0.0, 4.0)
        seen = set()
        for s in spans:
            self.assertNotIn((s.char_from, s.char_to), seen)
            seen.add((s.char_from, s.char_to))


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

    def test_bobs_across_lane_rows_while_singing(self):
        rest = max_subrow(DOT_LANE_ROWS)
        start, _ = arc_window(_two_word_state(word_idx=0, sync_pos=10.1))
        # With amplitude 4, the bob reaches across terminal rows (row 0 and row 1)
        subrows = [dot_subrow(_two_word_state(word_idx=0, sync_pos=10.0 + i * 0.1))
                   for i in range(10)]
        rows_visited = {y // SUBPIXELS_PER_ROW for y in subrows}
        self.assertGreater(len(rows_visited), 1)

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

    def test_line_follows_karaoke_progress(self):
        # The row is a progress marker, not a static highlight: words up to
        # and including the active one are accent, the rest stay white until
        # their onset. This mirrors the WordBounce behaviour.
        spans = (WordSpan(0.0, 0.4, "hello", 0, 5),
                 WordSpan(0.5, 0.9, "big", 6, 9),
                 WordSpan(1.0, 1.4, "world", 10, 15))
        text = "hello big world"
        dot = BouncingDot()
        accent = "bold #00e5ff"

        line = dot.render(_mk_state(text, spans, word_idx=-1,
                                    sync_pos=-0.1)).line
        self.assertEqual(_style_at(line, 0), "bold white")
        self.assertEqual(_style_at(line, 6), "bold white")
        self.assertEqual(_style_at(line, 10), "bold white")

        line = dot.render(_mk_state(text, spans, word_idx=0,
                                    sync_pos=0.1)).line
        self.assertEqual(_style_at(line, 0), accent)
        self.assertEqual(_style_at(line, 6), "bold white")
        self.assertEqual(_style_at(line, 10), "bold white")

        line = dot.render(_mk_state(text, spans, word_idx=1,
                                    sync_pos=0.6)).line
        self.assertEqual(_style_at(line, 0), accent)
        self.assertEqual(_style_at(line, 6), accent)
        self.assertEqual(_style_at(line, 10), "bold white")

        line = dot.render(_mk_state(text, spans, word_idx=2,
                                    sync_pos=1.1)).line
        self.assertEqual(_style_at(line, 10), accent)

    def test_line_without_word_data_stays_white(self):
        frame = BouncingDot().render(_no_word_state())
        self.assertEqual(_style_at(frame.line, 0), "bold white")

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

    def test_dot_colour_is_constant(self):
        # One accent style at rest, mid-jump and at the lane top: the dot
        # never fades to grey.
        dot = BouncingDot()
        styles = set()
        for pos in (10.1, 10.5, 10.9):
            state = _two_word_state(word_idx=0, sync_pos=pos)
            frame = dot.render(state)
            for row in frame.above:
                if DOT_GLYPH not in row.plain:
                    continue
                spanned = [str(sp.style) for sp in row.spans
                           if DOT_GLYPH in row.plain[sp.start:sp.end]]
                styles.update(spanned or [str(row.style)])
        self.assertEqual(styles, {"bold #00e5ff"})

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


def _style_at(line, offset):
    """Style string in effect at a character offset of a rich ``Text``."""
    for span in line.spans:
        if span.start <= offset < span.end:
            return str(span.style)
    return str(line.style)


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


class TestAnimationSettings(unittest.TestCase):
    """The lyric style lives in settings and survives the settings panel.

    Regression: 'v' cycled the style but wrote only the module global, so
    opening settings and pressing Enter re-applied the stale on-disk value and
    silently undid the cycle.
    """

    def setUp(self):
        from sweep_lyrics import app
        self.app = app
        self._anim = app.ANIMATION
        self._settings = dict(app.SETTINGS)

    def tearDown(self):
        self.app.ANIMATION = self._anim
        self.app.animation_instance()
        self.app.SETTINGS.clear()
        self.app.SETTINGS.update(self._settings)

    def test_v_is_no_longer_a_keybinding(self):
        # 'v' was removed: the style is picked in settings now.
        import io
        from contextlib import redirect_stdout
        src = __import__("inspect").getsource(self.app.show_help)
        self.assertNotIn('("v",', src)

    def test_animation_has_its_own_category(self):
        # Not buried under "Karaoke / wipe" any more.
        keys = [k for k, _l, _items in self.app._SETTINGS_CATEGORIES]
        self.assertIn("animation", keys)
        items = dict((i[0], i) for i in self.app._SETTINGS_BY_KEY["animation"][1])
        self.assertIn("animation", items)
        self.assertEqual(items["animation"][2], "str")  # stored as a name
        self.assertEqual(items["animation"][3], "wipe")

    def test_animation_is_top_level_menu_entry(self):
        self.assertIn("animation", self.app._SETTINGS_TOP)
        self.assertNotIn("animation", self.app._SETTINGS_ADVANCED)

    def test_default_settings_animation_is_a_valid_name(self):
        s = self.app._default_settings()
        self.assertIn(s["animation"], self.app.ANIMATION_NAMES)

    def test_cycle_writes_the_settings_dict(self):
        # The whole point: 'v' must persist the choice where 'o' -> Enter reads.
        self.app.SETTINGS["animation"] = self.app.ANIMATION
        before = self.app.SETTINGS["animation"]
        self.app.cycle_animation()
        self.assertEqual(self.app.SETTINGS["animation"], self.app.ANIMATION)
        self.assertNotEqual(self.app.SETTINGS["animation"], before)

    def test_apply_settings_reads_the_name(self):
        for name in self.app.ANIMATION_NAMES:
            self.app.apply_settings({"animation": name})
            self.assertEqual(self.app.ANIMATION, name)
            self.assertEqual(self.app.animation_instance().name, name)

    def test_unknown_name_falls_back_to_default(self):
        self.app.apply_settings({"animation": "does-not-exist"})
        self.assertEqual(self.app.ANIMATION, self.app.ANIMATION_NAMES[0])

    def test_legacy_integer_index_migrates(self):
        # settings.json written before the name change stored an int index.
        import json
        import tempfile
        from pathlib import Path
        real = self.app.SETTINGS_FILE
        try:
            with tempfile.TemporaryDirectory() as d:
                self.app.SETTINGS_FILE = Path(d) / "settings.json"
                self.app.SETTINGS_FILE.write_text(json.dumps({"animation": 1}))
                loaded = self.app.load_settings()
                self.assertEqual(loaded["animation"], self.app.ANIMATION_NAMES[1])
                # ...and it is now the name, not an index.
                self.assertIsInstance(loaded["animation"], str)
        finally:
            self.app.SETTINGS_FILE = real

    def test_garbage_animation_value_falls_back(self):
        import json
        import tempfile
        from pathlib import Path
        real = self.app.SETTINGS_FILE
        try:
            with tempfile.TemporaryDirectory() as d:
                self.app.SETTINGS_FILE = Path(d) / "settings.json"
                self.app.SETTINGS_FILE.write_text(json.dumps({"animation": "bogus"}))
                self.assertEqual(self.app.load_settings()["animation"],
                                 self.app.ANIMATION_NAMES[0])
        finally:
            self.app.SETTINGS_FILE = real

    def test_labels_cover_every_name(self):
        for name in self.app.ANIMATION_NAMES:
            self.assertIn(name, self.app.ANIMATION_LABELS)
            self.assertTrue(self.app.ANIMATION_LABELS[name])

    def test_labels_fit_the_settings_value_column(self):
        # "‹ Bouncing dot ›" must fit the wider value column (17), or the
        # settings row truncates it to "Bouncing d…".
        for name in self.app.ANIMATION_NAMES:
            label = self.app.ANIMATION_LABELS[name]
            self.assertLessEqual(len(label) + 4, 17, f"{label!r} too wide")

    def test_set_animation_records_the_choice(self):
        self.app.SETTINGS.clear()
        self.app.set_animation("dot")
        self.assertEqual(self.app.SETTINGS["animation"], "dot")

    def test_env_override_wins_and_survives_a_reapply(self):
        # SWEEP_ANIMATION is for a single run (no disk write) and must not be
        # dropped by a later apply_settings() — e.g. after the settings panel.
        import os
        key = self.app.animations.ENV_VAR
        old = os.environ.get(key)
        try:
            os.environ[key] = "bounce"
            s = {"animation": "wipe"}
            self.app.apply_settings(s)
            self.assertEqual(self.app.ANIMATION, "bounce")
            self.app.apply_settings(s)          # called again later
            self.assertEqual(self.app.ANIMATION, "bounce")
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.app.apply_settings({"animation": "wipe"})

    def test_invalid_env_override_falls_back_to_the_saved_choice(self):
        import os
        key = self.app.animations.ENV_VAR
        old = os.environ.get(key)
        try:
            os.environ[key] = "not-a-style"
            self.app.apply_settings({"animation": "dot"})
            self.assertEqual(self.app.ANIMATION, "dot")
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


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

    def test_bounce_reserves_one_row(self):
        self.app.WORD_MODE = False
        self.app.set_animation("bounce")
        self.assertEqual(self.app._anim_lane_rows(24), BOUNCE_LANE_ROWS)
        # Bounce does not shrink on a short terminal: still one row.
        self.assertEqual(self.app._anim_lane_rows(5), BOUNCE_LANE_ROWS)

    def test_per_word_mode_reserves_no_lane(self):
        # Cycling animations in per-word mode must not move the big word.
        self.app.WORD_MODE = True
        for name in ("wipe", "dot", "bounce", "none", "typewriter"):
            self.app.set_animation(name)
            self.assertEqual(self.app._anim_lane_rows(24), 0)


class TestTypewriter(unittest.TestCase):
    """The active line types itself in, one character at a time."""

    TEXT = "hello world"
    SPANS = (
        WordSpan(0.0, 1.0, "hello", 0, 5),
        WordSpan(1.0, 2.0, "world", 6, 11),
    )

    def _state(self, sync_pos):
        return _mk_state(self.TEXT, self.SPANS, word_idx=0, sync_pos=sync_pos)

    def _visible(self, line):
        # Drop placeholders, the caret and word gaps: what the user reads.
        return (line.plain.replace(HIDDEN, "").replace(CARET, "")
                .replace(" ", ""))

    def test_config(self):
        tw = Typewriter()
        self.assertEqual(tw.name, "typewriter")
        self.assertEqual(tw.label, "Typewriter")
        self.assertEqual(tw.extra_rows_above, 0)
        self.assertEqual(tw.interlude_mode, "three_dots")
        # The label has to fit the settings value column.
        self.assertLessEqual(len(tw.label) + 4, 17)

    def test_registered(self):
        from sweep_lyrics import animations
        self.assertIn("typewriter", animations.animation_names())
        self.assertEqual(animations.get_animation("typewriter").name, "typewriter")

    def test_nothing_typed_before_the_first_onset(self):
        state = _mk_state(self.TEXT, self.SPANS, word_idx=-1, sync_pos=-1.0)
        line = Typewriter().render(state).line
        self.assertEqual(self._visible(line), "")

    def test_partial_word_reveals_its_leading_characters(self):
        # "hello" spans 0..1 s; half way through, ~3 of its 5 letters are up.
        line = Typewriter().render(self._state(0.5)).line
        self.assertTrue(self._visible(line).startswith("hel"))
        self.assertFalse(self._visible(line).startswith("hell"))

    def test_first_character_appears_at_the_onset(self):
        self.assertEqual(self._visible(Typewriter().render(self._state(0.0)).line), "h")

    def test_word_is_fully_typed_at_its_end(self):
        line = Typewriter().render(self._state(1.0)).line
        self.assertTrue(self._visible(line).startswith("hello"))

    def test_whole_line_typed_once_the_last_word_ends(self):
        line = Typewriter().render(self._state(2.0)).line
        self.assertEqual(line.plain, self.TEXT)
        self.assertNotIn(HIDDEN, line.plain)

    def test_trailing_punctuation_is_revealed_with_the_last_word(self):
        text = "go now!"
        spans = (WordSpan(0.0, 1.0, "go", 0, 2), WordSpan(1.0, 2.0, "now", 3, 6))
        state = _mk_state(text, spans, word_idx=1, sync_pos=2.0)
        self.assertEqual(Typewriter().render(state).line.plain, text)

    def test_row_width_never_shifts_as_it_fills_in(self):
        expected = cell_len(self.TEXT)
        for t in (-1.0, 0.0, 0.3, 0.7, 1.0, 1.5, 2.0, 99.0):
            line = Typewriter().render(self._state(t)).line
            self.assertEqual(cell_len(line.plain), expected, f"t={t}")

    def test_word_gaps_stay_visible_while_words_are_blank(self):
        line = Typewriter().render(self._state(0.2)).line
        # The space between the words is a real space, not a braille blank.
        self.assertIn(" ", line.plain)

    def test_typed_text_is_accent_coloured(self):
        line = Typewriter().render(self._state(0.5)).line
        self.assertIn("#00e5ff", _style_at(line, 0))

    def test_no_word_data_shows_the_whole_line(self):
        state = _mk_state("no words here", (), word_idx=-1, sync_pos=10.0)
        self.assertEqual(Typewriter().render(state).line.plain, "no words here")

    def test_empty_line_is_safe(self):
        state = _mk_state("", (), word_idx=-1, sync_pos=0.0)
        self.assertEqual(Typewriter().render(state).line.plain, "")

    def test_caret_blinks_off_sync_pos(self):
        self.assertTrue(caret_on(0.0))     # phase 0 -> on
        self.assertFalse(caret_on(0.4))    # 0.64 -> off
        self.assertTrue(caret_on(1.5))     # 2.4 -> 0.4 -> on
        # Frozen clock => identical frame, no drift while paused.
        a = Typewriter().render(self._state(0.5)).line.plain
        b = Typewriter().render(self._state(0.5)).line.plain
        self.assertEqual(a, b)

    def test_caret_sits_on_the_next_character(self):
        line = Typewriter().render(self._state(0.0)).line  # caret on, "h" typed
        self.assertIn(CARET, line.plain)
        self.assertEqual(line.plain.index(CARET), 1)

    def test_no_caret_once_the_line_is_complete(self):
        line = Typewriter().render(self._state(2.0)).line
        self.assertNotIn(CARET, line.plain)

    def test_boundary_is_monotonic(self):
        prev = -1
        for i in range(0, 41):
            t = i * 0.05
            b = typed_boundary(self._state(t))
            self.assertGreaterEqual(b, prev, f"t={t}")
            prev = b


class TestWordBounce(unittest.TestCase):
    """The active word hops between the lane row and the lyric row."""

    def test_config(self):
        bounce = WordBounce()
        self.assertEqual(bounce.name, "bounce")
        self.assertEqual(bounce.label, "Word bounce")
        self.assertEqual(bounce.extra_rows_above, BOUNCE_LANE_ROWS)
        # The lane is always one row, whatever the terminal height.
        self.assertEqual(bounce.lane_rows_for(24), BOUNCE_LANE_ROWS)
        self.assertEqual(bounce.lane_rows_for(4), BOUNCE_LANE_ROWS)
        self.assertEqual(bounce.lane_rows_for(None), BOUNCE_LANE_ROWS)

    def test_progress_tracks_the_sung_word(self):
        # "hello" is held 10.0..10.3; progress is 0 at the onset, 1 at the end.
        self.assertAlmostEqual(
            bounce_progress(_two_word_state(word_idx=0, sync_pos=10.0)), 0.0)
        self.assertAlmostEqual(
            bounce_progress(_two_word_state(word_idx=0, sync_pos=10.15)), 0.5)
        self.assertAlmostEqual(
            bounce_progress(_two_word_state(word_idx=0, sync_pos=10.3)), 1.0)

    def test_progress_clamps_and_is_none_without_a_word(self):
        self.assertEqual(bounce_progress(_no_word_state()), None)
        self.assertEqual(
            bounce_progress(_two_word_state(word_idx=-1, sync_pos=9.0)), None)
        # Past the word's end the progress saturates at 1, never overshoots.
        self.assertEqual(
            bounce_progress(_two_word_state(word_idx=0, sync_pos=99.0)), 1.0)

    def test_position_switches_at_mid_hop(self):
        self.assertEqual(bounce_position(0.0), "lane")
        self.assertEqual(bounce_position(0.49), "lane")
        self.assertEqual(bounce_position(0.5), "line")
        self.assertEqual(bounce_position(1.0), "line")
        self.assertEqual(bounce_position(None), "lane")

    def test_word_is_up_in_the_lane_at_the_onset(self):
        frame = WordBounce().render(_two_word_state(word_idx=0, sync_pos=10.0))
        self.assertEqual(len(frame.above), BOUNCE_LANE_ROWS)
        # The word "hello" is drawn in the lane row, in its own columns.
        self.assertIn("hello", frame.above[0].plain)
        # ...and hollowed out of the lyric row, so it is never drawn twice.
        self.assertNotIn("hello", frame.line.plain)

    def test_word_lands_back_on_the_lyric_row(self):
        frame = WordBounce().render(_two_word_state(word_idx=0, sync_pos=10.3))
        # Settled: the word is back on the lyric row, the lane is blank.
        self.assertIn("hello", frame.line.plain)
        self.assertNotIn("hello", frame.above[0].plain)
        self.assertEqual(frame.above[0].plain, BOUNCE_BLANK * 11)

    def test_exactly_one_copy_of_the_word(self):
        # Across the whole hop the word is drawn in exactly one of the two
        # rows — never both, never neither.
        for pos in (10.0, 10.05, 10.14, 10.15, 10.2, 10.3):
            frame = WordBounce().render(
                _two_word_state(word_idx=0, sync_pos=pos))
            in_lane = "hello" in frame.above[0].plain
            in_line = "hello" in frame.line.plain
            self.assertNotEqual(in_lane, in_line)

    def test_lane_width_matches_the_line(self):
        # The alignment guarantee: the lane is exactly as wide as the lyric
        # row, so the shared centring/rail prefix lines the columns up.
        for pos in (10.0, 10.3):
            frame = WordBounce().render(
                _two_word_state(word_idx=0, sync_pos=pos))
            self.assertEqual(cell_len(frame.above[0].plain), 11)
            self.assertEqual(cell_len(frame.line.plain), 11)

    def test_no_trailing_ascii_space(self):
        # rich rstrips whitespace before centring a table cell; the lane must
        # not end in a space or it shifts right in the split layout.
        for pos in (10.0, 10.3):
            frame = WordBounce().render(
                _two_word_state(word_idx=0, sync_pos=pos))
            for row in (frame.above[0], frame.line):
                self.assertEqual(row.plain, row.plain.rstrip())

    def test_no_words_keeps_the_lane(self):
        frame = WordBounce().render(_no_word_state())
        self.assertEqual(len(frame.above), BOUNCE_LANE_ROWS)
        self.assertNotIn("hello", frame.above[0].plain)
        # The line is still rendered, in the accent colour.
        self.assertEqual(frame.line.plain, "no words here")

    def test_same_position_is_deterministic(self):
        # A paused player re-renders the same sync_pos; nothing may move.
        bounce = WordBounce()
        state = _two_word_state(word_idx=0, sync_pos=10.1)
        shots = {tuple(r.plain for r in bounce.render(state).above)
                 for _ in range(20)}
        self.assertEqual(len(shots), 1)

    def test_active_word_span(self):
        state = _two_word_state(word_idx=0, sync_pos=10.1)
        self.assertEqual(active_word_span(state), (0, 5))
        self.assertIsNone(active_word_span(_no_word_state()))


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
