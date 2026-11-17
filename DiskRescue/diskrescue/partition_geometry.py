"""Pixel geometry helpers for the disk partition map."""

from __future__ import annotations

from typing import Any


def _size_bytes(partition: dict[str, Any]) -> int:
    try:
        return max(0, int(partition.get("Size") or 0))
    except (TypeError, ValueError):
        return 0


def _apportion_pixels(total_pixels: int, weights: list[int]) -> list[int]:
    """Allocate integer pixels proportionally using the largest-remainder method."""
    if not weights:
        return []
    weight_sum = sum(max(0, weight) for weight in weights)
    if total_pixels <= 0 or weight_sum <= 0:
        return [0] * len(weights)

    exact = [total_pixels * max(0, weight) / weight_sum for weight in weights]
    result = [int(value) for value in exact]
    remainder = total_pixels - sum(result)
    order = sorted(
        range(len(weights)),
        key=lambda index: (exact[index] - result[index], weights[index], -index),
        reverse=True,
    )
    for index in order[:remainder]:
        result[index] += 1
    return result


def calculate_partition_segment_widths(
    partitions: list[dict[str, Any]],
    disk_total_bytes: int,
    bar_width_pixels: int,
    min_visible_pixels: int = 6,
) -> list[int]:
    """Return segment widths that preserve total allocation and reveal tiny partitions.

    The sum of segment widths equals the pixel width representing all allocated
    partition bytes. The remaining bar width is left untouched for unallocated
    space. Partitions smaller than the minimum visible width borrow pixels from
    larger partitions; this is intentionally a visual aid, not a capacity scale.
    Exact byte sizes remain shown in the partition table.
    """
    count = len(partitions)
    widths = [0] * count
    total = max(0, int(disk_total_bytes or 0))
    bar_width = max(0, int(bar_width_pixels or 0))
    minimum = max(1, int(min_visible_pixels or 1))

    if count == 0 or total == 0 or bar_width == 0:
        return widths

    sizes = [_size_bytes(partition) for partition in partitions]
    positive_indices = [index for index, size in enumerate(sizes) if size > 0]
    if not positive_indices:
        return widths

    allocated_bytes = min(sum(sizes), total)
    allocated_pixels = min(
        bar_width,
        int(bar_width * allocated_bytes / total + 0.5),
    )
    if allocated_pixels <= 0:
        return widths

    positive_sizes = [sizes[index] for index in positive_indices]
    base_widths = _apportion_pixels(allocated_pixels, positive_sizes)
    for index, width in zip(positive_indices, base_widths):
        widths[index] = width

    # If there are more non-empty partitions than available pixels, the screen
    # cannot show every one separately. Give a pixel to the largest partitions.
    if allocated_pixels < len(positive_indices):
        widths = [0] * count
        largest = sorted(positive_indices, key=lambda index: (sizes[index], -index), reverse=True)
        for index in largest[:allocated_pixels]:
            widths[index] = 1
        return widths

    # Keep every non-empty partition discernible, without changing the total
    # allocated width or the dark-gray unallocated remainder.
    effective_minimum = min(minimum, max(1, allocated_pixels // len(positive_indices)))
    small = [index for index in positive_indices if widths[index] < effective_minimum]
    deficit = sum(effective_minimum - widths[index] for index in small)
    if deficit <= 0:
        return widths

    donors = [index for index in positive_indices if widths[index] > effective_minimum]
    capacities = [widths[index] - effective_minimum for index in donors]
    capacity_sum = sum(capacities)
    if capacity_sum < deficit:
        # This should be prevented by effective_minimum, but remain conservative.
        return widths

    cuts = _apportion_pixels(deficit, capacities)
    for index, cut in zip(donors, cuts):
        widths[index] -= cut
    for index in small:
        widths[index] = effective_minimum

    return widths
