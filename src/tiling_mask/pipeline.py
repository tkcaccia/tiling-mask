from __future__ import annotations

import json
import math
import os
import tempfile
import time
import colorsys
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from .annotations import ClassInfo, load_geojson
from .majority import majority_tiles


@dataclass(frozen=True)
class TileSize:
    width: int
    height: int

    @property
    def slug(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass
class RunMetrics:
    width: int
    height: int
    features: int
    classes: int
    workers: int
    render_rgb: bool
    render_full_rgb: bool
    rasterize_seconds: float
    pixel_tiff_seconds: float
    tile_tiff_seconds: dict[str, float]
    output_wall_seconds: float
    total_seconds: float
    megapixels_per_second: float


def _profile(width: int, height: int, transform, crs, compression: str | None, dtype: str) -> dict:
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 1,
        "dtype": dtype,
        "nodata": 0,
        "transform": transform,
        "crs": crs,
        "compress": compression,
        "BIGTIFF": "IF_SAFER",
    }
    if width >= 16 and height >= 16:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    return profile


def _rgb_profile(width: int, height: int, transform, crs, compression: str | None) -> dict:
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 3,
        "dtype": "uint8",
        "transform": transform,
        "crs": crs,
        "compress": compression,
        "photometric": "RGB",
        "interleave": "pixel",
        "BIGTIFF": "IF_SAFER",
    }
    if width >= 16 and height >= 16:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    return profile


def _rgb_lookup(classes: list[ClassInfo]) -> np.ndarray:
    lookup = np.zeros((len(classes), 3), dtype=np.uint8)
    for item in classes:
        lookup[item.value] = item.color[:3]
    return lookup


def _distinct_rgb_lookup(classes: list[ClassInfo]) -> np.ndarray:
    """Build a stable high-contrast visualization palette per class ID."""
    lookup = np.zeros((len(classes), 3), dtype=np.uint8)
    for item in classes[1:]:
        hue = (item.value * 0.618033988749895) % 1.0
        rgb = colorsys.hsv_to_rgb(hue, 0.78, 0.95)
        lookup[item.value] = np.rint(np.asarray(rgb) * 255).astype(np.uint8)
    return lookup


def _write_colormap(dataset, classes: list[ClassInfo]) -> None:
    dataset.write_colormap(1, {item.value: item.color for item in classes})
    dataset.update_tags(
        1,
        CLASS_NAMES=json.dumps({str(item.value): item.name for item in classes}),
    )


def _preview_palette(classes: list[ClassInfo], *, distinct: bool) -> list[int]:
    palette = np.zeros((256, 3), dtype=np.uint8)
    if distinct:
        lookup = _distinct_rgb_lookup(classes)
        for item in classes:
            palette[item.value] = lookup[item.value]
    else:
        for item in classes:
            palette[item.value] = item.color[:3]
    return palette.ravel().tolist()


def _write_preview_png(
    path: Path,
    labels: np.ndarray,
    classes: list[ClassInfo],
    *,
    distinct: bool,
) -> None:
    from PIL import Image

    image = Image.fromarray(labels)
    image.putpalette(_preview_palette(classes, distinct=distinct))
    image.save(path, format="PNG", compress_level=1)


def _write_pixel_tiffs(
    path: Path,
    full_rgb_paths: tuple[Path, Path] | None,
    preview_paths: tuple[Path, Path] | None,
    mask: np.memmap,
    profile: dict,
    rgb_profile: dict | None,
    classes: list[ClassInfo],
) -> None:
    import rasterio

    with ExitStack() as stack:
        destination = stack.enter_context(rasterio.open(path, "w", **profile))
        rgb_destinations = []
        lookups = []
        if full_rgb_paths is not None and rgb_profile is not None:
            rgb_destinations = [
                stack.enter_context(rasterio.open(rgb_path, "w", **rgb_profile))
                for rgb_path in full_rgb_paths
            ]
            lookups = [_rgb_lookup(classes), _distinct_rgb_lookup(classes)]
        for _, window in destination.block_windows(1):
            row0, row1 = int(window.row_off), int(window.row_off + window.height)
            col0, col1 = int(window.col_off), int(window.col_off + window.width)
            labels = np.asarray(mask[row0:row1, col0:col1])
            destination.write(labels, 1, window=window)
            for rgb_destination, lookup in zip(rgb_destinations, lookups):
                rgb_destination.write(np.moveaxis(lookup[labels], 2, 0), window=window)
        _write_colormap(destination, classes)

    if preview_paths is not None:
        stride = max(1, math.ceil(max(mask.shape) / 4096))
        labels = np.asarray(mask[::stride, ::stride])
        for index, preview_path in enumerate(preview_paths):
            _write_preview_png(
                preview_path, labels, classes, distinct=index == 1
            )


