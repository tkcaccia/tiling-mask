from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .pipeline import TileSize, build_masks


def _tile_size(value: str) -> TileSize:
    text = value.lower().strip()
    try:
        if "x" in text:
            width_text, height_text = text.split("x", 1)
            width, height = int(width_text), int(height_text)
        else:
            width = height = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use N or WIDTHxHEIGHT, for example 512 or 512x256") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("tile dimensions must be positive")
    return TileSize(width, height)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tiling-mask",
        description="Rasterize GeoJSON annotations and aggregate them by majority class.",
    )
    parser.add_argument("image", type=Path, help="OME-TIFF reference image")
    parser.add_argument("annotations", type=Path, help="GeoJSON annotation file")
    parser.add_argument("--tile-size", "-t", action="append", type=_tile_size, required=True,
                        help="Repeatable tile size: N or WIDTHxHEIGHT")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Output directory")
    parser.add_argument("--class-property", help="Dotted GeoJSON property path, e.g. classification.name")
    parser.add_argument("--coordinates", choices=("pixel", "world"), default="pixel",
                        help="Interpret GeoJSON coordinates as image pixels (default) or geospatial coordinates")
    parser.add_argument("--all-touched", action="store_true", help="Burn every pixel touched by a polygon")
    parser.add_argument("--ignore-background", action="store_true",
                        help="Choose a foreground class whenever a tile contains any annotation")
    parser.add_argument("--compression", choices=("deflate", "lzw", "zstd", "none"), default="deflate")
    parser.add_argument("--chunk-megapixels", type=float, default=64,
                        help="Approximate aggregation working-set size (default: 64)")
    parser.add_argument("--workers", "-j", type=int, default=4,
                        help="Parallel output workers (default: 4; use 1 for sequential execution)")
    parser.add_argument("--border-error-percent", type=float, default=0,
                        help="Percentage of eligible tissue-boundary tiles to relabel; writes additional masks (default: 0)")
    parser.add_argument("--border-error-seed", type=int, default=0,
                        help="Reproducible boundary-tile selection seed (default: 0)")
    parser.add_argument("--render-rgb", action="store_true",
                        help="Write standard PNG previews, including distinct multicolor versions (default: off)")
    parser.add_argument("--render-full-rgb", action="store_true",
                        help="Also write full-size RGB BigTIFF masks for WSI viewers (default: off)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    compression = None if args.compression == "none" else args.compression
    try:
        metrics = build_masks(
            args.image,
            args.annotations,
            args.tile_size,
            args.output,
            class_property=args.class_property,
            coordinates=args.coordinates,
            all_touched=args.all_touched,
            ignore_background=args.ignore_background,
            compression=compression,
            chunk_megapixels=args.chunk_megapixels,
            workers=args.workers,
            border_error_percent=args.border_error_percent,
            border_error_seed=args.border_error_seed,
            render_rgb=args.render_rgb or args.render_full_rgb,
            render_full_rgb=args.render_full_rgb,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"tiling-mask: error: {exc}") from exc
    print(json.dumps(asdict(metrics), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
