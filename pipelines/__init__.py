"""
Data Pipeline Package

Orchestrates data ingestion, validation, and quality checks:
- Real-time streaming from Polygon.io WebSocket
- Batch backfill using REST API
- Data quality validation
- Anomaly detection
- Multi-resolution aggregation
"""

from .data_ingestion_pipeline import DataIngestionPipeline
from .quality_checks import DataQualityChecker, AnomalyDetector, QualityIssue

__all__ = [
    "DataIngestionPipeline",
    "DataQualityChecker",
    "AnomalyDetector",
    "QualityIssue"
]
