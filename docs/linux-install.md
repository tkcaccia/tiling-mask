# Linux installation without sudo

The native program needs CMake 3.20+, a C++20 compiler with OpenMP, libtiff,
libpng, simdjson 3.x, and GDAL at runtime. First check which packages are
already available. Ubuntu 22.04 supplies simdjson 1.0.2, so the following
builds version 3.0.1 without replacing the system package. On a machine where
the other libraries are already present, this keeps the dependency and the
tool inside your home directory:

```bash
git clone --depth 1 --branch v3.0.1 \
  https://github.com/simdjson/simdjson.git "$HOME/.local/opt/tiling-mask-simdjson-3.0.1-src"
cmake -S "$HOME/.local/opt/tiling-mask-simdjson-3.0.1-src" \
  -B "$HOME/.local/opt/tiling-mask-simdjson-3.0.1-build" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
  -DCMAKE_INSTALL_PREFIX="$HOME/.local/opt/tiling-mask-deps"
cmake --build "$HOME/.local/opt/tiling-mask-simdjson-3.0.1-build" --parallel 4
cmake --install "$HOME/.local/opt/tiling-mask-simdjson-3.0.1-build"

git clone https://github.com/tkcaccia/tiling-mask.git "$HOME/.local/opt/tiling-mask"
cmake -S "$HOME/.local/opt/tiling-mask/cpp" \
  -B "$HOME/.local/opt/tiling-mask/cpp/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="$HOME/.local/opt/tiling-mask-deps"
cmake --build "$HOME/.local/opt/tiling-mask/cpp/build" --parallel 4
mkdir -p "$HOME/.local/bin"
install -m 0755 "$HOME/.local/opt/tiling-mask/cpp/build/tiling-mask-cpp" \
  "$HOME/.local/bin/tiling-mask-cpp"
```

The dependency is linked statically, so there is no simdjson runtime library
path to configure. Check the actual rasterizer after a run in
`metrics_cpp.json`: it should be `gdal` for byte-consistent polygon boundaries.
Use `$HOME/.local/bin/tiling-mask-cpp` if `~/.local/bin` is not on your `PATH`.
If `libgdal.so` is outside the system loader path, set `TILING_MASK_GDAL` to
its absolute path. The clean and optional border-error tile masks can be
generated using the command on the [main page](../README.md#run).
