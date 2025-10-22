"""Data storage package initialization"""

from .timescale_writer import TimescaleWriter

# Deprecated: InfluxDB writer kept for reference only
# from .influx_writer import InfluxWriter, InfluxConfig

__all__ = ["TimescaleWriter"]
