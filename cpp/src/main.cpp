#include <simdjson.h>
#include <tiffio.h>
#include <png.h>
#include <omp.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <dlfcn.h>

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

struct Point { double x, y; };
using Ring = std::vector<Point>;
using Polygon = std::vector<Ring>;

struct Feature {
    std::vector<Polygon> polygons;
    uint8_t class_id{};
};

struct ClassInfo {
    uint8_t id{};
    std::string name;
    std::array<uint8_t, 3> color{};
};

struct TileSize {
    int width{}, height{};
    std::string slug() const { return std::to_string(width) + "x" + std::to_string(height); }
};

struct TileResult {
    TileSize size;
    int width{}, height{};
    std::vector<uint8_t> labels;
    double seconds{};
};

struct Options {
    fs::path image, geojson, output;
    std::vector<TileSize> tiles;
    int workers = 4;
    int raster_workers = 4;
    int raster_chunk_rows = 0;
    bool render_previews = false;
};

class MappedFile {
public:
    MappedFile(const fs::path& path, size_t bytes) : path_(path), bytes_(bytes) {
        fd_ = ::open(path.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0600);
        if (fd_ < 0 || ftruncate(fd_, static_cast<off_t>(bytes)) != 0) {
            throw std::runtime_error("cannot create temporary mask: " + path.string());
        }
        data_ = static_cast<uint8_t*>(mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, 0));
        if (data_ == MAP_FAILED) throw std::runtime_error("cannot mmap temporary mask");
    }
    ~MappedFile() {
        if (data_ && data_ != MAP_FAILED) munmap(data_, bytes_);
        if (fd_ >= 0) close(fd_);
        std::error_code ec;
        fs::remove(path_, ec);
    }
    MappedFile(const MappedFile&) = delete;
    MappedFile& operator=(const MappedFile&) = delete;
    uint8_t* data() { return data_; }
private:
    fs::path path_;
    size_t bytes_{};
    int fd_ = -1;
    uint8_t* data_ = nullptr;
};

static double seconds_since(Clock::time_point start) {
    return std::chrono::duration<double>(Clock::now() - start).count();
}

static std::array<uint8_t, 3> fallback_color(const std::string& name) {
    uint64_t h = 1469598103934665603ULL;
    for (unsigned char c : name) { h ^= c; h *= 1099511628211ULL; }
    return {static_cast<uint8_t>(48 + h % 176),
            static_cast<uint8_t>(48 + (h >> 8) % 176),
            static_cast<uint8_t>(48 + (h >> 16) % 176)};
}

static bool parse_hex_color(std::string_view text, std::array<uint8_t, 3>& out) {
    if (!text.empty() && text.front() == '#') text.remove_prefix(1);
    if (text.size() < 6) return false;
    auto nibble = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (int i = 0; i < 3; ++i) {
        int a = nibble(text[2 * i]), b = nibble(text[2 * i + 1]);
        if (a < 0 || b < 0) return false;
        out[i] = static_cast<uint8_t>((a << 4) | b);
    }
    return true;
}

static double number(simdjson::dom::element value) {
    double d{};
    if (value.get(d) == simdjson::SUCCESS) return d;
    int64_t i{};
    if (value.get(i) == simdjson::SUCCESS) return static_cast<double>(i);
    uint64_t u{};
    if (value.get(u) == simdjson::SUCCESS) return static_cast<double>(u);
    throw std::runtime_error("GeoJSON coordinate is not numeric");
}

static Ring parse_ring(simdjson::dom::element element) {
    simdjson::dom::array array;
    if (element.get(array)) throw std::runtime_error("invalid GeoJSON ring");
    Ring ring;
    ring.reserve(array.size());
    for (auto point_element : array) {
        simdjson::dom::array pair;
        if (point_element.get(pair)) continue;
        auto it = pair.begin();
        if (it == pair.end()) continue;
        double x = number(*it++);
        if (it == pair.end()) continue;
        double y = number(*it);
        ring.push_back({x, y});
    }
    return ring;
}

static Polygon parse_polygon(simdjson::dom::element element) {
    simdjson::dom::array rings;
    if (element.get(rings)) throw std::runtime_error("invalid GeoJSON polygon");
    Polygon polygon;
    polygon.reserve(rings.size());
    for (auto ring : rings) {
        Ring parsed = parse_ring(ring);
        if (parsed.size() >= 3) polygon.push_back(std::move(parsed));
    }
    return polygon;
}

