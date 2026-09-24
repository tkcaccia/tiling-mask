"""Fast pixel- and tile-resolution masks from GeoJSON annotations."""

from .majority import majority_tiles

__all__ = ["majority_tiles"]
__version__ = "0.1.0"
