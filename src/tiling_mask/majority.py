from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def _majority_rows(
    pixels: NDArray[np.integer],
    classes: int,
    ignore_background: bool,
) -> NDArray[np.integer]:
    """Choose one class for each row of an already grouped pixel matrix."""
    counts = np.empty((pixels.shape[0], classes), dtype=np.uint32)
    for class_id in range(classes):
        counts[:, class_id] = np.count_nonzero(pixels == class_id, axis=1)
    if ignore_background and classes > 1:
        counts[:, 0] = 0
    winners = counts.argmax(axis=1)
    winners[counts.sum(axis=1) == 0] = 0
    return winners.astype(pixels.dtype, copy=False)


def majority_tiles(
    mask: NDArray[np.integer],
    tile_height: int,
    tile_width: int,
    *,
    class_count: int | None = None,
    ignore_background: bool = False,
) -> NDArray[np.integer]:
    """Return one majority class per tile using a vectorized histogram.

    Partial edge tiles are included. Ties resolve to the lowest class ID, making
    results deterministic. Input class IDs must be compact non-negative integers.
    """
    if mask.ndim != 2:
        raise ValueError("mask must be a 2-D array")
    if tile_height <= 0 or tile_width <= 0:
        raise ValueError("tile dimensions must be positive")
    height, width = mask.shape
    if height == 0 or width == 0:
        return np.empty((0, 0), dtype=mask.dtype)

    classes = class_count or int(mask.max(initial=0)) + 1
    if classes <= 0 or classes > 65536:
        raise ValueError("class_count must be between 1 and 65,536")
    if int(mask.min(initial=0)) < 0 or int(mask.max(initial=0)) >= classes:
        raise ValueError("mask contains a class ID outside class_count")

    full_rows, bottom_height = divmod(height, tile_height)
    full_cols, right_width = divmod(width, tile_width)
    rows = full_rows + bool(bottom_height)
    cols = full_cols + bool(right_width)
    result = np.zeros((rows, cols), dtype=mask.dtype)

    # The common interior path is a zero-copy reshape. Only the narrow partial
    # edge strips are handled separately, avoiding full-chunk padding and a
    # second full-size validity array.
    if full_rows and full_cols:
        interior = mask[: full_rows * tile_height, : full_cols * tile_width]
        pixels = interior.reshape(full_rows, tile_height, full_cols, tile_width)
        pixels = pixels.transpose(0, 2, 1, 3).reshape(full_rows * full_cols, -1)
        result[:full_rows, :full_cols] = _majority_rows(
            pixels, classes, ignore_background
        ).reshape(full_rows, full_cols)

    if right_width and full_rows:
        right = mask[: full_rows * tile_height, full_cols * tile_width :]
        pixels = right.reshape(full_rows, tile_height * right_width)
        result[:full_rows, full_cols] = _majority_rows(
            pixels, classes, ignore_background
        )

    if bottom_height and full_cols:
        bottom = mask[full_rows * tile_height :, : full_cols * tile_width]
        pixels = bottom.reshape(bottom_height, full_cols, tile_width)
        pixels = pixels.transpose(1, 0, 2).reshape(full_cols, -1)
        result[full_rows, :full_cols] = _majority_rows(
            pixels, classes, ignore_background
        )

    if bottom_height and right_width:
        corner = mask[full_rows * tile_height :, full_cols * tile_width :].reshape(1, -1)
        result[full_rows, full_cols] = _majority_rows(
            corner, classes, ignore_background
        )[0]

    return result
