#!/usr/bin/env python3
"""Unit tests for the auto-mode MPRIS noise filter in find_active_player.

These run without a real playerctl: they monkey-patch app.playerctl and
app._player_artists so the picker sees a fixed set of MPRIS players.
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "src")

from sweep_lyrics import app


class TestAutoModeNoiseFilter(unittest.TestCase):
    def setUp(self):
        self._playerctl = app.playerctl
        self._artists = app._player_artists
        self._pinned = app.find_active_player._pinned
        app.find_active_player._pinned = None
        app.find_active_player._stale.clear()
        app.find_active_player._stopped_since.clear()

    def tearDown(self):
        app.playerctl = self._playerctl
        app._player_artists = self._artists
        app.find_active_player._pinned = self._pinned
        app.find_active_player._stale.clear()
        app.find_active_player._stopped_since.clear()

    def _install(self, listed, status, artists):
        def fake_playerctl(*args, **kwargs):
            if args and args[0] == "-l":
                return listed
            if args and args[0] == "status":
                return status.get(kwargs.get("player"), "Stopped")
            if args and args[0] == "metadata":
                return ""
            return ""
        app.playerctl = fake_playerctl
        app._player_artists = lambda: artists

    def test_artistless_browser_is_ignored_when_a_real_player_exists(self):
        # The claude.ai login page bridges as plasma-browser-integration with
        # no artist; the YT Music tab carries a real artist.
        self._install(
            "plasma-browser-integration\nplasma-browser-integration-720690",
            {"plasma-browser-integration": "Playing",
             "plasma-browser-integration-720690": "Paused"},
            {"plasma-browser-integration": "",
             "plasma-browser-integration-720690": "Leend"},
        )
        picked, st = app.find_active_player(None)
        self.assertEqual(picked, "plasma-browser-integration-720690")

    def test_bare_brave_instance_is_ignored(self):
        # Brave's own MPRIS instance with no artist is noise too.
        self._install(
            "brave.instance720171\nplasma-browser-integration-720690",
            {"brave.instance720171": "Playing",
             "plasma-browser-integration-720690": "Playing"},
            {"brave.instance720171": "",
             "plasma-browser-integration-720690": "Leend"},
        )
        picked, st = app.find_active_player(None)
        self.assertEqual(picked, "plasma-browser-integration-720690")

    def test_kdeconnect_never_wins(self):
        # kdeconnect is a phone remote target, not a player, even though it
        # reports an artist.
        self._install(
            "kdeconnect.mpris_abc\nplasma-browser-integration-720690",
            {"kdeconnect.mpris_abc": "Paused",
             "plasma-browser-integration-720690": "Paused"},
            {"kdeconnect.mpris_abc": "Ido Shmuel (PC1948)",
             "plasma-browser-integration-720690": "Leend"},
        )
        picked, st = app.find_active_player(None)
        self.assertEqual(picked, "plasma-browser-integration-720690")

    def test_lone_noise_still_returned_when_nothing_else(self):
        # If the only MPRIS player is the noise session we must still show
        # something rather than nothing.
        self._install(
            "plasma-browser-integration",
            {"plasma-browser-integration": "Playing"},
            {"plasma-browser-integration": ""},
        )
        picked, st = app.find_active_player(None)
        self.assertEqual(picked, "plasma-browser-integration")


if __name__ == "__main__":
    unittest.main()
