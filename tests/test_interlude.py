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

    def test_brightness_follows_the_hop(self):
        # truecolor path: dim grey at the bottom, the accent at the peak
        bottom = app._interlude_lane("#00e5ff", t=0.0)
        peak = app._interlude_lane("#00e5ff", t=0.5 / app.INTERLUDE_HOP_HZ)

        def dot_style(rows):
            return next(r.style for r in rows if r.plain == app.INTERLUDE_DOT_GLYPH)

        self.assertNotEqual(dot_style(bottom), dot_style(peak))
        self.assertEqual(dot_style(peak), "#00e5ff")
        r, g, b = app._hex_to_rgb(dot_style(bottom))
        pr, pg, pb = app._hex_to_rgb("#00e5ff")
        self.assertLess(r + g + b, pr + pg + pb)

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


if __name__ == "__main__":
    unittest.main()