static std::string property_string(simdjson::dom::object props, std::string_view key) {
    auto result = props[key];
    if (result.error()) return {};
    std::string_view value;
    if (result.get(value) == simdjson::SUCCESS) return std::string(value);
    int64_t integer{};
    if (result.get(integer) == simdjson::SUCCESS) return std::to_string(integer);
    uint64_t unsigned_integer{};
    if (result.get(unsigned_integer) == simdjson::SUCCESS) return std::to_string(unsigned_integer);
    return {};
}

static std::array<uint8_t, 3> parse_color_element(
    simdjson::dom::element element, const std::string& name) {
    std::string_view string_value;
    std::array<uint8_t, 3> color{};
    if (element.get(string_value) == simdjson::SUCCESS && parse_hex_color(string_value, color)) {
        return color;
    }
    simdjson::dom::array values;
    if (element.get(values) == simdjson::SUCCESS) {
        int index = 0;
        for (auto value : values) {
            if (index >= 3) break;
            color[index++] = static_cast<uint8_t>(std::clamp(number(value), 0.0, 255.0));
        }
        if (index == 3) return color;
    }
    return fallback_color(name);
}

static std::pair<std::vector<Feature>, std::vector<ClassInfo>> load_geojson(const fs::path& path) {
    simdjson::dom::parser parser;
    simdjson::dom::element root = parser.load(path.string());
    simdjson::dom::array features_json;
    if (root["features"].get(features_json)) throw std::runtime_error("GeoJSON has no features array");

    std::vector<Feature> features;
    std::vector<ClassInfo> classes{{0, "background", {0, 0, 0}}};
    std::unordered_map<std::string, uint8_t> class_ids;

    for (auto feature_element : features_json) {
        simdjson::dom::object props;
        if (feature_element["properties"].get(props)) continue;
        std::string name;
        std::array<uint8_t, 3> color{};
        bool color_set = false;

        simdjson::dom::object classification;
        auto cls_result = props["classification"];
        if (!cls_result.error() && cls_result.get(classification) == simdjson::SUCCESS) {
            name = property_string(classification, "name");
            auto color_result = classification["color"];
            if (!color_result.error()) {
                color = parse_color_element(color_result.value(), name);
                color_set = true;
            }
        }
        for (std::string_view key : {"class", "label", "category", "name", "value"}) {
            if (name.empty()) name = property_string(props, key);
        }
        if (name.empty()) name = "annotation";
        if (!color_set) {
            auto color_result = props["color"];
            color = color_result.error() ? fallback_color(name)
                                         : parse_color_element(color_result.value(), name);
        }

        uint8_t class_id{};
        auto found = class_ids.find(name);
        if (found == class_ids.end()) {
            if (classes.size() >= 256) throw std::runtime_error("C++ backend supports at most 255 classes");
            class_id = static_cast<uint8_t>(classes.size());
            class_ids.emplace(name, class_id);
            classes.push_back({class_id, name, color});
        } else {
            class_id = found->second;
        }

        simdjson::dom::object geometry;
        if (feature_element["geometry"].get(geometry)) continue;
        std::string_view type;
        simdjson::dom::array coordinates;
        if (geometry["type"].get(type) || geometry["coordinates"].get(coordinates)) continue;
        Feature feature;
        feature.class_id = class_id;
        if (type == "Polygon") {
            Polygon polygon = parse_polygon(coordinates);
            if (!polygon.empty()) feature.polygons.push_back(std::move(polygon));
        } else if (type == "MultiPolygon") {
            feature.polygons.reserve(coordinates.size());
            for (auto polygon_element : coordinates) {
                Polygon polygon = parse_polygon(polygon_element);
                if (!polygon.empty()) feature.polygons.push_back(std::move(polygon));
            }
        }
        if (!feature.polygons.empty()) features.push_back(std::move(feature));
    }
    if (features.empty()) throw std::runtime_error("no Polygon or MultiPolygon annotations found");
    return {std::move(features), std::move(classes)};
}

struct Edge { int y_end; double x, step; };