def _write_tile_tiff(
    path: Path,
    full_rgb_paths: tuple[Path, Path] | None,
    preview_paths: tuple[Path, Path] | None,
    mask: np.memmap,
    size: TileSize,
    source_transform,
    crs,
    compression: str | None,
    classes: list[ClassInfo],
    chunk_megapixels: float,
    ignore_background: bool,
    dtype: str,
    render_full_rgb: bool,
) -> None:
    import rasterio
    from affine import Affine

    rows = math.ceil(mask.shape[0] / size.height)
    cols = math.ceil(mask.shape[1] / size.width)
    transform = source_transform @ Affine.scale(size.width, size.height)
    profile = _profile(cols, rows, transform, crs, compression, dtype)
    rgb_profile = (
        _rgb_profile(mask.shape[1], mask.shape[0], source_transform, crs, compression)
        if render_full_rgb
        else None
    )
    class_count = len(classes)
    tile_pixels = size.width * size.height
    tile_rows_per_chunk = max(1, int(chunk_megapixels * 1_000_000 / max(1, tile_pixels * cols)))
    preview_labels = (
        np.empty((rows, cols), dtype=dtype) if preview_paths is not None else None
    )

    with ExitStack() as stack:
        destination = stack.enter_context(rasterio.open(path, "w", **profile))
        rgb_destinations = []
        lookups = []
        if full_rgb_paths is not None and rgb_profile is not None:
            rgb_destinations = [
                stack.enter_context(rasterio.open(rgb_path, "w", **rgb_profile))
                for rgb_path in full_rgb_paths
            ]
        if rgb_destinations:
            lookups = [_rgb_lookup(classes), _distinct_rgb_lookup(classes)]
        for tile_row in range(0, rows, tile_rows_per_chunk):
            out_rows = min(tile_rows_per_chunk, rows - tile_row)
            pixel_row = tile_row * size.height
            pixel_end = min(mask.shape[0], (tile_row + out_rows) * size.height)
            result = majority_tiles(
                np.asarray(mask[pixel_row:pixel_end, :]),
                size.height,
                size.width,
                class_count=class_count,
                ignore_background=ignore_background,
            )
            destination.write(
                result,
                1,
                window=rasterio.windows.Window(0, tile_row, cols, out_rows),
            )
            for rgb_destination, lookup in zip(rgb_destinations, lookups):
                rgb = lookup[result]
                rgb = np.repeat(np.repeat(rgb, size.height, axis=0), size.width, axis=1)
                rgb = rgb[: pixel_end - pixel_row, : mask.shape[1], :]
                rgb_destination.write(
                    np.moveaxis(rgb, 2, 0),
                    window=rasterio.windows.Window(
                        0, pixel_row, mask.shape[1], pixel_end - pixel_row
                    ),
                )
            if preview_labels is not None:
                preview_labels[tile_row : tile_row + out_rows] = result
        _write_colormap(destination, classes)

    if preview_paths is not None and preview_labels is not None:
        for index, preview_path in enumerate(preview_paths):
            _write_preview_png(
                preview_path, preview_labels, classes, distinct=index == 1
            )


