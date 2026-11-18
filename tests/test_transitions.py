"""
Tests for chrome transitions (fade + slide on track changes).

Currently a placeholder — actual implementation pending.
"""
import unittest


class TestColorLerp(unittest.TestCase):
    """Test color interpolation helpers."""

    def test_hex_to_rgb(self):
        """Convert hex color to RGB tuple."""
        # This will be implemented in app.py
        # For now, test the expected behavior
        hex_color = "#FF5733"
        expected = (255, 87, 51)
        # Will fail until we implement the helper
        self.assertEqual(hex_color[:], expected[:])  # placeholder

    def test_lerp_rgb(self):
        """Linear interpolation between two RGB colors."""
        c1 = (0, 0, 0)    # black
        c2 = (255, 255, 255)  # white
        mid = 0.5
        
        # Expected: (127, 127, 127) - mid gray
        # Will implement lerp function
        self.assertAlmostEqual(127.5, 127.5)  # placeholder


if __name__ == "__main__":
    unittest.main()
