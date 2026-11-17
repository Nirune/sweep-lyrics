#!/usr/bin/env python3
"""Settings tests for the Layout / Interface split.

``layout_mode`` used to hold the lyric arrangement *and* the dock chrome
(4 = dock, 5 = split). It is now 0..4 (lyric) plus ``ui_mode`` 0..1
(standard / dock). Files written before the split must keep the look the
user saved.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "src")

import sweep_lyrics.app as app


class LayoutInterfaceMigrationTest(unittest.TestCase):
    def _load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(data))
            original = app.SETTINGS_FILE
            app.SETTINGS_FILE = path
            try:
                return app.load_settings()
            finally:
                app.SETTINGS_FILE = original

    def test_legacy_lyric_layouts_keep_standard_chrome(self):
        for mode in range(4):
            s = self._load({"layout_mode": mode})
            self.assertEqual(s["layout_mode"], mode)
            self.assertEqual(s["ui_mode"], 0)

    def test_legacy_dock_splits_into_classic_plus_dock(self):
        s = self._load({"layout_mode": 4})
        self.assertEqual(s["layout_mode"], app.LAYOUT_CLASSIC)
        self.assertEqual(s["ui_mode"], app._UI_DOCK)

    def test_legacy_split_becomes_lyric_split(self):
        s = self._load({"layout_mode": 5})
        self.assertEqual(s["layout_mode"], app.LAYOUT_SPLIT)
        self.assertEqual(s["ui_mode"], 0)

    def test_new_settings_are_not_remigrated(self):
        # layout_mode 4 now means split; a file that already has ui_mode
        # must be read as-is.
        s = self._load({"layout_mode": 4, "ui_mode": 1})
        self.assertEqual(s["layout_mode"], app.LAYOUT_SPLIT)
        self.assertEqual(s["ui_mode"], 1)

    def test_ui_mode_is_clamped(self):
        self.assertEqual(self._load({"ui_mode": 9})["ui_mode"], 1)
        self.assertEqual(self._load({"ui_mode": -3})["ui_mode"], 0)
        self.assertEqual(self._load({"ui_mode": "nope"})["ui_mode"], 0)

    def test_fresh_install_defaults(self):
        s = self._load({})
        self.assertEqual(s["layout_mode"], app.LAYOUT_CLASSIC)
        self.assertEqual(s["ui_mode"], 0)


class LayoutInterfaceHelpersTest(unittest.TestCase):
    def test_ui_docked_follows_ui_mode(self):
        original = app.UI_MODE
        try:
            app.UI_MODE = 0
            self.assertFalse(app.ui_docked())
            app.UI_MODE = app._UI_DOCK
            self.assertTrue(app.ui_docked())
        finally:
            app.UI_MODE = original

    def test_lyric_layout_clamps(self):
        self.assertEqual(app.lyric_layout(-1), app.LAYOUT_CLASSIC)
        self.assertEqual(app.lyric_layout(9), app.LAYOUT_SPLIT)
        self.assertEqual(app.lyric_layout("nope"), app.LAYOUT_CLASSIC)


if __name__ == "__main__":
    unittest.main()