static void rasterize_polygon(const Polygon& polygon, uint8_t value,
                              uint8_t* mask, int width, int height) {
    int min_y = height, max_y = 0;
    struct Pending { int y_start; Edge edge; };
    std::vector<Pending> pending;
    size_t edge_count = 0;
    for (const auto& ring : polygon) edge_count += ring.size();
    pending.reserve(edge_count);

    for (const auto& ring : polygon) {
        for (size_t i = 0; i < ring.size(); ++i) {
            Point a = ring[i], b = ring[(i + 1) % ring.size()];
            if (a.y == b.y) continue;
            if (a.y > b.y) std::swap(a, b);
            int ys = std::max(0, static_cast<int>(std::ceil(a.y - 0.5)));
            int ye = std::min(height, static_cast<int>(std::ceil(b.y - 0.5)));
            if (ys >= ye) continue;
            double step = (b.x - a.x) / (b.y - a.y);
            double x = a.x + ((ys + 0.5) - a.y) * step;
            pending.push_back({ys, {ye, x, step}});
            min_y = std::min(min_y, ys);
            max_y = std::max(max_y, ye);
        }
    }
    if (pending.empty()) return;
    std::sort(pending.begin(), pending.end(), [](const Pending& a, const Pending& b) {
        return a.y_start < b.y_start;
    });

    std::vector<Edge> active;
    active.reserve(pending.size());
    size_t next = 0;
    std::vector<double> intersections;
    for (int y = min_y; y < max_y; ++y) {
        while (next < pending.size() && pending[next].y_start == y) {
            active.push_back(pending[next++].edge);
        }
        active.erase(std::remove_if(active.begin(), active.end(),
                                    [y](const Edge& edge) { return edge.y_end <= y; }),
                     active.end());
        intersections.clear();
        intersections.reserve(active.size());
        for (const auto& edge : active) intersections.push_back(edge.x);
        std::sort(intersections.begin(), intersections.end());
        uint8_t* row = mask + static_cast<size_t>(y) * width;
        for (size_t i = 0; i + 1 < intersections.size(); i += 2) {
            int x0 = std::max(0, static_cast<int>(std::ceil(intersections[i] - 0.5)));
            int x1 = std::min(width, static_cast<int>(std::ceil(intersections[i + 1] - 0.5)));
            if (x1 > x0) std::memset(row + x0, value, static_cast<size_t>(x1 - x0));
        }
        for (auto& edge : active) edge.x += edge.step;
    }
}

static void rasterize_features_fallback(const std::vector<Feature>& features, uint8_t* mask,
                                        int width, int height) {
    for (const auto& feature : features) {
        for (const auto& polygon : feature.polygons) {
            rasterize_polygon(polygon, feature.class_id, mask, width, height);
        }
    }
}

class GdalApi {
public:
    using Handle = void*;
    using AllRegisterFn = void (*)();
    using GetDriverFn = Handle (*)(const char*);
    using CreateFn = Handle (*)(Handle, const char*, int, int, int, int, char**);
    using GetBandFn = Handle (*)(Handle, int);
    using SetTransformFn = int (*)(Handle, double*);
    using RasterIOFn = int (*)(Handle, int, int, int, int, int, void*, int, int, int,
                               intptr_t, intptr_t);
    using CloseFn = void (*)(Handle);
    using CreateGeometryFn = Handle (*)(int);
    using AddPointFn = void (*)(Handle, double, double);
    using AddGeometryDirectlyFn = int (*)(Handle, Handle);
    using DestroyGeometryFn = void (*)(Handle);
    using CloneGeometryFn = Handle (*)(Handle);
    using RasterizeFn = int (*)(Handle, int, const int*, int, const Handle*, void*, void*,
                                const double*, char**, void*, void*);

