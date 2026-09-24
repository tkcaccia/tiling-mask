import json

from tiling_mask.annotations import load_geojson


def test_qupath_properties_and_colors(tmp_path):
    path = tmp_path / "annotations.geojson"
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 0]]]},
                "properties": {"classification": {"name": "Tumor", "color": [255, 0, 8]}},
            }
        ],
    }))
    annotations = load_geojson(path)
    assert annotations.shapes[0][1] == 1
    assert annotations.classes[1].name == "Tumor"
    assert annotations.classes[1].color == (255, 0, 8, 255)
