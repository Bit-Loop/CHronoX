"""
Configuration Management for ChronoX Trading Bot

Manages application configuration from multiple sources:
- Environment variables (.env)
- YAML/JSON configuration files
- Command-line arguments
- Defaults

Configuration follows the 12-factor app methodology:
https://12factor.net/config
"""

import os
import yaml
import json
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv


@dataclass
class PolygonConfig:
    """Polygon.io API configuration."""
    api_key: str = field(default_factory=lambda: os.getenv("POLYGON_API_KEY", ""))
    base_url: str = "https://api.polygon.io"
    rate_limit_calls: int = 5
    rate_limit_period: int = 1  # seconds
    retry_attempts: int = 3
    retry_backoff_multiplier: float = 1.0
    retry_backoff_min: float = 4.0
    retry_backoff_max: float = 10.0
    timeout: int = 30


@dataclass
class TimescaleConfig:
    """TimescaleDB configuration."""
    host: str = field(default_factory=lambda: os.getenv("TIMESCALE_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.getenv("TIMESCALE_PORT", "5432")))
    database: str = field(default_factory=lambda: os.getenv("TIMESCALE_DB", "chronox"))
    user: str = field(default_factory=lambda: os.getenv("TIMESCALE_USER", "postgres"))
    password: str = field(default_factory=lambda: os.getenv("TIMESCALE_PASSWORD", ""))
    min_connections: int = 1
    max_connections: int = 20


@dataclass
class TrainingConfig:
    """ML training configuration."""
    batch_size: int = 256
    learning_rate: float = 1e-4
    epochs: int = 100
    hidden_dim: int = 512
    num_layers: int = 8
    num_heads: int = 8
    dropout: float = 0.1
    gradient_clip: float = 1.0
    use_amp: bool = True  # Automatic Mixed Precision
    use_gradient_checkpointing: bool = False
    
    # Hardware optimization
    device: str = "cuda"  # or "cpu"
    num_workers: int = 4
    pin_memory: bool = True
    
    # Multi-scale settings
    timescales: list = field(default_factory=lambda: ["1min", "15min", "1hour", "12hour", "daily"])
    short_term_sequence_length: int = 1000  # ~1 week of 15min bars
    long_term_sequence_length: int = 5000   # ~months of 12hr bars
    
    # Scaling recommendations (from hardware analysis)
    max_symbols_short_term: int = 15  # For 15min/1hour training
    max_symbols_long_term: int = 10   # For 12hour/daily training
    max_symbols_inference: int = 30   # Real-time inference


@dataclass
class BacktestConfig:
    """Backtesting configuration."""
    initial_capital: float = 100000.0
    commission_rate: float = 0.001  # 0.1% per trade
    slippage_bps: float = 5.0  # 5 basis points
    max_position_size: float = 0.1  # 10% of portfolio per position
    risk_free_rate: float = 0.04  # 4% annual
    
    # Performance metrics
    calculate_sharpe: bool = True
    calculate_sortino: bool = True
    calculate_max_drawdown: bool = True
    calculate_calmar: bool = True


@dataclass
class TradingConfig:
    """Live/paper trading configuration."""
    mode: str = "paper"  # "paper" or "live"
    max_symbols: int = 10
    position_sizing: str = "kelly"  # "equal", "kelly", "risk_parity"
    rebalance_frequency: str = "daily"  # "daily", "weekly", "monthly"
    
    # Risk management
    max_portfolio_risk: float = 0.02  # 2% max loss per trade
    max_daily_loss: float = 0.05  # 5% max daily loss (circuit breaker)
    max_positions: int = 20
    
    # Alpaca API (paper/live trading)
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    alpaca_base_url: str = "https://paper-api.alpaca.markets"  # or live


@dataclass
class MonitoringConfig:
    """Monitoring and alerting configuration."""
    prometheus_port: int = 9090
    grafana_port: int = 3000
    mlflow_port: int = 5000
    
    # Alerting
    enable_email_alerts: bool = False
    enable_slack_alerts: bool = False
    enable_telegram_alerts: bool = False
    
    alert_email: str = field(default_factory=lambda: os.getenv("ALERT_EMAIL", ""))
    slack_webhook: str = field(default_factory=lambda: os.getenv("SLACK_WEBHOOK", ""))
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))