    explicit GdalApi(const fs::path& library) {
        library_ = dlopen(library.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (!library_) throw std::runtime_error(std::string("cannot load GDAL: ") + dlerror());
        all_register = symbol<AllRegisterFn>("GDALAllRegister");
        get_driver = symbol<GetDriverFn>("GDALGetDriverByName");
        create = symbol<CreateFn>("GDALCreate");
        get_band = symbol<GetBandFn>("GDALGetRasterBand");
        set_transform = symbol<SetTransformFn>("GDALSetGeoTransform");
        raster_io = symbol<RasterIOFn>("GDALRasterIO");
        close = symbol<CloseFn>("GDALClose");
        create_geometry = symbol<CreateGeometryFn>("OGR_G_CreateGeometry");
        add_point = symbol<AddPointFn>("OGR_G_AddPoint_2D");
        add_geometry_directly = symbol<AddGeometryDirectlyFn>("OGR_G_AddGeometryDirectly");
        destroy_geometry = symbol<DestroyGeometryFn>("OGR_G_DestroyGeometry");
        clone_geometry = symbol<CloneGeometryFn>("OGR_G_Clone");
        rasterize = symbol<RasterizeFn>("GDALRasterizeGeometries");
    }
    ~GdalApi() { if (library_) dlclose(library_); }

    AllRegisterFn all_register{};
    GetDriverFn get_driver{};
    CreateFn create{};
    GetBandFn get_band{};
    SetTransformFn set_transform{};
    RasterIOFn raster_io{};
    CloseFn close{};
    CreateGeometryFn create_geometry{};
    AddPointFn add_point{};
    AddGeometryDirectlyFn add_geometry_directly{};
    DestroyGeometryFn destroy_geometry{};
    CloneGeometryFn clone_geometry{};
    RasterizeFn rasterize{};

private:
    template <typename T> T symbol(const char* name) {
        auto value = reinterpret_cast<T>(dlsym(library_, name));
        if (!value) throw std::runtime_error(std::string("missing GDAL symbol: ") + name);
        return value;
    }
    void* library_{};
};

static fs::path find_gdal_library() {
    if (const char* configured = std::getenv("TILING_MASK_GDAL")) return configured;
    const std::vector<fs::path> roots = {
        "/Library/Frameworks/Python.framework/Versions/3.12/lib/python3.12/site-packages/rasterio/.dylibs",
        "/opt/homebrew/lib"
    };
    for (const auto& root : roots) {
        std::error_code ec;
        if (!fs::exists(root, ec)) continue;
        for (const auto& entry : fs::directory_iterator(root, ec)) {
            auto name = entry.path().filename().string();
            if (name.starts_with("libgdal") && name.ends_with(".dylib")) return entry.path();
        }
    }
    return {};
}

static GdalApi::Handle make_ogr_geometry(GdalApi& api, const Feature& feature) {
    constexpr int WKB_POLYGON = 3, WKB_MULTIPOLYGON = 6, WKB_LINEAR_RING = 101;
    auto multi = api.create_geometry(WKB_MULTIPOLYGON);
    for (const auto& polygon_data : feature.polygons) {
        auto polygon = api.create_geometry(WKB_POLYGON);
        for (const auto& ring_data : polygon_data) {
            auto ring = api.create_geometry(WKB_LINEAR_RING);
            for (const auto& point : ring_data) api.add_point(ring, point.x, point.y);
            if (!ring_data.empty() &&
                (ring_data.front().x != ring_data.back().x || ring_data.front().y != ring_data.back().y)) {
                api.add_point(ring, ring_data.front().x, ring_data.front().y);
            }
            if (api.add_geometry_directly(polygon, ring) != 0) {
                api.destroy_geometry(ring);
                api.destroy_geometry(polygon);
                api.destroy_geometry(multi);
                throw std::runtime_error("GDAL rejected polygon ring");
            }
        }
        if (api.add_geometry_directly(multi, polygon) != 0) {
            api.destroy_geometry(polygon);
            api.destroy_geometry(multi);
            throw std::runtime_error("GDAL rejected polygon");
        }
    }
    return multi;
}

static bool rasterize_features_gdal(const std::vector<Feature>& features, uint8_t* mask,
                                    int width, int height, int raster_chunk_rows,
                                    int raster_workers) {
    fs::path library = find_gdal_library();
    if (library.empty()) return false;
    GdalApi api(library);
    api.all_register();
    auto driver = api.get_driver("MEM");
    if (!driver) throw std::runtime_error("GDAL MEM driver unavailable");
    std::vector<GdalApi::Handle> geometries;
    std::vector<double> burn_values;
    geometries.reserve(features.size());
    burn_values.reserve(features.size());
    try {
        for (const auto& feature : features) {
            geometries.push_back(make_ogr_geometry(api, feature));
            burn_values.push_back(feature.class_id);
        }
        constexpr int GDT_BYTE = 1, GF_READ = 0;
        int stripes = std::min(raster_workers, height);
        std::vector<int> errors(stripes, 0);
        #pragma omp parallel for num_threads(stripes) schedule(static)
        for (int stripe = 0; stripe < stripes; ++stripe) {
            int y0 = static_cast<int>(static_cast<int64_t>(height) * stripe / stripes);
            int y1 = static_cast<int>(static_cast<int64_t>(height) * (stripe + 1) / stripes);
            auto dataset = api.create(driver, "", width, y1 - y0, 1, GDT_BYTE, nullptr);
            if (!dataset) { errors[stripe] = 1; continue; }
            std::vector<GdalApi::Handle> local_geometries;
            local_geometries.reserve(geometries.size());
            for (auto geometry : geometries) {
                auto copy = api.clone_geometry(geometry);
                if (!copy) { errors[stripe] = 1; break; }
                local_geometries.push_back(copy);
            }
            if (errors[stripe]) {
                for (auto geometry : local_geometries) api.destroy_geometry(geometry);
                api.close(dataset);
                continue;
            }
            double transform[6] = {0, 1, 0, static_cast<double>(y0), 0, 1};
            api.set_transform(dataset, transform);
            int band = 1;
            std::string chunk_option;
            char* raster_options[] = {nullptr, nullptr};
            if (raster_chunk_rows > 0) {
                chunk_option = "CHUNKYSIZE=" + std::to_string(raster_chunk_rows);
                raster_options[0] = chunk_option.data();
            }
            int error = api.rasterize(dataset, 1, &band, static_cast<int>(local_geometries.size()),
                                      local_geometries.data(), nullptr, nullptr, burn_values.data(),
                                      raster_options[0] ? raster_options : nullptr, nullptr, nullptr);
            if (error == 0) {
                auto raster_band = api.get_band(dataset, 1);
                error = api.raster_io(raster_band, GF_READ, 0, 0, width, y1 - y0,
                                      mask + static_cast<size_t>(y0) * width,
                                      width, y1 - y0, GDT_BYTE, 1, width);
            }
            errors[stripe] = error;
            for (auto geometry : local_geometries) api.destroy_geometry(geometry);
            api.close(dataset);
        }
        if (std::any_of(errors.begin(), errors.end(), [](int error) { return error != 0; })) {
            throw std::runtime_error("GDAL stripe rasterization failed");
        }
    } catch (...) {
        for (auto geometry : geometries) api.destroy_geometry(geometry);
        throw;
    }
    for (auto geometry : geometries) api.destroy_geometry(geometry);
    return true;
}

static std::string rasterize_features(const std::vector<Feature>& features, uint8_t* mask,
                                      int width, int height, int raster_chunk_rows,
                                      int raster_workers) {
    if (rasterize_features_gdal(
            features, mask, width, height, raster_chunk_rows, raster_workers)) return "gdal";
    rasterize_features_fallback(features, mask, width, height);
    return "native-scanline";
}

static std::array<uint8_t, 3> distinct_color(uint8_t id) {
    if (id == 0) return {0, 0, 0};
    double hue = std::fmod(id * 0.618033988749895, 1.0);
    double h = hue * 6.0;
    int sector = static_cast<int>(std::floor(h));
    double f = h - sector, v = 0.95, s = 0.78;
    double p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f));
    double r{}, g{}, b{};
    switch (sector % 6) {
        case 0: r=v; g=t; b=p; break; case 1: r=q; g=v; b=p; break;
        case 2: r=p; g=v; b=t; break; case 3: r=p; g=q; b=v; break;
        case 4: r=t; g=p; b=v; break; default: r=v; g=p; b=q; break;
    }
    return {static_cast<uint8_t>(std::lround(r * 255)),
            static_cast<uint8_t>(std::lround(g * 255)),
            static_cast<uint8_t>(std::lround(b * 255))};
}

