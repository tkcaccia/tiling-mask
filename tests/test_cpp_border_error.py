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
    assert metrics["border_error_tiles"]["2x2"] == {"eligible": 2, "swapped": 1}
    assert (output / "mask_tile_2x2_preview_multicolor_border_error_cpp.png").is_file()
