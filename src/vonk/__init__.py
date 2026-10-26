"""vonk: turn the Scouts en Gidsen Tervant vonk PDFs into approved calendar events."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vonk")
    """Single source: ``version`` in pyproject.toml; CI refuses a tag that differs."""
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0+unknown"