static void set_tiff_palette(TIFF* tif, const std::vector<ClassInfo>& classes) {
    static thread_local std::array<uint16_t, 256> red{}, green{}, blue{};
    red.fill(0); green.fill(0); blue.fill(0);
    for (const auto& item : classes) {
        red[item.id] = static_cast<uint16_t>(item.color[0]) * 257;
        green[item.id] = static_cast<uint16_t>(item.color[1]) * 257;
        blue[item.id] = static_cast<uint16_t>(item.color[2]) * 257;
    }
    TIFFSetField(tif, TIFFTAG_COLORMAP, red.data(), green.data(), blue.data());
}

static void write_tiff(const fs::path& path, const uint8_t* data, int width, int height,
                       const std::vector<ClassInfo>& classes) {
    TIFF* tif = TIFFOpen(path.c_str(), "w");
    if (!tif) throw std::runtime_error("cannot create TIFF: " + path.string());
    TIFFSetField(tif, TIFFTAG_IMAGEWIDTH, width);
    TIFFSetField(tif, TIFFTAG_IMAGELENGTH, height);
    TIFFSetField(tif, TIFFTAG_SAMPLESPERPIXEL, 1);
    TIFFSetField(tif, TIFFTAG_BITSPERSAMPLE, 8);
    TIFFSetField(tif, TIFFTAG_SAMPLEFORMAT, SAMPLEFORMAT_UINT);
    TIFFSetField(tif, TIFFTAG_PHOTOMETRIC, PHOTOMETRIC_PALETTE);
    TIFFSetField(tif, TIFFTAG_PLANARCONFIG, PLANARCONFIG_CONTIG);
    TIFFSetField(tif, TIFFTAG_COMPRESSION, COMPRESSION_ADOBE_DEFLATE);
    TIFFSetField(tif, TIFFTAG_ZIPQUALITY, 1);
    TIFFSetField(tif, TIFFTAG_TILEWIDTH, 256);
    TIFFSetField(tif, TIFFTAG_TILELENGTH, 256);
    set_tiff_palette(tif, classes);
    std::vector<uint8_t> tile(256 * 256);
    for (int y = 0; y < height; y += 256) {
        for (int x = 0; x < width; x += 256) {
            std::fill(tile.begin(), tile.end(), 0);
            int copy_w = std::min(256, width - x), copy_h = std::min(256, height - y);
            for (int row = 0; row < copy_h; ++row) {
                std::memcpy(tile.data() + row * 256,
                            data + static_cast<size_t>(y + row) * width + x,
                            static_cast<size_t>(copy_w));
            }
            ttile_t index = TIFFComputeTile(tif, x, y, 0, 0);
            if (TIFFWriteEncodedTile(tif, index, tile.data(), tile.size()) < 0) {
                TIFFClose(tif); throw std::runtime_error("TIFF tile write failed");
            }
        }
    }
    TIFFClose(tif);
}

