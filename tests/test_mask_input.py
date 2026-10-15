import json

import numpy as np
import pytest
import rasterio
from affine import Affine
from PIL import Image

from tiling_mask.cli import main
from tiling_mask.pipeline import TileSize, build_masks


def test_python_mask_input_preserves_labels_palette_and_transform(tmp_path):
    source_mask = tmp_path / "labels.tif"
    labels = np.tile(np.array([0, 0, 5, 5, 5, 5, 9, 9, 9, 9], dtype=np.uint8), (2, 1))
    with rasterio.open(
        source_mask, "w", driver="GTiff", width=10, height=2, count=1,
        dtype="uint8", transform=Affine.translation(10, 20),
    ) as destination:
        destination.write(labels, 1)
        destination.write_colormap(1, {0: (0, 0, 0, 0), 5: (255, 0, 0, 255),
                                       9: (0, 0, 255, 255)})
        destination.update_tags(1, CLASS_NAMES=json.dumps({"5": "stroma", "9": "tumor"}))

    output = tmp_path / "output"
    metrics = build_masks(
        tile_sizes=[TileSize(2, 2)], output_dir=output, mask_path=source_mask,
        workers=1, render_rgb=True, border_error_percent=100,
    )
    assert metrics.input_mode == "mask"
    assert metrics.features == 0
    with rasterio.open(output / "mask_pixel.tif") as source:
        np.testing.assert_array_equal(source.read(1), labels)
        assert source.colormap(1)[5][:3] == (255, 0, 0)
        assert source.tags(1)["CLASS_NAMES"]
    with rasterio.open(output / "mask_tile_grid_2x2.tif") as source:
        np.testing.assert_array_equal(source.read(1), [[0, 5, 5, 9, 9]])
        assert source.transform == Affine(2, 0, 10, 0, 2, 20)
    with rasterio.open(output / "mask_tile_grid_2x2_border_error.tif") as source:
        np.testing.assert_array_equal(source.read(1), [[0, 5, 9, 5, 9]])
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["input_mode"] == "mask"
    assert manifest["source_mask"] == str(source_mask.resolve())
    assert manifest["classes"][5]["name"] == "stroma"
    assert (output / "mask_tile_2x2_preview_multicolor_border_error.png").is_file()

    cli_output = tmp_path / "cli_output"
    assert main(["--mask", str(source_mask), "-t", "2", "-o", str(cli_output)]) == 0
    assert (cli_output / "mask_tile_grid_2x2.tif").is_file()


def test_mask_input_rejects_rgb_data(tmp_path):
    source_mask = tmp_path / "rgb.tif"
    with rasterio.open(source_mask, "w", driver="GTiff", width=2, height=2,
                       count=3, dtype="uint8") as destination:
        destination.write(np.zeros((3, 2, 2), dtype=np.uint8))
    with pytest.raises(ValueError, match="single-band uint8"):
        build_masks(None, None, [TileSize(2, 2)], tmp_path / "output",
                    mask_path=source_mask)


def test_python_mask_input_accepts_grayscale_png(tmp_path):
    source_mask = tmp_path / "labels.png"
    Image.fromarray(np.array([[1, 1, 2, 2], [1, 1, 2, 2]], dtype=np.uint8)).save(source_mask)
    output = tmp_path / "output"
    build_masks(None, None, [TileSize(2, 2)], output, mask_path=source_mask,
                workers=1)
    with rasterio.open(output / "mask_tile_grid_2x2.tif") as source:
        np.testing.assert_array_equal(source.read(1), [[1, 2]])
