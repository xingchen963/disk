import unittest

from diskrescue.partition_geometry import calculate_partition_segment_widths


class PartitionSegmentWidthTests(unittest.TestCase):
    def test_tiny_partition_gets_minimum_visible_width(self):
        parts = [
            {"Size": 200},
            {"Size": 999_800},
        ]
        widths = calculate_partition_segment_widths(parts, 1_000_000, 1000)
        self.assertEqual(widths, [6, 994])
        self.assertEqual(sum(widths), 1000)

    def test_multiple_tiny_partitions_remain_separable(self):
        parts = [
            {"Size": 100},
            {"Size": 200},
            {"Size": 300},
            {"Size": 999_400},
        ]
        widths = calculate_partition_segment_widths(parts, 1_000_000, 1200)
        self.assertEqual(widths[:3], [6, 6, 6])
        self.assertEqual(sum(widths), 1200)

    def test_unallocated_space_is_not_consumed_by_minimum_width(self):
        parts = [
            {"Size": 300_000},
            {"Size": 500_000},
        ]
        widths = calculate_partition_segment_widths(parts, 1_000_000, 1000)
        self.assertEqual(widths, [300, 500])
        self.assertEqual(sum(widths), 800)
        self.assertEqual(1000 - sum(widths), 200)

    def test_zero_sized_partitions_receive_no_pixels(self):
        parts = [
            {"Size": 0},
            {"Size": 100},
            {"Size": 0},
            {"Size": 900},
        ]
        widths = calculate_partition_segment_widths(parts, 1000, 100)
        self.assertEqual(widths, [0, 10, 0, 90])

    def test_more_tiny_partitions_than_pixels_favors_largest_without_overflow(self):
        parts = [{"Size": size} for size in range(1, 11)]
        widths = calculate_partition_segment_widths(parts, 55, 3, min_visible_pixels=6)
        self.assertEqual(sum(widths), 3)
        self.assertEqual(sorted(widths, reverse=True), [1, 1, 1, 0, 0, 0, 0, 0, 0, 0])

    def test_empty_or_invalid_input_is_safe(self):
        self.assertEqual(calculate_partition_segment_widths([], 1000, 100), [])
        self.assertEqual(calculate_partition_segment_widths([{"Size": 100}], 0, 100), [0])
        self.assertEqual(calculate_partition_segment_widths([{"Size": 100}], 100, 0), [0])
        self.assertEqual(
            calculate_partition_segment_widths([{"Size": "bad"}], 100, 100),
            [0],
        )


if __name__ == "__main__":
    unittest.main()