static void write_png(const fs::path& path, const uint8_t* data, int width, int height,
                      const std::vector<ClassInfo>& classes, bool distinct) {
    FILE* fp = std::fopen(path.c_str(), "wb");
    if (!fp) throw std::runtime_error("cannot create PNG: " + path.string());
    png_structp png = png_create_write_struct(PNG_LIBPNG_VER_STRING, nullptr, nullptr, nullptr);
    png_infop info = png_create_info_struct(png);
    if (!png || !info || setjmp(png_jmpbuf(png))) {
        if (png && info) png_destroy_write_struct(&png, &info);
        std::fclose(fp); throw std::runtime_error("PNG write failed");
    }
    png_init_io(png, fp);
    png_set_IHDR(png, info, width, height, 8, PNG_COLOR_TYPE_PALETTE,
                 PNG_INTERLACE_NONE, PNG_COMPRESSION_TYPE_DEFAULT, PNG_FILTER_TYPE_DEFAULT);
    std::vector<png_color> palette(std::max<size_t>(classes.size(), 1));
    for (const auto& item : classes) {
        auto color = distinct ? distinct_color(item.id) : item.color;
        palette[item.id] = {color[0], color[1], color[2]};
    }
    png_set_PLTE(png, info, palette.data(), static_cast<int>(palette.size()));
    png_set_compression_level(png, 1);
    png_write_info(png, info);
    for (int y = 0; y < height; ++y) {
        png_write_row(png, const_cast<png_bytep>(data + static_cast<size_t>(y) * width));
    }
    png_write_end(png, info);
    png_destroy_write_struct(&png, &info);
    std::fclose(fp);
}

static TileResult majority_grid(const uint8_t* mask, int width, int height,
                                TileSize size, int class_count) {
    auto start = Clock::now();
    TileResult result;
    result.size = size;
    result.width = (width + size.width - 1) / size.width;
    result.height = (height + size.height - 1) / size.height;
    result.labels.resize(static_cast<size_t>(result.width) * result.height);
    for (int ty = 0; ty < result.height; ++ty) {
        int y0 = ty * size.height, y1 = std::min(height, y0 + size.height);
        for (int tx = 0; tx < result.width; ++tx) {
            int x0 = tx * size.width, x1 = std::min(width, x0 + size.width);
            std::array<uint32_t, 256> counts{};
            for (int y = y0; y < y1; ++y) {
                const uint8_t* row = mask + static_cast<size_t>(y) * width;
                for (int x = x0; x < x1; ++x) ++counts[row[x]];
            }
            uint8_t winner = 0;
            for (int c = 1; c < class_count; ++c) {
                if (counts[c] > counts[winner]) winner = static_cast<uint8_t>(c);
            }
            result.labels[static_cast<size_t>(ty) * result.width + tx] = winner;
        }
    }
    result.seconds = seconds_since(start);
    return result;
}

