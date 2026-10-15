import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import rasterio

from tiling_mask.border_error import swap_border_tiles


CPP_BINARY = Path(__file__).resolve().parents[1] / "cpp/build/tiling-mask-cpp"


@pytest.mark.skipif(not CPP_BINARY.is_file(), reason="C++ binary is not built")
def test_cpp_border_error_matches_python(tmp_path):
    image = tmp_path / "image.ome.tif"
    geojson = tmp_path / "annotations.geojson"
    output = tmp_path / "output"
    with rasterio.open(image, "w", driver="GTiff", width=8, height=2,
                       count=1, dtype="uint8") as destination:
        destination.write(np.zeros((2, 8), dtype=np.uint8), 1)
    geojson.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature", "properties": {"class": name},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [left, 0], [right, 0], [right, 2], [left, 2], [left, 0]
                ]]},
            }
            for name, left, right in (("left", 0, 4), ("right", 4, 8))
        ],
    }), encoding="utf-8")

    subprocess.run([
        str(CPP_BINARY), str(image), str(geojson), "-t", "2", "-o", str(output),
        "--workers", "1", "--raster-workers", "1",
        "--border-error-percent", "50", "--border-error-seed", "17",
        "--render-rgb",
    ], check=True, capture_output=True, text=True)

    with rasterio.open(output / "mask_tile_grid_2x2_cpp.tif") as source:
        clean = source.read(1)
    with rasterio.open(output / "mask_tile_grid_2x2_border_error_cpp.tif") as source:
        noisy = source.read(1)
    np.testing.assert_array_equal(clean, [[1, 1, 2, 2]])
    expected, eligible, swapped = swap_border_tiles(clean, 50, seed=17)
    np.testing.assert_array_equal(noisy, expected)
    assert (eligible, swapped) == (2, 1)
    metrics = json.loads((output / "metrics_cpp.json").read_text())
    assert metrics["rasterizer"] == "gdal"
    assert metrics["border_error_tiles"]["2x2"] == {"eligible": 2, "swapped": 1}
    assert (output / "mask_tile_2x2_preview_multicolor_border_error_cpp.png").is_file()


@pytest.mark.skipif(not CPP_BINARY.is_file(), reason="C++ binary is not built")
@pytest.mark.parametrize("tiled", [False, True])
def test_cpp_uses_categorical_mask_as_only_input(tmp_path, tiled):
    source_mask = tmp_path / "labels.tif"
    width, height, tile_size = (32, 16, 16) if tiled else (8, 2, 2)
    labels = np.full((height, width), 5, dtype=np.uint8)
    labels[:, width // 2:] = 9
    profile = dict(driver="GTiff", width=width, height=height, count=1,
                   dtype="uint8", tiled=tiled)
    if tiled:
        profile.update(blockxsize=16, blockysize=16)
    with rasterio.open(source_mask, "w", **profile) as destination:
        destination.write(labels, 1)
        destination.write_colormap(1, {0: (0, 0, 0), 5: (255, 0, 0),
                                       9: (0, 0, 255)})

    output = tmp_path / "output"
    subprocess.run([
        str(CPP_BINARY), "--mask", str(source_mask), "-t", str(tile_size),
        "-o", str(output), "--workers", "1", "--border-error-percent", "100",
        "--render-rgb",
    ], check=True, capture_output=True, text=True)
    with rasterio.open(output / "mask_pixel_cpp.tif") as source:
        np.testing.assert_array_equal(source.read(1), labels)
        assert source.colormap(1)[5][:3] == (255, 0, 0)
        assert source.colormap(1)[9][:3] == (0, 0, 255)
    slug = f"{tile_size}x{tile_size}"
    with rasterio.open(output / f"mask_tile_grid_{slug}_cpp.tif") as source:
        expected = [[5, 9]] if tiled else [[5, 5, 9, 9]]
        np.testing.assert_array_equal(source.read(1), expected)
    metrics = json.loads((output / "metrics_cpp.json").read_text())
    assert metrics["input_mode"] == "mask"
    assert metrics["rasterizer"] == "mask-input"
    assert metrics["border_error_tiles"][slug]["swapped"] == 2
    assert (output / f"mask_tile_{slug}_preview_multicolor_border_error_cpp.png").is_file()


@pytest.mark.skipif(not CPP_BINARY.is_file(), reason="C++ binary is not built")
def test_cpp_mask_input_rejects_rgb_tiff(tmp_path):
    source_mask = tmp_path / "rgb.tif"
    with rasterio.open(source_mask, "w", driver="GTiff", width=2, height=2,
                       count=3, dtype="uint8") as destination:
        destination.write(np.zeros((3, 2, 2), dtype=np.uint8))
    run = subprocess.run([
        str(CPP_BINARY), "--mask", str(source_mask), "-t", "2",
        "-o", str(tmp_path / "output"),
    ], capture_output=True, text=True)
    assert run.returncode != 0
    assert "single-band 8-bit" in run.stderr
