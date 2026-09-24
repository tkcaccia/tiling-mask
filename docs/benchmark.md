# `crop_roi` benchmark

Input: the 43,458 × 39,886 `crop_roi` OME-TIFF and its corresponding GeoJSON.
The complete workflow writes one full-resolution categorical TIFF, four tile
TIFFs (28, 56, 90, and 224 pixels), and readable PNG previews. Both native
worker settings were 4 for the latest C++ run.

| Implementation | Median wall time | Throughput |
| --- | ---: | ---: |
| Original Python, one worker | 61.24 s | 28.30 MP/s |
| Optimized Python, four workers | 41.40 s | 41.87 MP/s |
| Previous C++, one raster worker | 22.66 s | 76.48 MP/s |
| Current C++, four raster workers | **19.20 s** | **90.30 MP/s** |

The current C++ runs took 20.06, 19.20, and 15.88 seconds. Their median is
reported above. These timings are specific to the test machine and are
affected by filesystem cache and concurrent compression. Pixel-mask and tile
class IDs were compared with the previous GDAL-backed C++ output: zero
differences across all 1,733,365,788 source pixels and all four tile grids.

The image below is the 224 × 224-tile grid rendered with distinct class
colors. The TIFF has the same grid dimensions but stores integer class IDs.

![Distinct-color tile grid](images/crop_roi_tile_224_multicolor.png)

Generated benchmark TIFFs and other previews are intentionally excluded from
Git to avoid publishing large outputs or the source dataset. Reproduce the
workflow using the command on the [main page](../README.md#run).
