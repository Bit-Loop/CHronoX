"""
Logging utilities for ChronoX Trading Bot

Provides structured logging with:
- Multiple output formats (console, file, JSON)
- Log rotation and compression
- Performance tracking
- Context injection (request IDs, user IDs, etc.)
- Integration with monitoring systems (Prometheus, ELK)

Architecture follows best practices from:
- Python logging cookbook: https://docs.python.org/3/howto/logging-cookbook.html
- 12-factor app logging: https://12factor.net/logs
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime
import json


class JSONFormatter(logging.Formatter):
    """
    Format log records as JSON for structured logging.
    
    Compatible with log aggregation systems like:
    - Elasticsearch/Logstash/Kibana (ELK)
    - Splunk
    - CloudWatch Logs
    - Grafana Loki
    """
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Format log record as JSON.
        
        Args:
            record: Log record to format
            
        Returns:
            str: JSON-formatted log line
        """
        log_obj = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        # Add extra context fields
        if hasattr(record, "context"):
            log_obj["context"] = record.context
        
        return json.dumps(log_obj)


class ColoredFormatter(logging.Formatter):
    """
    Add ANSI color codes to console output for better readability.
    
    Colors:
    - DEBUG: Cyan
    - INFO: Green
    - WARNING: Yellow
    - ERROR: Red
    - CRITICAL: Bold Red
    """
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[1;31m', # Bold Red
    }
    RESET = '\033[0m'
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Format log record with ANSI colors.
        
        Args:
            record: Log record to format
            
        Returns:
            str: Colored log line
        """
        # Add color codes
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"
        
        # Format the message
        formatted = super().format(record)
        
        # Reset levelname for next handler
        record.levelname = levelname
        
        return formatted


def setup_logging(
    name: str = "ChronoX",
    log_dir: Optional[Path] = None,
    level: int = logging.INFO,
    enable_console: bool = True,
    enable_file: bool = True,
    enable_json: bool = False,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Setup logging with multiple handlers.
    
    Creates a logger with:
    - Console handler (colored, human-readable)
    - File handler (rotating, plain text)
    - JSON handler (optional, for log aggregation)
    
    Args:
        name: Logger name
        log_dir: Directory for log files. If None, uses ./logs
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        enable_console: Enable console output
        enable_file: Enable file output
        enable_json: Enable JSON output for log aggregation
        max_bytes: Max size per log file before rotation
        backup_count: Number of backup files to keep
        
    Returns:
        logging.Logger: Configured logger instance
        
    Example:
        logger = setup_logging(
            name="ChronoX.Training",
            log_dir=Path("logs"),
            level=logging.DEBUG
        )
        
        logger.info("Training started")
        logger.error("GPU out of memory", extra={"context": {"batch_size": 256}})
    """
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    
    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()
    
    # Console handler (colored, human-readable)
    if enable_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        
        console_formatter = ColoredFormatter(
            fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
    # File handlers
    if enable_file and log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Plain text file handler (rotating)
        text_log_path = log_dir / f"{name.replace('.', '_')}.log"
        file_handler = logging.handlers.RotatingFileHandler(
            filename=text_log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(level)
        
        file_formatter = logging.Formatter(
            fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(module)s:%(funcName)s:%(lineno)d | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
        
        # JSON file handler (rotating, for log aggregation)
        if enable_json:
            json_log_path = log_dir / f"{name.replace('.', '_')}.json"
            json_handler = logging.handlers.RotatingFileHandler(
                filename=json_log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding='utf-8'
            )
            json_handler.setLevel(level)
            json_handler.setFormatter(JSONFormatter())
            logger.addHandler(json_handler)
    
    return logger


class PerformanceLogger:
    """
    Context manager for logging function/block execution time.
    
    Example:
        with PerformanceLogger(logger, "Data preprocessing"):
            df = preprocess_data(raw_df)
        
        # Output: Data preprocessing completed in 2.34 seconds
    """
    
    def __init__(self, logger: logging.Logger, operation: str, level: int = logging.INFO):
        """
        Initialize performance logger.
        
        Args:
            logger: Logger instance
            operation: Description of operation being timed
            level: Log level for performance message
        """
        self.logger = logger
        self.operation = operation
        self.level = level
        self.start_time = None
    
    def __enter__(self):
        """Start timing."""
        self.start_time = datetime.now()
        self.logger.log(self.level, f"{self.operation} started")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop timing and log duration."""
        elapsed = (datetime.now() - self.start_time).total_seconds()
        
        if exc_type is None:
            self.logger.log(self.level, f"{self.operation} completed in {elapsed:.2f} seconds")
        else:
            self.logger.error(f"{self.operation} failed after {elapsed:.2f} seconds: {exc_val}")
        
        return False  # Don't suppress exceptions


def log_gpu_memory(logger: logging.Logger):
    """
    Log current GPU memory usage (if CUDA available).
    
    Args:
        logger: Logger instance
    """
    try:
        import torch
        if torch.cuda.is_available():
            allocated = torch.cuda.memory_allocated() / 1e9
            reserved = torch.cuda.memory_reserved() / 1e9
            max_allocated = torch.cuda.max_memory_allocated() / 1e9
            
            logger.info(
                f"GPU Memory: {allocated:.2f} GB allocated, "
                f"{reserved:.2f} GB reserved, "
                f"{max_allocated:.2f} GB max allocated"
            )
        else:
            logger.debug("CUDA not available, skipping GPU memory logging")
    except ImportError:
        logger.debug("PyTorch not installed, skipping GPU memory logging")


def log_system_resources(logger: logging.Logger):
    """
    Log current system resource usage (CPU, RAM).
    
    Args:
        logger: Logger instance
    """
    try:
        import psutil
        
        # CPU usage
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # RAM usage
        memory = psutil.virtual_memory()
        ram_used_gb = memory.used / 1e9
        ram_total_gb = memory.total / 1e9
        ram_percent = memory.percent
        
        logger.info(
            f"System Resources: CPU {cpu_percent:.1f}%, "
            f"RAM {ram_used_gb:.1f}/{ram_total_gb:.1f} GB ({ram_percent:.1f}%)"
        )
    except ImportError:
        logger.debug("psutil not installed, skipping system resource logging")


# Example usage
if __name__ == "__main__":
    # Setup logger
    logger = setup_logging(
        name="ChronoX.Example",
        log_dir=Path("../logs"),
        level=logging.DEBUG,
        enable_json=True
    )
    
    # Test different log levels
    logger.debug("Debug message - detailed diagnostic info")
    logger.info("Info message - normal operation")
    logger.warning("Warning message - potential issue")
    logger.error("Error message - something failed")
    logger.critical("Critical message - system failure")
    
    # Test performance logging
    with PerformanceLogger(logger, "Example operation"):
        import time
        time.sleep(1)
    
    # Test GPU memory logging
    log_gpu_memory(logger)
    
    # Test system resource logging
    log_system_resources(logger)
    
    # Test logging with context
    logger.info(
        "Trade executed",
        extra={
            "context": {
                "ticker": "AAPL",
                "action": "BUY",
                "quantity": 100,
                "price": 150.25
            }
        }
    )
