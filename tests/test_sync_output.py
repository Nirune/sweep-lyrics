#!/usr/bin/env python3
"""The DEC 2026 sync markers must not print a newline at the screen bottom.

Regression test for "the album cover keeps drifting upward during the dot
animation". Inside ``Live``, ``sys.stdout`` is a ``FileProxy`` whose ``write``
buffers text until a newline and then re-prints it through the console -- with
a trailing newline. Writing the sync markers there scrolled the alt screen by
a row per marker; Kitty anchors image placements to screen rows, so the cover
art climbed upward while the redrawn text stayed put.

The markers now go straight to ``console.file``. This test renders on an
exact-height console, replays the emitted cursor movement, and asserts the
screen never scrolls.
"""

from __future__ import annotations

import io
import sys
import unittest

sys.path.insert(0, "src")

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from sweep_lyrics import app


def count_scrolls(stream: str, height: int) -> int:
    """Rows scrolled by a byte stream on a ``height``-row terminal.

    Only the movement this regression cares about: ``ESC[H`` homes the cursor
    (what ``Live`` emits in alt-screen mode) and ``\n`` advances a row, with a
    scroll once it would pass the bottom. Text is assumed not to wrap, which
    holds for the fixed-width panel used here.
    """
    row = 0
    scrolls = 0
    i = 0
    while i < len(stream):
        if stream.startswith("\x1b[H", i):
            row = 0
            i += 3
            continue
        if stream[i] == "\n":
            row += 1
            if row > height - 1:
                scrolls += 1
                row = height - 1
        i += 1
    return scrolls


class TestSyncOutputWrap(unittest.TestCase):
    WIDTH = 40
    HEIGHT = 12
    FRAMES = 4

    def _capture(self) -> str:
        buffer = io.StringIO()
        console = Console(
            file=buffer, force_terminal=True, width=self.WIDTH, height=self.HEIGHT
        )
        with Live(console=console, screen=True, auto_refresh=False) as live:
            for i in range(self.FRAMES):
                frame = Panel(Text(f"frame {i}"), height=self.HEIGHT)
                app._sync_output_wrap(live, frame)
        return buffer.getvalue()

    def test_markers_are_written_raw(self):
        out = self._capture()
        # Exactly one start/end pair per frame, emitted as real escape
        # sequences (not escaped/reprinted text).
        self.assertEqual(out.count("\x1b[?2026h"), self.FRAMES)
        self.assertEqual(out.count("\x1b[?2026l"), self.FRAMES)
        # The markers must not be followed by a newline (that is what used to
        # push the bottom row up and scroll the alt screen).
        self.assertNotIn("\x1b[?2026h\n", out)
        self.assertNotIn("\x1b[?2026l\n", out)

    def test_full_height_frames_do_not_scroll(self):
        out = self._capture()
        self.assertEqual(count_scrolls(out, self.HEIGHT), 0)


if __name__ == "__main__":
    unittest.main()