static std::pair<int, int> image_dimensions(const fs::path& path) {
    TIFF* tif = TIFFOpen(path.c_str(), "r");
    if (!tif) throw std::runtime_error("cannot open image TIFF: " + path.string());
    uint32_t width{}, height{};
    TIFFGetField(tif, TIFFTAG_IMAGEWIDTH, &width);
    TIFFGetField(tif, TIFFTAG_IMAGELENGTH, &height);
    TIFFClose(tif);
    return {static_cast<int>(width), static_cast<int>(height)};
}

static TileSize parse_tile(const std::string& text) {
    auto pos = text.find('x');
    int w = std::stoi(pos == std::string::npos ? text : text.substr(0, pos));
    int h = pos == std::string::npos ? w : std::stoi(text.substr(pos + 1));
    if (w <= 0 || h <= 0) throw std::runtime_error("tile dimensions must be positive");
    return {w, h};
}

static Options parse_args(int argc, char** argv) {
    if (argc < 4) {
        throw std::runtime_error(
            "usage: tiling-mask-cpp IMAGE GEOJSON --output DIR --tile-size N [...] "
            "[--workers 4] [--raster-workers 4] [--render-rgb]");
    }
    Options options;
    options.image = argv[1]; options.geojson = argv[2];
    for (int i = 3; i < argc; ++i) {
        std::string arg = argv[i];
        if ((arg == "--output" || arg == "-o") && i + 1 < argc) options.output = argv[++i];
        else if ((arg == "--tile-size" || arg == "-t") && i + 1 < argc) options.tiles.push_back(parse_tile(argv[++i]));
        else if ((arg == "--workers" || arg == "-j") && i + 1 < argc) options.workers = std::stoi(argv[++i]);
        else if (arg == "--raster-workers" && i + 1 < argc) options.raster_workers = std::stoi(argv[++i]);
        else if (arg == "--raster-chunk-rows" && i + 1 < argc) options.raster_chunk_rows = std::stoi(argv[++i]);
        else if (arg == "--render-rgb") options.render_previews = true;
        else throw std::runtime_error("unknown or incomplete argument: " + arg);
    }
    if (options.output.empty() || options.tiles.empty()) throw std::runtime_error("--output and --tile-size are required");
    if (options.workers <= 0) throw std::runtime_error("workers must be positive");
    if (options.raster_workers <= 0) throw std::runtime_error("raster workers must be positive");
    if (options.raster_chunk_rows < 0) throw std::runtime_error("raster chunk rows must be nonnegative");
    return options;
}

static void write_manifest(const Options& options, int width, int height,
                           const std::vector<ClassInfo>& classes,
                           const std::string& rasterizer,
                           double raster_seconds, double output_seconds, double total_seconds,
                           const std::vector<TileResult>& results) {
    std::ofstream out(options.output / "metrics_cpp.json");
    out << "{\n  \"backend\": \"cpp\",\n  \"rasterizer\": \"" << rasterizer
        << "\",\n  \"width\": " << width
        << ",\n  \"height\": " << height << ",\n  \"workers\": " << options.workers
        << ",\n  \"raster_workers\": " << options.raster_workers
        << ",\n  \"render_rgb\": " << (options.render_previews ? "true" : "false")
        << ",\n  \"rasterize_seconds\": " << raster_seconds
        << ",\n  \"output_wall_seconds\": " << output_seconds
        << ",\n  \"total_seconds\": " << total_seconds
        << ",\n  \"megapixels_per_second\": "
        << (static_cast<double>(width) * height / 1e6 / total_seconds)
        << ",\n  \"tile_seconds\": {";
    for (size_t i = 0; i < results.size(); ++i) {
        if (i) out << ',';
        out << "\n    \"" << results[i].size.slug() << "\": " << results[i].seconds;
    }
    out << "\n  },\n  \"classes\": [";
    for (size_t i = 0; i < classes.size(); ++i) {
        if (i) out << ',';
        out << "\n    {\"value\": " << static_cast<int>(classes[i].id)
            << ", \"name\": \"" << classes[i].name << "\", \"color\": ["
            << static_cast<int>(classes[i].color[0]) << ','
            << static_cast<int>(classes[i].color[1]) << ','
            << static_cast<int>(classes[i].color[2]) << "]}";
    }
    out << "\n  ]\n}\n";
}

