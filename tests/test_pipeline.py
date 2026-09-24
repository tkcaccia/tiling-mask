import json

import numpy as np
import rasterio
from affine import Affine

from tiling_mask.pipeline import TileSize, build_masks


def test_end_to_end_pixel_and_tile_masks(tmp_path):
    image = tmp_path / "image.ome.tif"
    annotations = tmp_path / "annotations.geojson"
    output = tmp_path / "output"
    with rasterio.open(
        image,
        "w",
        driver="GTiff",
        width=5,
        height=3,
        count=1,
        dtype="uint8",
        transform=Affine.translation(10, 20),
    ) as destination:
        destination.write(np.zeros((3, 5), dtype=np.uint8), 1)

    annotations.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"class": "left", "color": "#ff0000"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [3, 0], [3, 2], [0, 2], [0, 0]]],
                },
            },
            {
                "type": "Feature",
                "properties": {"class": "right", "color": "#0000ff"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[3, 0], [5, 0], [5, 3], [3, 3], [3, 0]]],
                },
            },
        ],
    }), encoding="utf-8")

    metrics = build_masks(
        image, annotations, [TileSize(2, 2)], output, workers=2, render_rgb=True
    )
    assert metrics.features == 2
    assert metrics.workers == 2
    assert metrics.render_rgb is True
    assert metrics.render_full_rgb is False
    with rasterio.open(output / "mask_pixel.tif") as source:
        np.testing.assert_array_equal(source.read(1), [
            [1, 1, 1, 2, 2],
            [1, 1, 1, 2, 2],
            [0, 0, 0, 2, 2],
        ])
    with rasterio.open(output / "mask_tile_grid_2x2.tif") as source:
        np.testing.assert_array_equal(source.read(1), [[1, 1, 2], [0, 0, 2]])
        assert source.transform == Affine(2, 0, 10, 0, 2, 20)
    with rasterio.open(output / "mask_tile_2x2_preview.png") as source:
        assert source.shape == (2, 3)
        assert source.count == 1
        assert source.colormap(1)[1][:3] == (255, 0, 0)
        assert source.colormap(1)[2][:3] == (0, 0, 255)
    with rasterio.open(output / "mask_tile_2x2_preview_multicolor.png") as source:
        assert source.shape == (2, 3)
        assert source.count == 1
        assert source.colormap(1)[1] != source.colormap(1)[2]
