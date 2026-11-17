#!/usr/bin/env python3
"""Pause behaviour: the transport clock must stop, and so must the dots.

Regression tests for "music paused but the lyrics keep cycling and the dot
keeps hopping on its own". Two separate causes:

1. ``InterpolatedClock`` advanced one 30 fps tick per rendered frame even when
   the player was paused (the raw position was frozen, but the free-running
   counter was not).
2. The dot hop sampled the wall clock, so it animated regardless of playback.

Pure Python: no terminal, player or audio device needed.
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "src")

from sweep_lyrics import app
from sweep_lyrics.animations.base import AnimState, WordSpan
from sweep_lyrics.animations.dot import (
    ARC_LEAD,
    ARC_SEC,
    BouncingDot,
    DOT_GLYPH,
    DOT_LANE_ROWS,
)


class TestInterpolatedClock(unittest.TestCase):
    def test_advances_while_playing(self):
        clock = app.InterpolatedClock()
        first = clock.next_frame(100.0, playing=True)
        second = clock.next_frame(100.0, playing=True)
        third = clock.next_frame(100.0, playing=True)
        self.assertEqual(first, 100.0)
        self.assertAlmostEqual(second - first, clock.FRAME_DT)
        self.assertAlmostEqual(third - second, clock.FRAME_DT)

    def test_does_not_advance_while_paused(self):
        clock = app.InterpolatedClock()
        clock.next_frame(100.0, playing=True)
        paused = [clock.next_frame(100.0, playing=False) for _ in range(300)]
        # Ten wall-clock seconds of paused frames: the song clock must not
        # creep forward by even one tick.
        self.assertEqual(set(paused), {100.0})

    def test_pause_freezes_at_the_displayed_position(self):
        clock = app.InterpolatedClock()
        clock.next_frame(100.0, playing=True)
        displayed = clock.next_frame(100.03, playing=True)
        frozen = clock.next_frame(100.03, playing=False)
        self.assertEqual(frozen, 100.03)
        self.assertLessEqual(abs(frozen - displayed), clock.FRAME_DT)

    def test_resume_continues_from_the_frozen_position(self):
        clock = app.InterpolatedClock()
        clock.next_frame(100.0, playing=True)
        clock.next_frame(100.0, playing=False)
        resumed = clock.next_frame(100.0, playing=True)
        self.assertEqual(resumed, 100.0)
        nxt = clock.next_frame(100.0, playing=True)
        self.assertAlmostEqual(nxt - resumed, clock.FRAME_DT)

    def test_seek_while_paused_shows_the_new_position(self):
        clock = app.InterpolatedClock()
        clock.next_frame(100.0, playing=False)
        self.assertEqual(clock.next_frame(150.0, playing=False), 150.0)

    def test_large_forward_seek_while_playing_snaps(self):
        clock = app.InterpolatedClock()
        clock.next_frame(100.0, playing=True)
        self.assertEqual(clock.next_frame(120.0, playing=True), 120.0)


def _state(sync_pos, onset=10.0, word_idx=0):
    # "hello" is held from onset..onset+0.3; "world" starts onset+0.5, so the
    # jump window is onset+0.3 .. onset+0.5-ARC_LEAD.
    words = (
        WordSpan(onset, onset + 0.3, "hello", 0, 5),
        WordSpan(onset + 0.5, onset + 0.8, "world", 6, 11),
    )
    return AnimState(
        pos=sync_pos, sync_pos=sync_pos, line_idx=0, line_text="hello world",
        line_start=onset, line_end=onset + 2.0, wipe_span=2.0, frac=0.5,
        accent_hex="#00e5ff", words=words, word_idx=word_idx,
        word_progress=0.5, next_onset=None, time_to_next=None, width=80,
        height=24, line_cols=11, word_cols=((0, 5), (6, 11)),
    )


def _dot_row(frame):
    return next(i for i, row in enumerate(frame.above) if DOT_GLYPH in row.plain)


class TestDotFreezesWithTheMusic(unittest.TestCase):
    def test_same_song_position_renders_the_same_frame(self):
        dot = BouncingDot()  # position is a pure function of state.sync_pos
        state = _state(10.1)
        first = dot.render(state)
        second = dot.render(state)
        self.assertEqual([r.plain for r in first.above],
                         [r.plain for r in second.above])
        self.assertEqual([str(r.style) for r in first.above],
                         [str(r.style) for r in second.above])

    def test_dot_follows_the_song_clock_not_the_wall_clock(self):
        dot = BouncingDot()
        rest = dot.render(_state(10.1))                     # word is held
        end = 10.5 - ARC_LEAD
        arc = dot.render(_state(end - ARC_SEC / 2))         # mid-jump

        self.assertEqual(_dot_row(rest), DOT_LANE_ROWS - 1)
        self.assertEqual(_dot_row(arc), 0)
        self.assertNotEqual(_dot_row(rest), _dot_row(arc))
        self.assertEqual(len(rest.above), DOT_LANE_ROWS)

    def test_thirty_identical_frames_stay_identical(self):
        # What a paused player does: the same sync_pos is rendered again and
        # again. Nothing may move on its own.
        dot = BouncingDot()
        shots = {tuple(r.plain for r in dot.render(_state(10.1)).above)
                 for _ in range(30)}
        self.assertEqual(len(shots), 1)


class TestInterludeDotFreezes(unittest.TestCase):
    def setUp(self):
        # Interlude hop tests need the single-dot style.
        app.set_animation("dot")

    def test_same_song_clock_keeps_the_dot_still(self):
        gap = {"rem": 8.0, "is_outro": False}
        first = app._instrumental_rows("#00e5ff", 100, 20, gap, t=10.123)
        second = app._instrumental_rows("#00e5ff", 100, 20, gap, t=10.123)
        self.assertEqual([r.plain for r in first], [r.plain for r in second])
        self.assertEqual([str(r.style) for r in first],
                         [str(r.style) for r in second])

    def test_a_later_song_clock_moves_it(self):
        gap = {"rem": 8.0, "is_outro": False}

        def dot_row(rows):
            return [i for i, r in enumerate(rows)
                    if r.plain == app.INTERLUDE_DOT_GLYPH]

        rows_a = app._instrumental_rows("#00e5ff", 100, 20, gap, t=10.0)
        rows_b = app._instrumental_rows("#00e5ff", 100, 20, gap,
                                        t=10.0 + 0.5 / app.INTERLUDE_HOP_HZ)
        self.assertNotEqual(dot_row(rows_a), dot_row(rows_b))


if __name__ == "__main__":
    unittest.main()
