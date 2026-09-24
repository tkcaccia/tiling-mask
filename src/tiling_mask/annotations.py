from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClassInfo:
    value: int
    name: str
    color: tuple[int, int, int, int]


@dataclass(frozen=True)
class AnnotationSet:
    shapes: list[tuple[dict[str, Any], int]]
    classes: list[ClassInfo]


_AUTO_CLASS_PATHS = (
    "classification.name",
    "class",
    "label",
    "category",
    "name",
    "value",
)
_AUTO_COLOR_PATHS = ("classification.color", "color", "fill")


def _nested(properties: dict[str, Any], path: str) -> Any:
    value: Any = properties
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _class_name(properties: dict[str, Any], class_property: str | None) -> str:
    if class_property:
        value = _nested(properties, class_property)
        if value is None:
            raise ValueError(f"Feature is missing class property '{class_property}'")
        return str(value)
    for path in _AUTO_CLASS_PATHS:
        value = _nested(properties, path)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return "annotation"


def _fallback_color(name: str) -> tuple[int, int, int, int]:
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=3).digest()
    # Keep generated colors visible without making them pastel.
    return tuple(48 + (channel % 176) for channel in digest) + (255,)


def _parse_color(value: Any, name: str) -> tuple[int, int, int, int]:
    if isinstance(value, str):
        text = value.lstrip("#")
        if len(text) in (6, 8):
            try:
                parts = tuple(int(text[i : i + 2], 16) for i in range(0, len(text), 2))
                return parts + ((255,) if len(parts) == 3 else ())
            except ValueError:
                pass
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        try:
            parts = tuple(max(0, min(255, int(x))) for x in value)
            return parts + ((255,) if len(parts) == 3 else ())
        except (TypeError, ValueError):
            pass
    return _fallback_color(name)


def load_geojson(path: str | Path, class_property: str | None = None) -> AnnotationSet:
    """Load Polygon/MultiPolygon features and map labels to compact integer IDs."""
    with Path(path).open("r", encoding="utf-8") as stream:
        document = json.load(stream)

    if document.get("type") == "FeatureCollection":
        features = document.get("features", [])
    elif document.get("type") == "Feature":
        features = [document]
    else:
        raise ValueError("GeoJSON root must be a FeatureCollection or Feature")

    class_ids: dict[str, int] = {}
    classes = [ClassInfo(0, "background", (0, 0, 0, 0))]
    shapes: list[tuple[dict[str, Any], int]] = []
    for index, feature in enumerate(features):
        geometry = feature.get("geometry")
        if not geometry or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        properties = feature.get("properties") or {}
        name = _class_name(properties, class_property)
        if name not in class_ids:
            class_id = len(classes)
            if class_id > 65535:
                raise ValueError("More than 65,535 classes are not supported")
            color_value = None
            for color_path in _AUTO_COLOR_PATHS:
                color_value = _nested(properties, color_path)
                if color_value is not None:
                    break
            class_ids[name] = class_id
            classes.append(ClassInfo(class_id, name, _parse_color(color_value, name)))
        shapes.append((geometry, class_ids[name]))

    if not shapes:
        raise ValueError(f"No Polygon or MultiPolygon features found in {path}")
    return AnnotationSet(shapes=shapes, classes=classes)
