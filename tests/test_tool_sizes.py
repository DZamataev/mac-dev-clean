from __future__ import annotations

import unittest

from mac_dev_clean.tools.sizes import parse_tool_size


class ParseToolSizeTests(unittest.TestCase):
    def test_parses_docker_gigabytes(self):
        self.assertEqual(parse_tool_size("15.03GB"), 15030000000)

    def test_parses_docker_kilobytes_with_lowercase_prefix(self):
        self.assertEqual(parse_tool_size("106.5kB"), 106500)

    def test_strips_a_percentage_suffix(self):
        self.assertEqual(parse_tool_size("15.03GB (68%)"), 15030000000)

    def test_parses_a_value_without_a_percentage_suffix(self):
        self.assertEqual(parse_tool_size("7.499GB"), 7499000000)

    def test_zero_bytes(self):
        self.assertEqual(parse_tool_size("0B"), 0)

    def test_bare_bytes(self):
        self.assertEqual(parse_tool_size("512B"), 512)

    def test_parses_homebrew_megabytes(self):
        self.assertEqual(parse_tool_size("799.8MB"), 799800000)

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(parse_tool_size("  22.03GB  "), 22030000000)

    def test_terabytes(self):
        self.assertEqual(parse_tool_size("1.5TB"), 1500000000000)

    def test_empty_string_is_zero(self):
        self.assertEqual(parse_tool_size(""), 0)

    def test_unparseable_text_is_zero(self):
        self.assertEqual(parse_tool_size("N/A"), 0)

    def test_missing_unit_is_treated_as_bytes(self):
        self.assertEqual(parse_tool_size("1024"), 1024)

    def test_negative_values_are_clamped_to_zero(self):
        self.assertEqual(parse_tool_size("-5GB"), 0)

    def test_overflowing_numeric_string_degrades_to_zero(self):
        self.assertEqual(parse_tool_size("9" * 400), 0)

    def test_overflowing_value_with_unit_degrades_to_zero(self):
        self.assertEqual(parse_tool_size(("9" * 400) + "GB"), 0)


if __name__ == "__main__":
    unittest.main()
