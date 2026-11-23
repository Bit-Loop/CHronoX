"""Polygon.io API client package for ChronoX."""

from __future__ import annotations

__version__ = "1.0.0"

from .aggregates import AggregatesClient, SnapshotClient
from .client import PolygonAPIError, PolygonClient
from .corporate_actions import CorporateActionsClient
from .indicators import IndicatorsClient
from .news import NewsClient
from .reference import ReferenceClient

try:
    from .websocket_client import PolygonWebSocketClient
except ImportError:  # Optional dependency: websocket-client
    PolygonWebSocketClient = None  # type: ignore[assignment,misc]

__all__ = [
    "PolygonClient",
    "PolygonAPIError",
    "AggregatesClient",
    "SnapshotClient",
    "CorporateActionsClient",
    "ReferenceClient",
    "NewsClient",
    "IndicatorsClient",
    "PolygonWebSocketClient",
]