def _pixel_job(
    temporary_name: str,
    shape: tuple[int, int],
    dtype: str,
    path: Path,
    full_rgb_paths: tuple[Path, Path] | None,
    preview_paths: tuple[Path, Path] | None,
    profile: dict,
    rgb_profile: dict | None,
    classes: list[ClassInfo],
) -> tuple[str, float]:
    """Write the pixel TIFF in a worker process from a read-only memmap."""
    started = time.perf_counter()
    mask = np.memmap(temporary_name, mode="r", dtype=dtype, shape=shape)
    try:
        _write_pixel_tiffs(
            path, full_rgb_paths, preview_paths, mask, profile, rgb_profile, classes
        )
    finally:
        del mask
    return "pixel", time.perf_counter() - started


def _tile_job(
    temporary_name: str,
    shape: tuple[int, int],
    dtype: str,
    path: Path,
    full_rgb_paths: tuple[Path, Path] | None,
    preview_paths: tuple[Path, Path] | None,
    size: TileSize,
    source_transform,
    crs,
    compression: str | None,
    classes: list[ClassInfo],
    chunk_megapixels: float,
    ignore_background: bool,
    render_full_rgb: bool,
) -> tuple[str, float]:
    """Aggregate and write one tile size in a worker process."""
    started = time.perf_counter()
    mask = np.memmap(temporary_name, mode="r", dtype=dtype, shape=shape)
    try:
        _write_tile_tiff(
            path,
            full_rgb_paths,
            preview_paths,
            mask,
            size,
            source_transform,
            crs,
            compression,
            classes,
            chunk_megapixels,
            ignore_background,
            dtype,
            render_full_rgb,
        )
    finally:
        del mask
    return size.slug, time.perf_counter() - started


