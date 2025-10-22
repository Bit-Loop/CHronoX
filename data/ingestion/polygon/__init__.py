"""Polygon.io API client package for ChronoX trading bot."""

__version__ = "0.1.0"

from .client import PolygonClient
from .aggregates import AggregatesClient, SnapshotClient
from .corporate_actions import CorporateActionsClient
from .reference import ReferenceClient
from .news import NewsClient
from .indicators import IndicatorsClient
from .websocket_client import PolygonWebSocketClient

__all__ = [
    "PolygonClient",
    "AggregatesClient",
    "SnapshotClient",
    "CorporateActionsClient",
    "ReferenceClient",
    "NewsClient",
    "IndicatorsClient",
    "PolygonWebSocketClient",
]
