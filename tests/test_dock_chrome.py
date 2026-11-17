#!/usr/bin/env python3
"""The dock strip must not repeat the cover caption.

Title/artist belong either to the cover caption (art visible) or to the
chrome that replaces it (cover disabled). Rendering the same metadata in
both places wastes a row and reads as a duplicate. SongRec state stays in
the strip because the caption never shows it.
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "src")

from rich.console import Console

import sweep_lyrics.app as app

ENTRIES = [
    (0.0, "first lyric line"),
    (4.0, "second lyric line"),
    (8.0, "third lyric line"),
]


def render_text(**kwargs):
    frame = app.render_frame(
        "Dock Song", "Dock Artist", ENTRIES, 1.0, 12.0, "Playing",
        100, 40, genre="Pop", karaoke_enabled=True, **kwargs)
    console = Console(width=100, color_system=None,
                      force_terminal=False, legacy_windows=False)
    return "".join(seg.text for seg in console.render(frame))


class DockChromeTest(unittest.TestCase):
    def setUp(self):
        self.saved = (app.LAYOUT_MODE, app.UI_MODE, app.ZEN_MODE,
                      app.WORD_MODE, app.SETTINGS)
        app.LAYOUT_MODE = app.LAYOUT_CLASSIC
        app.UI_MODE = app._UI_DOCK
        app.ZEN_MODE = False
        app.WORD_MODE = False
        app.SETTINGS = {}

    def tearDown(self):
        (app.LAYOUT_MODE, app.UI_MODE, app.ZEN_MODE,
         app.WORD_MODE, app.SETTINGS) = self.saved

    def test_dock_with_cover_shows_meta_once(self):
        text = render_text(cover_offset=app.COVER_GUTTER)
        self.assertEqual(text.count("Dock Song"), 1)
        self.assertEqual(text.count("Dock Artist"), 1)
        self.assertEqual(text.count("Pop"), 1)

    def test_dock_without_cover_shows_meta_once(self):
        text = render_text(cover_offset=0)
        self.assertEqual(text.count("Dock Song"), 1)
        self.assertEqual(text.count("Dock Artist"), 1)

    def test_dock_songrec_not_duplicated(self):
        text = render_text(cover_offset=app.COVER_GUTTER, shazam_active=True)
        self.assertEqual(text.count("songrec"), 1)

    def test_standard_cover_caption_shows_meta_once(self):
        app.UI_MODE = 0
        text = render_text(cover_offset=app.COVER_GUTTER)
        self.assertEqual(text.count("Dock Song"), 1)
        self.assertEqual(text.count("Dock Artist"), 1)


if __name__ == "__main__":
    unittest.main()
