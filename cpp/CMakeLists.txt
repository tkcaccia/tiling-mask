cmake_minimum_required(VERSION 3.20)
project(tiling_mask_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(OpenMP REQUIRED)
find_path(SIMDJSON_INCLUDE_DIR simdjson.h PATHS /opt/homebrew/include REQUIRED)
find_library(SIMDJSON_LIBRARY simdjson PATHS /opt/homebrew/lib REQUIRED)
find_path(TIFF_INCLUDE_DIR tiffio.h PATHS /opt/homebrew/include REQUIRED)
find_library(TIFF_LIBRARY tiff PATHS /opt/homebrew/lib REQUIRED)
find_path(PNG_INCLUDE_DIR png.h PATHS /opt/homebrew/include REQUIRED)
find_library(PNG_LIBRARY png PATHS /opt/homebrew/lib REQUIRED)

add_executable(tiling-mask-cpp src/main.cpp)
target_include_directories(tiling-mask-cpp PRIVATE
  ${SIMDJSON_INCLUDE_DIR}
  ${TIFF_INCLUDE_DIR}
  ${PNG_INCLUDE_DIR}
)
target_link_libraries(tiling-mask-cpp PRIVATE
  OpenMP::OpenMP_CXX
  ${SIMDJSON_LIBRARY}
  ${TIFF_LIBRARY}
  ${PNG_LIBRARY}
)
target_compile_options(tiling-mask-cpp PRIVATE -O3 -DNDEBUG -Wall -Wextra)
