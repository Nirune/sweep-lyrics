#!/usr/bin/env python3
"""Unit tests for the interlude dot indicator (the dot shown between lyric
lines during intro / breaks / outro).

Pure functions and pure layout helpers only: no terminal, player or audio
device is needed, so these run in CI and before a commit.
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "src")

from rich.cells import cell_len

from sweep_lyrics import app


class TestInterludeHop(unittest.TestCase):
    def test_phase_bounds_and_period(self):
        hz = app.INTERLUDE_HOP_HZ
        self.assertAlmostEqual(app.interlude_hop_phase(0.0), 0.0)
        self.assertAlmostEqual(app.interlude_hop_phase(0.5 / hz), 1.0)
        self.assertAlmostEqual(app.interlude_hop_phase(1.0 / hz), 0.0, places=6)
        for i in range(60):
            phase = app.interlude_hop_phase(i * 0.037)
            self.assertGreaterEqual(phase, 0.0)
            self.assertLessEqual(phase, 1.0)

    def test_dot_row_stays_inside_lane(self):
        for i in range(200):
            row = app.interlude_dot_row(i * 0.013)
            self.assertGreaterEqual(row, 0)
            self.assertLess(row, app.INTERLUDE_LANE_ROWS)

    def test_dot_row_reaches_every_row(self):
        rows = {app.interlude_dot_row(i * 0.01) for i in range(400)}
        self.assertEqual(rows, set(range(app.INTERLUDE_LANE_ROWS)))

    def test_dot_starts_at_the_bottom_and_tops_out(self):
        # t=0 is the bottom of the hop, half a period later it is the peak
        self.assertEqual(app.interlude_dot_row(0.0), app.INTERLUDE_LANE_ROWS - 1)
        peak_t = 0.5 / app.INTERLUDE_HOP_HZ
        self.assertEqual(app.interlude_dot_row(peak_t), 0)


class TestInterludeLane(unittest.TestCase):
    def setUp(self):
        # These tests exercise the single-dot hop, so force the dot style.
        app.set_animation("dot")

    def test_lane_has_fixed_height(self):
        # The lane never changes size: that is what keeps the rows around the
        # dot from jittering while it jumps.
        for t in (0.0, 0.13, 0.5, 1.0, 2.7, 9.42):
            rows = app._interlude_lane("#00e5ff", t=t)
            self.assertEqual(len(rows), app.INTERLUDE_LANE_ROWS)
            self.assertGreaterEqual(len(rows), 3)

    def test_exactly_one_dot_on_the_curve(self):
        for t in (0.0, 0.2, 0.55, 0.9, 1.4):
            rows = app._interlude_lane("#00e5ff", t=t)
            dot_rows = [i for i, r in enumerate(rows)
                        if r.plain == app.INTERLUDE_DOT_GLYPH]
            self.assertEqual(dot_rows, [app.interlude_dot_row(t)])

    def test_blank_rows_occupy_one_cell_and_are_not_whitespace(self):
        rows = app._interlude_lane("#00e5ff", t=0.0)
        blanks = [r for r in rows if r.plain != app.INTERLUDE_DOT_GLYPH]
        self.assertEqual(len(blanks), app.INTERLUDE_LANE_ROWS - 1)
        for row in blanks:
            self.assertEqual(row.plain, app.INTERLUDE_LANE_BLANK)
            self.assertNotEqual(row.plain, "")       # an empty Text renders 0 rows
            self.assertFalse(row.plain.isspace())    # whitespace gets rstripped
            self.assertEqual(cell_len(row.plain), 1)

    def test_dot_is_centred_and_one_cell_wide(self):
        # No horizontal progress: the dot sits on the centre column at every
        # phase, and the glyph must stay single width or the row would shift.
        for t in (0.0, 0.3, 0.7, 1.1):
            for row in app._interlude_lane("#00e5ff", t=t):
                self.assertEqual(cell_len(row.plain), 1)

    def test_dot_colour_is_constant(self):
        # The single dot keeps the plain accent at every hop phase: no
        # grey -> accent breathing blend.
        def dot_style(rows):
            return next(r.style for r in rows if r.plain == app.INTERLUDE_DOT_GLYPH)

        for t in (0.0, 0.15, 0.5 / app.INTERLUDE_HOP_HZ, 1.1, 4.7):
            self.assertEqual(dot_style(app._interlude_lane("#00e5ff", t=t)),
                             "#00e5ff")

    def test_lane_can_match_the_lyric_lane(self):
        # With the 2-row ``dot`` style the interlude lane shrinks to the same
        # height, so entering a break does not change the block height.
        rows = app._interlude_lane("#00e5ff", t=0.0, lane_rows=2)
        self.assertEqual(len(rows), 2)
        dot_rows = [i for i, r in enumerate(rows)
                    if r.plain == app.INTERLUDE_DOT_GLYPH]
        self.assertEqual(
            dot_rows, [app.interlude_dot_row(0.0, lane=2, peak=1)])

    def test_single_row_lane(self):
        rows = app._interlude_lane("#00e5ff", t=0.3, lane_rows=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].plain, app.INTERLUDE_DOT_GLYPH)


class TestInterludeThreeDots(unittest.TestCase):
    def setUp(self):
        # wipe/none show the Apple countdown instead of a hopping single dot.
        app.set_animation("wipe")

    def _dots_row(self, gap_info):
        """The middle lane row for a countdown at ``gap_info``."""
        rows = app._interlude_lane("#00e5ff", t=0.0, lane_rows=3,
                                   gap_info=gap_info)
        self.assertEqual(len(rows), app.INTERLUDE_LANE_ROWS)
        return rows[app.INTERLUDE_LANE_ROWS // 2]

    def _dot_styles(self, gap_info):
        """Per-dot styles (dots sit at character indices 0, 3, 6)."""
        text = self._dots_row(gap_info)

        def style_at(idx):
            for sp in text.spans:
                if sp.start <= idx < sp.end:
                    return str(sp.style)
            return str(text.style)

        return [style_at(i * 3) for i in range(3)]

    def test_three_dots_lane_is_three_rows(self):
        rows = app._interlude_lane("#00e5ff", t=0.0, lane_rows=3,
                                   gap_info={"rem": 2.0, "is_outro": False})
        self.assertEqual(len(rows), app.INTERLUDE_LANE_ROWS)
        self.assertEqual(len(rows), 3)

    def test_three_dots_are_side_by_side_in_middle(self):
        text = self._dots_row({"rem": 2.0, "is_outro": False})
        want = f"{app.INTERLUDE_DOT_GLYPH}  {app.INTERLUDE_DOT_GLYPH}  {app.INTERLUDE_DOT_GLYPH}"
        self.assertEqual(text.plain, want)

    def test_intro_countdown_lights_dots_left_to_right(self):
        # Gap start (rem 4): every dot colourless. rem 2: dots 1-2 lit.
        # rem 1: all three lit — a countdown to the next sung line.
        acc = "#00e5ff"
        grey = self._dot_styles({"rem": 4.0, "is_outro": False})[0]
        self.assertEqual(len(set(self._dot_styles({"rem": 4.0, "is_outro": False}))), 1)
        self.assertEqual(self._dot_styles({"rem": 2.0, "is_outro": False}),
                         [acc, acc, grey])
        self.assertEqual(self._dot_styles({"rem": 1.0, "is_outro": False}),
                         [acc, acc, acc])

    def test_outro_fades_out_right_to_left(self):
        # Early outro (rem 7): all lit. As rem shrinks, dot 3 fades first,
        # then dot 2; dot 1 holds longest.
        acc = "#00e5ff"
        early = self._dot_styles({"rem": 7.0, "is_outro": True})
        self.assertEqual(early[:2], [acc, acc])
        self.assertNotEqual(early[2], acc)
        late = self._dot_styles({"rem": 1.0, "is_outro": True})
        self.assertNotEqual(late[2], acc)
        self.assertNotEqual(late[1], acc)

    def test_countdown_freezes_with_the_song_clock(self):
        # rem drives the colours, not the wall clock: same gap_info, same row.
        gap = {"rem": 2.0, "is_outro": False}
        a = self._dots_row(gap)
        b = app._interlude_lane("#00e5ff", t=999.0, lane_rows=3,
                                gap_info=gap)[app.INTERLUDE_LANE_ROWS // 2]
        self.assertEqual(a.plain, b.plain)
        self.assertEqual([str(s) for s in a.spans], [str(s) for s in b.spans])


if __name__ == "__main__":
    unittest.main()