class ConfigManager:
    """
    Central configuration manager for ChronoX.
    
    Loads configuration from multiple sources in priority order:
    1. Command-line arguments (highest priority)
    2. Environment variables
    3. Configuration file (YAML/JSON)
    4. Defaults (lowest priority)
    
    Example:
        config = ConfigManager("config/production.yaml")
        
        # Access subsystem configs
        db_host = config.timescale.host
        api_key = config.polygon.api_key
        batch_size = config.training.batch_size
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize configuration manager.
        
        Args:
            config_path: Path to YAML/JSON config file. If None, uses defaults.
        """
        # Load environment variables from .env
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        
        # Load config file if provided
        self.config_data = {}
        if config_path:
            self.config_data = self._load_config_file(config_path)
        
        # Initialize subsystem configurations
        self.polygon = self._init_polygon_config()
        self.timescale = self._init_timescale_config()
        self.training = self._init_training_config()
        self.backtest = self._init_backtest_config()
        self.trading = self._init_trading_config()
        self.monitoring = self._init_monitoring_config()
    
    def _load_config_file(self, path: str) -> Dict[str, Any]:
        """Load configuration from YAML or JSON file."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        
        with open(config_path, 'r') as f:
            if path.endswith('.yaml') or path.endswith('.yml'):
                return yaml.safe_load(f) or {}
            elif path.endswith('.json'):
                return json.load(f)
            else:
                raise ValueError(f"Unsupported config format: {path}")
    
    def _init_polygon_config(self) -> PolygonConfig:
        """Initialize Polygon.io configuration."""
        if "polygon" in self.config_data:
            return PolygonConfig(**self.config_data["polygon"])
        return PolygonConfig()
    
    def _init_timescale_config(self) -> TimescaleConfig:
        """Initialize TimescaleDB configuration."""
        if "timescale" in self.config_data:
            return TimescaleConfig(**self.config_data["timescale"])
        return TimescaleConfig()
    
    def _init_training_config(self) -> TrainingConfig:
        """Initialize training configuration."""
        if "training" in self.config_data:
            return TrainingConfig(**self.config_data["training"])
        return TrainingConfig()
    
    def _init_backtest_config(self) -> BacktestConfig:
        """Initialize backtesting configuration."""
        if "backtest" in self.config_data:
            return BacktestConfig(**self.config_data["backtest"])
        return BacktestConfig()
    
    def _init_trading_config(self) -> TradingConfig:
        """Initialize trading configuration."""
        if "trading" in self.config_data:
            return TradingConfig(**self.config_data["trading"])
        return TradingConfig()
    
    def _init_monitoring_config(self) -> MonitoringConfig:
        """Initialize monitoring configuration."""
        if "monitoring" in self.config_data:
            return MonitoringConfig(**self.config_data["monitoring"])
        return MonitoringConfig()
    
    def to_dict(self) -> Dict[str, Any]:
        """Export all configuration as dictionary."""
        return {
            "polygon": self.polygon.__dict__,
            "timescale": self.timescale.__dict__,
            "training": self.training.__dict__,
            "backtest": self.backtest.__dict__,
            "trading": self.trading.__dict__,
            "monitoring": self.monitoring.__dict__,
        }
    
    def save(self, path: str):
        """
        Save current configuration to file.
        
        Args:
            path: Output path (.yaml or .json)
        """
        config_dict = self.to_dict()
        
        with open(path, 'w') as f:
            if path.endswith('.yaml') or path.endswith('.yml'):
                yaml.dump(config_dict, f, default_flow_style=False)
            elif path.endswith('.json'):
                json.dump(config_dict, f, indent=2)
            else:
                raise ValueError(f"Unsupported format: {path}")


# Singleton instance for global access
_config_instance: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    """
    Get global configuration instance.
    
    Returns:
        ConfigManager: Singleton configuration instance
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigManager()
    return _config_instance


def set_config(config: ConfigManager):
    """
    Set global configuration instance.
    
    Args:
        config: ConfigManager instance to use globally
    """
    global _config_instance
    _config_instance = config