int main(int argc, char** argv) {
    try {
        Options options = parse_args(argc, argv);
        fs::create_directories(options.output);
        auto total_start = Clock::now();
        auto dimensions = image_dimensions(options.image);
        int width = dimensions.first;
        int height = dimensions.second;
        auto annotation_data = load_geojson(options.geojson);
        auto& features = annotation_data.first;
        auto& classes = annotation_data.second;
        MappedFile mapped(options.output / "mask_cpp.tmp", static_cast<size_t>(width) * height);

        auto raster_start = Clock::now();
        std::string rasterizer = rasterize_features(
            features, mapped.data(), width, height,
            options.raster_chunk_rows, options.raster_workers
        );
        double raster_seconds = seconds_since(raster_start);

        auto output_start = Clock::now();
        std::vector<TileResult> results(options.tiles.size());
        double pixel_seconds = 0.0;
        #pragma omp parallel num_threads(options.workers)
        {
            #pragma omp single
            {
                #pragma omp task
                {
                    auto start = Clock::now();
                    write_tiff(options.output / "mask_pixel_cpp.tif", mapped.data(), width, height, classes);
                    if (options.render_previews) {
                        int stride = std::max(1, static_cast<int>(std::ceil(std::max(width, height) / 4096.0)));
                        int pw = (width + stride - 1) / stride, ph = (height + stride - 1) / stride;
                        std::vector<uint8_t> preview(static_cast<size_t>(pw) * ph);
                        for (int y = 0; y < ph; ++y)
                            for (int x = 0; x < pw; ++x)
                                preview[static_cast<size_t>(y) * pw + x] = mapped.data()[static_cast<size_t>(y * stride) * width + x * stride];
                        write_png(options.output / "mask_pixel_preview_cpp.png", preview.data(), pw, ph, classes, false);
                        write_png(options.output / "mask_pixel_preview_multicolor_cpp.png", preview.data(), pw, ph, classes, true);
                    }
                    pixel_seconds = seconds_since(start);
                }
                for (size_t index = 0; index < options.tiles.size(); ++index) {
                    #pragma omp task firstprivate(index)
                    {
                        auto task_start = Clock::now();
                        TileResult result = majority_grid(mapped.data(), width, height, options.tiles[index], static_cast<int>(classes.size()));
                        auto slug = result.size.slug();
                        write_tiff(options.output / ("mask_tile_grid_" + slug + "_cpp.tif"), result.labels.data(), result.width, result.height, classes);
                        if (options.render_previews) {
                            write_png(options.output / ("mask_tile_" + slug + "_preview_cpp.png"), result.labels.data(), result.width, result.height, classes, false);
                            write_png(options.output / ("mask_tile_" + slug + "_preview_multicolor_cpp.png"), result.labels.data(), result.width, result.height, classes, true);
                        }
                        result.seconds = seconds_since(task_start);
                        results[index] = std::move(result);
                    }
                }
                #pragma omp taskwait
            }
        }
        double output_seconds = seconds_since(output_start);
        double total_seconds = seconds_since(total_start);
        write_manifest(options, width, height, classes, rasterizer, raster_seconds, output_seconds, total_seconds, results);
        std::cout << "{\"backend\":\"cpp\",\"rasterizer\":\"" << rasterizer
                  << "\",\"width\":" << width
                  << ",\"height\":" << height << ",\"workers\":" << options.workers
                  << ",\"raster_workers\":" << options.raster_workers
                  << ",\"rasterize_seconds\":" << raster_seconds
                  << ",\"pixel_seconds\":" << pixel_seconds
                  << ",\"output_wall_seconds\":" << output_seconds
                  << ",\"total_seconds\":" << total_seconds
                  << ",\"megapixels_per_second\":"
                  << (static_cast<double>(width) * height / 1e6 / total_seconds) << "}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "tiling-mask-cpp: error: " << error.what() << '\n';
        return 1;
    }
}