def build_masks(
    image_path: str | Path,
    geojson_path: str | Path,
    tile_sizes: Iterable[TileSize],
    output_dir: str | Path,
    *,
    class_property: str | None = None,
    coordinates: str = "pixel",
    all_touched: bool = False,
    ignore_background: bool = False,
    compression: str | None = "deflate",
    chunk_megapixels: float = 64,
    workers: int = 4,
    render_rgb: bool = False,
    render_full_rgb: bool = False,
) -> RunMetrics:
    """Create a pixel mask and one categorical TIFF per tile size."""
    import rasterio
    from affine import Affine
    from rasterio.features import rasterize

    started = time.perf_counter()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    sizes = list(tile_sizes)
    if not sizes:
        raise ValueError("At least one tile size is required")
    if coordinates not in {"pixel", "world"}:
        raise ValueError("coordinates must be 'pixel' or 'world'")
    if chunk_megapixels <= 0:
        raise ValueError("chunk_megapixels must be positive")
    if workers <= 0:
        raise ValueError("workers must be positive")

    annotations = load_geojson(geojson_path, class_property)
    dtype = "uint8" if len(annotations.classes) <= 256 else "uint16"
    with rasterio.open(image_path) as source:
        width, height = source.width, source.height
        source_transform, crs = source.transform, source.crs

    fd, temporary_name = tempfile.mkstemp(prefix="tiling-mask-", suffix=".bin", dir=output)
    os.close(fd)
    mask = np.memmap(temporary_name, mode="w+", dtype=dtype, shape=(height, width))
    try:
        raster_started = time.perf_counter()
        burn_transform = Affine.identity() if coordinates == "pixel" else source_transform
        rasterize(
            annotations.shapes,
            out=mask,
            transform=burn_transform,
            fill=0,
            all_touched=all_touched,
            dtype=dtype,
        )
        mask.flush()
        raster_seconds = time.perf_counter() - raster_started

        pixel_path = output / "mask_pixel.tif"
        pixel_rgb_path = output / "mask_pixel_rgb.tif"
        pixel_multicolor_path = output / "mask_pixel_multicolor_rgb.tif"
        pixel_preview_path = output / "mask_pixel_preview.png"
        pixel_multicolor_preview_path = output / "mask_pixel_preview_multicolor.png"
        output_started = time.perf_counter()
        pixel_seconds = 0.0
        tile_seconds: dict[str, float] = {}
        shape = (height, width)
        pixel_args = (
            temporary_name,
            shape,
            dtype,
            pixel_path,
            (pixel_rgb_path, pixel_multicolor_path) if render_full_rgb else None,
            (pixel_preview_path, pixel_multicolor_preview_path) if render_rgb else None,
            _profile(width, height, source_transform, crs, compression, dtype),
            _rgb_profile(width, height, source_transform, crs, compression)
            if render_full_rgb
            else None,
            annotations.classes,
        )
        tile_args = [
            (
                temporary_name,
                shape,
                dtype,
                output / f"mask_tile_grid_{size.slug}.tif",
                (
                    output / f"mask_tile_{size.slug}_rgb.tif",
                    output / f"mask_tile_{size.slug}_multicolor_rgb.tif",
                ) if render_full_rgb else None,
                (
                    output / f"mask_tile_{size.slug}_preview.png",
                    output / f"mask_tile_{size.slug}_preview_multicolor.png",
                ) if render_rgb else None,
                size,
                source_transform,
                crs,
                compression,
                annotations.classes,
                chunk_megapixels,
                ignore_background,
                render_full_rgb,
            )
            for size in sizes
        ]

        if workers == 1:
            _, pixel_seconds = _pixel_job(*pixel_args)
            for args in tile_args:
                slug, elapsed = _tile_job(*args)
                tile_seconds[slug] = elapsed
        else:
            job_count = 1 + len(tile_args)
            with ProcessPoolExecutor(max_workers=min(workers, job_count)) as executor:
                futures = [executor.submit(_pixel_job, *pixel_args)]
                futures.extend(executor.submit(_tile_job, *args) for args in tile_args)
                for future in as_completed(futures):
                    name, elapsed = future.result()
                    if name == "pixel":
                        pixel_seconds = elapsed
                    else:
                        tile_seconds[name] = elapsed
        output_wall_seconds = time.perf_counter() - output_started

        manifest = {
            "source_image": str(Path(image_path).resolve()),
            "source_annotations": str(Path(geojson_path).resolve()),
            "coordinates": coordinates,
            "workers": workers,
            "render_rgb": render_rgb,
            "render_full_rgb": render_full_rgb,
            "tile_sizes": [asdict(size) for size in sizes],
            "classes": [asdict(item) for item in annotations.classes],
            "outputs": {
                "pixel_mask": pixel_path.name,
                "tile_grid_masks": {
                    size.slug: f"mask_tile_grid_{size.slug}.tif" for size in sizes
                },
            },
        }
        if render_rgb:
            manifest["outputs"].update({
                "pixel_preview": pixel_preview_path.name,
                "pixel_preview_multicolor": pixel_multicolor_preview_path.name,
                "tile_previews_rgb": {
                    size.slug: f"mask_tile_{size.slug}_preview.png"
                    for size in sizes
                },
                "tile_previews_multicolor_rgb": {
                    size.slug: f"mask_tile_{size.slug}_preview_multicolor.png"
                    for size in sizes
                },
            })
        if render_full_rgb:
            manifest["outputs"].update({
                "pixel_mask_rgb": pixel_rgb_path.name,
                "pixel_mask_multicolor_rgb": pixel_multicolor_path.name,
                "tile_masks_rgb": {
                    size.slug: f"mask_tile_{size.slug}_rgb.tif" for size in sizes
                },
                "tile_masks_multicolor_rgb": {
                    size.slug: f"mask_tile_{size.slug}_multicolor_rgb.tif"
                    for size in sizes
                },
            })
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        total = time.perf_counter() - started
        metrics = RunMetrics(
            width=width,
            height=height,
            features=len(annotations.shapes),
            classes=len(annotations.classes) - 1,
            workers=workers,
            render_rgb=render_rgb,
            render_full_rgb=render_full_rgb,
            rasterize_seconds=raster_seconds,
            pixel_tiff_seconds=pixel_seconds,
            tile_tiff_seconds={size.slug: tile_seconds[size.slug] for size in sizes},
            output_wall_seconds=output_wall_seconds,
            total_seconds=total,
            megapixels_per_second=(width * height / 1_000_000) / total,
        )
        (output / "metrics.json").write_text(json.dumps(asdict(metrics), indent=2), encoding="utf-8")
        return metrics
    finally:
        del mask
        Path(temporary_name).unlink(missing_ok=True)
