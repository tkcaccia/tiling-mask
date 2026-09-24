import numpy as np

from tiling_mask.majority import majority_tiles


def test_majority_and_partial_edges():
    mask = np.array(
        [
            [1, 1, 2, 2, 2],
            [1, 0, 2, 1, 2],
            [3, 3, 0, 0, 2],
        ],
        dtype=np.uint16,
    )
    result = majority_tiles(mask, 2, 2, class_count=4)
    np.testing.assert_array_equal(result, [[1, 2, 2], [3, 0, 2]])


def test_ties_choose_lowest_class():
    mask = np.array([[2, 1], [2, 1]], dtype=np.uint16)
    np.testing.assert_array_equal(majority_tiles(mask, 2, 2, class_count=3), [[1]])


def test_ignore_background_when_foreground_exists():
    mask = np.array([[0, 0], [0, 2]], dtype=np.uint16)
    result = majority_tiles(mask, 2, 2, class_count=3, ignore_background=True)
    np.testing.assert_array_equal(result, [[2]])


def test_empty_background_tile_stays_background():
    mask = np.zeros((2, 2), dtype=np.uint16)
    result = majority_tiles(mask, 2, 2, class_count=2, ignore_background=True)
    np.testing.assert_array_equal(result, [[0]])
