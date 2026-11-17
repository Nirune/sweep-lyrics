"""
Tests for auto-drift compensation in sync offset cache.

Scenario: User manually corrects sync multiple times during playback.
The system learns a drift pattern (e.g., +0.05s per minute) and applies
it automatically on future plays of the same track.
"""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sweep_lyrics.app import (
    _track_hash,
    load_offset_cache,
    save_offset_cache,
    offset_for_track,
    drift_for_track,
    save_offset_for_track,
    _estimate_drift,
)


class TestDriftCompensation(unittest.TestCase):
    """Test drift detection and application."""

    def test_track_hash(self):
        """Verify track hashing is consistent."""
        h1 = _track_hash("Artist", "Title")
        h2 = _track_hash("artist", "title")
        self.assertEqual(h1, h2, "hashing should be case-insensitive")

    def test_load_empty_cache(self):
        """Load from nonexistent file returns empty dict."""
        with TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "cache.json"
            # mock the global OFFSET_CACHE_FILE by manipulating the module
            import sweep_lyrics.app as app_module
            old_cache_file = app_module.OFFSET_CACHE_FILE
            try:
                app_module.OFFSET_CACHE_FILE = cache_file
                cache = load_offset_cache()
                self.assertEqual(cache, {})
            finally:
                app_module.OFFSET_CACHE_FILE = old_cache_file

    def test_legacy_format_migration(self):
        """Old format (bare float) migrates to new format (dict)."""
        with TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "cache.json"
            import sweep_lyrics.app as app_module
            old_cache_file = app_module.OFFSET_CACHE_FILE
            try:
                app_module.OFFSET_CACHE_FILE = cache_file
                
                # write old format: {hash: 0.15}
                legacy = {
                    "abc123": 0.15,
                    "def456": -0.05,
                }
                cache_file.write_text(json.dumps(legacy))
                
                # load should migrate it
                cache = load_offset_cache()
                
                self.assertIsInstance(cache["abc123"], dict)
                self.assertEqual(cache["abc123"]["offset"], 0.15)
                self.assertEqual(cache["abc123"]["drift"], 0.0)
                self.assertEqual(cache["abc123"]["adjustments"], [])
                
                self.assertEqual(cache["def456"]["offset"], -0.05)
            finally:
                app_module.OFFSET_CACHE_FILE = old_cache_file

    def test_offset_for_track_default(self):
        """Untracked track returns 0.0 offset."""
        cache = {}
        offset = offset_for_track(cache, "Unknown", "Song")
        self.assertEqual(offset, 0.0)

    def test_offset_for_track_existing(self):
        """Tracked track returns stored offset."""
        h = _track_hash("Artist", "Title")
        cache = {h: {"offset": 0.25, "drift": 0.001, "adjustments": []}}
        offset = offset_for_track(cache, "Artist", "Title")
        self.assertEqual(offset, 0.25)

    def test_drift_for_track_no_drift(self):
        """Track with zero drift returns zero dynamic offset."""
        h = _track_hash("Artist", "Title")
        cache = {h: {"offset": 0.1, "drift": 0.0, "adjustments": []}}
        drift = drift_for_track(cache, "Artist", "Title", pos=120.0)
        self.assertEqual(drift, 0.0)

    def test_drift_for_track_positive_drift(self):
        """Positive drift increases offset over time."""
        h = _track_hash("Artist", "Title")
        # drift of +0.001 s/s = +1ms per second of playback
        cache = {h: {"offset": 0.1, "drift": 0.001, "adjustments": []}}
        
        # at pos=60s, drift component = 0.001 * 60 = 0.06s
        drift_60 = drift_for_track(cache, "Artist", "Title", pos=60.0)
        self.assertAlmostEqual(drift_60, 0.06, places=6)
        
        # at pos=120s, drift component = 0.001 * 120 = 0.12s
        drift_120 = drift_for_track(cache, "Artist", "Title", pos=120.0)
        self.assertAlmostEqual(drift_120, 0.12, places=6)

    def test_drift_for_track_negative_drift(self):
        """Negative drift decreases offset over time."""
        h = _track_hash("Artist", "Title")
        cache = {h: {"offset": 0.1, "drift": -0.0005, "adjustments": []}}
        
        drift_100 = drift_for_track(cache, "Artist", "Title", pos=100.0)
        self.assertAlmostEqual(drift_100, -0.05, places=6)

    def test_estimate_drift_insufficient_points(self):
        """Less than 3 points returns 0 drift."""
        adjustments = [
            {"pos": 10.0, "offset": 0.1, "t": 1000.0},
        ]
        drift = _estimate_drift(adjustments)
        self.assertEqual(drift, 0.0)

    def test_estimate_drift_positive_trend(self):
        """Adjustments trending positive give positive drift."""
        # simulate user pushing offset higher as song progresses
        adjustments = [
            {"pos": 30.0, "offset": 0.1, "t": 1000.0},
            {"pos": 60.0, "offset": 0.15, "t": 1001.0},
            {"pos": 90.0, "offset": 0.2, "t": 1002.0},
        ]
        drift = _estimate_drift(adjustments)
        # slope ≈ (0.2 - 0.1) / (90 - 30) = 0.1 / 60 ≈ 0.00167 s/s
        self.assertGreater(drift, 0.0)

    def test_estimate_drift_clamped_at_sanity_limit(self):
        """Very large slopes are clamped as noise."""
        # ridiculous adjustments: +1s offset per second of playback
        adjustments = [
            {"pos": 10.0, "offset": 1.0, "t": 1000.0},
            {"pos": 20.0, "offset": 2.0, "t": 1001.0},
            {"pos": 30.0, "offset": 3.0, "t": 1002.0},
        ]
        drift = _estimate_drift(adjustments)
        # slope = 0.1 s/s, should be clamped to 0
        self.assertEqual(drift, 0.0)

    def test_save_and_recall_offset(self):
        """Save offset, load, verify it persists."""
        with TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "cache.json"
            import sweep_lyrics.app as app_module
            old_cache_file = app_module.OFFSET_CACHE_FILE
            try:
                app_module.OFFSET_CACHE_FILE = cache_file
                
                cache = {}
                save_offset_for_track(cache, "Artist", "Title", 0.25)
                
                # verify in-memory cache
                h = _track_hash("Artist", "Title")
                self.assertEqual(cache[h]["offset"], 0.25)
                
                # verify persisted
                saved = json.loads(cache_file.read_text())
                self.assertEqual(saved[h]["offset"], 0.25)
                
                # load and verify again
                loaded = load_offset_cache()
                self.assertEqual(offset_for_track(loaded, "Artist", "Title"), 0.25)
            finally:
                app_module.OFFSET_CACHE_FILE = old_cache_file

    def test_save_with_pos_records_adjustment(self):
        """Saving with pos parameter records adjustment for drift estimation."""
        cache = {}
        save_offset_for_track(cache, "Artist", "Title", 0.1, pos=30.0)
        
        h = _track_hash("Artist", "Title")
        entry = cache[h]
        self.assertEqual(entry["offset"], 0.1)
        self.assertEqual(len(entry["adjustments"]), 1)
        self.assertAlmostEqual(entry["adjustments"][0]["pos"], 30.0)
        self.assertEqual(entry["adjustments"][0]["offset"], 0.1)

    def test_repeated_saves_estimate_drift(self):
        """Multiple saves at different positions estimate drift."""
        cache = {}
        
        # User corrects sync at three points in the track
        save_offset_for_track(cache, "Artist", "Title", 0.05, pos=30.0)
        time.sleep(0.01)  # tiny gap so timestamps differ
        save_offset_for_track(cache, "Artist", "Title", 0.10, pos=60.0)
        time.sleep(0.01)
        save_offset_for_track(cache, "Artist", "Title", 0.15, pos=90.0)
        
        h = _track_hash("Artist", "Title")
        entry = cache[h]
        
        # Should have 3 adjustments
        self.assertEqual(len(entry["adjustments"]), 3)
        
        # Drift should be estimated from the trend
        # (0.15 - 0.05) / (90 - 30) = 0.1 / 60 ≈ 0.00167 s/s
        self.assertGreater(entry["drift"], 0.0)
        self.assertLess(entry["drift"], 0.01)  # sanity limit

    def test_adjustment_history_bounded(self):
        """Keep only the last 20 adjustments to bound file size."""
        cache = {}
        
        # Make 30 adjustments
        for i in range(30):
            pos = 10.0 + i * 5.0
            offset = 0.05 + i * 0.001
            save_offset_for_track(cache, "Artist", "Title", offset, pos=pos)
        
        h = _track_hash("Artist", "Title")
        entry = cache[h]
        
        # Should keep only last 20
        self.assertEqual(len(entry["adjustments"]), 20)


if __name__ == "__main__":
    unittest.main()
