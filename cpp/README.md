# Native C++ backend

The native backend uses simdjson, libtiff, libpng, memory mapping, OpenMP, and
the GDAL shared library bundled with Rasterio. It implements the same
deterministic majority-tile semantics as the Python CLI, with compact
categorical TIFF masks and standard indexed-color PNG previews. Calling the
same GDAL rasterizer as Rasterio makes polygon-boundary results byte-identical.

Build on Apple Silicon with Homebrew dependencies:

```bash
cmake -S cpp -B cpp/build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=/opt/homebrew/bin/clang++
cmake --build cpp/build --parallel 4
```

Run:

```bash
cpp/build/tiling-mask-cpp image.ome.tif annotations.geojson \
  -t 28 -t 56 -t 90 -t 224 \
  -j 4 --raster-workers 4 --render-rgb -o output_cpp
```

`--raster-workers` divides the image into independent row stripes. Each worker
uses its own GDAL dataset and geometry copies, then writes to a separate part
of the mask. Four raster workers and four output workers are the defaults. Set
`--raster-workers 1` to reproduce the original native rasterization path.

On `crop_roi`, the three-run median for the four-worker rasterizer was 19.20
seconds for the complete workflow, versus 22.66 seconds for the previous
single-worker rasterizer. The complete pixel mask and every tile grid matched
the previous result exactly.

The C++ implementation currently supports pixel-coordinate Polygon and
MultiPolygon annotations and up to 255 foreground classes. Set
`TILING_MASK_GDAL=/absolute/path/to/libgdal` if GDAL is not in the bundled
Rasterio or Homebrew locations. A native scanline fallback remains available,
but GDAL is required for exact parity on complex shared polygon boundaries.

Use `--border-error-percent 10 --border-error-seed 42` to create additional
tile-grid TIFFs with 10% of eligible tissue-boundary tiles assigned to an
adjacent tissue class. Eligibility uses four-connected neighbors in the clean
tile grid and excludes background. The clean masks stay intact. With
`--render-rgb`, extra multicolor PNG previews are written too; counts are
recorded in `metrics_cpp.json`. The default is 0%, so no extra files are made.
