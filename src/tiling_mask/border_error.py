from __future__ import annotations

import math

import numpy as np


def swap_border_tiles(
    labels: np.ndarray, percent: float, seed: int = 0
) -> tuple[np.ndarray, int, int]:
    """Relabel an exact percentage of foreground tiles on class boundaries.

    A border tile has a four-connected neighbor with a different nonzero class.
    Its alternate label is the lowest such neighboring class ID. Selection is
    deterministic for a given seed and does not cascade through changed labels.
    Returns (changed labels, eligible count, swapped count).
    """
    if labels.ndim != 2:
        raise ValueError("labels must be a 2-D array")
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError("border_error_percent must be between 0 and 100")
    if seed < 0 or seed > 2**64 - 1:
        raise ValueError("border_error_seed must be a nonnegative 64-bit integer")

    changed = labels.copy()
    if percent == 0 or labels.size == 0:
        return changed, 0, 0

    alternate = np.zeros_like(labels)

    def consider(current: np.ndarray, neighbor: np.ndarray, target: np.ndarray) -> None:
        valid = (current != 0) & (neighbor != 0) & (current != neighbor)
        np.copyto(target, neighbor, where=valid & ((target == 0) | (neighbor < target)))

    consider(labels[1:, :], labels[:-1, :], alternate[1:, :])
    consider(labels[:-1, :], labels[1:, :], alternate[:-1, :])
    consider(labels[:, 1:], labels[:, :-1], alternate[:, 1:])
    consider(labels[:, :-1], labels[:, 1:], alternate[:, :-1])

    indices = np.flatnonzero(alternate)
    eligible = int(indices.size)
    count = min(eligible, math.floor(eligible * percent / 100 + 0.5))
    if count == 0:
        return changed, eligible, 0

    # SplitMix64 gives a stable ordering without a large random permutation.
    keys = indices.astype(np.uint64) + np.uint64(seed) + np.uint64(0x9E3779B97F4A7C15)
    keys = (keys ^ (keys >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    keys = (keys ^ (keys >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    keys ^= keys >> np.uint64(31)
    selected = indices[np.lexsort((indices, keys))[:count]]
    changed.flat[selected] = alternate.flat[selected]
    return changed, eligible, count
