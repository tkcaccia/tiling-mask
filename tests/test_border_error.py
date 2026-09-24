import numpy as np
import pytest

from tiling_mask.border_error import swap_border_tiles


def test_border_error_swaps_only_foreground_class_boundaries():
    labels = np.array([
        [0, 1, 1, 2],
        [0, 1, 1, 2],
        [0, 1, 1, 2],
    ], dtype=np.uint8)
    changed, eligible, swapped = swap_border_tiles(labels, 100)
    assert eligible == 6
    assert swapped == 6
    np.testing.assert_array_equal(changed, [
        [0, 1, 2, 1],
        [0, 1, 2, 1],
        [0, 1, 2, 1],
    ])
    assert labels[0, 2] == 1  # The clean input is untouched.


def test_border_error_percentage_and_seed_are_reproducible():
    labels = np.tile(np.array([1, 1, 2, 2], dtype=np.uint8), (5, 1))
    first, eligible, swapped = swap_border_tiles(labels, 30, seed=17)
    second, _, _ = swap_border_tiles(labels, 30, seed=17)
    assert eligible == 10
    assert swapped == 3
    assert np.count_nonzero(first != labels) == 3
    np.testing.assert_array_equal(first, second)


@pytest.mark.parametrize("percent", [-1, 101, float("nan")])
def test_border_error_rejects_invalid_percent(percent):
    with pytest.raises(ValueError, match="border_error_percent"):
        swap_border_tiles(np.array([[1, 2]], dtype=np.uint8), percent)
