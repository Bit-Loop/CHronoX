"""
Configuration Management for ChronoX (Personal Finance & Trading Platform).

Loads configuration from multiple sources in priority order:
1. Explicit arguments / overrides
2. Environment variables (.env supported)
3. YAML / JSON configuration files (with ${VAR} and ${VAR:-default} expansion)
4. Safe typed defaults

Security guarantees:
- Secrets are never serialized in plaintext by `to_safe_dict()` or `save()`.
- Environment placeholders are resolved safely without `eval()`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SENSITIVE_KEY_TOKENS = ("key", "secret", "password", "token", "webhook", "dsn")


def expand_env_vars(value: Any) -> Any:
    """Recursively expand `${VAR}` and `${VAR:-default}` placeholders."""
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2) if match.group(2) is not None else ""
            env_val = os.getenv(var_name)
            return env_val if env_val is not None and env_val != "" else default_val

        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(v) for v in value]
    return value


def redact_secrets(data: Any) -> Any:
    """Recursively mask sensitive fields before logging or saving config snapshots."""
    if isinstance(data, dict):
        sanitized: Dict[str, Any] = {}
        for k, v in data.items():
            lower_k = str(k).lower()
            if any(tok in lower_k for tok in _SENSITIVE_KEY_TOKENS):
                if isinstance(v, str) and v:
                    sanitized[k] = f"***{v[-4:]}" if len(v) > 8 else "***"
                else:
                    sanitized[k] = ""
            else:
                sanitized[k] = redact_secrets(v)
        return sanitized
    if isinstance(data, list):
        return [redact_secrets(v) for v in data]
    return data


def _filter_dataclass_kwargs(cls: type, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Filter dictionary keys to valid dataclass fields and coerce simple numeric strings."""
    valid_fields = {f.name: f for f in fields(cls)}
    filtered: Dict[str, Any] = {}
    for k, v in raw.items():
        if k not in valid_fields:
            continue
        if v == "" and valid_fields[k].type in (int, "int", float, "float", bool, "bool"):
            continue
        if isinstance(v, str):
            f_type = valid_fields[k].type
            if f_type in (int, "int"):
                try:
                    v = int(v)
                except ValueError:
                    continue
            elif f_type in (float, "float"):
                try:
                    v = float(v)
                except ValueError:
                    continue
            elif f_type in (bool, "bool"):
                v = v.strip().lower() in ("1", "true", "yes", "on")
        filtered[k] = v
    return filtered


@dataclass
class OpenCodeGoConfig:
    """OpenCode Go subscription API configuration."""

    api_key: str = field(
        default_factory=lambda: os.getenv("OPENCODE_GO_API_KEY") or os.getenv("OPENCODE_API_KEY", "")
    )
    base_url: str = field(
        default_factory=lambda: os.getenv("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1")
    )
    model: str = field(
        default_factory=lambda: os.getenv("OPENCODE_GO_MODEL", "glm-5.3-flash")
    )
    fallback_models: List[str] = field(
        default_factory=lambda: [
            "glm-5.3-flash",
            "glm-5.3",
            "deepseek-v4.1-flash",
            "kimi-k2.6",
            "qwen3.6-plus",
            "minimax-m2.7",
        ]
    )
    protocol: str = "auto"  # "auto", "openai", or "anthropic"
    timeout_seconds: int = 30
    max_daily_requests: int = field(
        default_factory=lambda: int(os.getenv("OPENCODE_GO_MAX_DAILY_REQUESTS", "60"))
    )
    cache_ttl_seconds: int = 900


@dataclass
class FinanceConfig:
    """ChronoX ADHD-Friendly Personal Finance & Auto-Budgeting configuration."""

    host: str = field(default_factory=lambda: os.getenv("CHRONOX_FINANCE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("CHRONOX_FINANCE_PORT", "8742")))
    db_path: str = field(
        default_factory=lambda: os.getenv("CHRONOX_FINANCE_DB_PATH", ".chronox/finance.sqlite3")
    )
    currency: str = field(default_factory=lambda: os.getenv("CHRONOX_FINANCE_CURRENCY", "USD"))
    default_cooling_hours: int = 48
    emergency_buffer_cents: int = 50_000  # $500.00 default overdraft/shock shield
    hourly_wage_cents: int = 3_000  # $30.00/hr default for ADHD work-hours impulse translator
    require_auth: bool = False


@dataclass
class PolygonConfig:
    """Polygon.io API configuration."""

    api_key: str = field(default_factory=lambda: os.getenv("POLYGON_API_KEY", ""))
    base_url: str = "https://api.polygon.io"
    rate_limit_calls: int = 5
    rate_limit_period: int = 1
    retry_attempts: int = 3
    retry_backoff_multiplier: float = 1.0
    retry_backoff_min: float = 4.0
    retry_backoff_max: float = 10.0
    timeout: int = 30


@dataclass
class TimescaleConfig:
    """TimescaleDB configuration."""

    host: str = field(default_factory=lambda: os.getenv("TIMESCALE_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.getenv("TIMESCALE_PORT", "5433")))
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
    use_amp: bool = True
    use_gradient_checkpointing: bool = False
    device: str = "cuda"
    num_workers: int = 4
    pin_memory: bool = True
    timescales: List[str] = field(
        default_factory=lambda: ["1min", "15min", "1hour", "12hour", "daily"]
    )
    short_term_sequence_length: int = 1000
    long_term_sequence_length: int = 5000
    max_symbols_short_term: int = 15
    max_symbols_long_term: int = 10
    max_symbols_inference: int = 30


@dataclass
class BacktestConfig:
    """Backtesting configuration."""

    initial_capital: float = 100000.0
    commission_rate: float = 0.001
    slippage_bps: float = 5.0
    max_position_size: float = 0.1
    risk_free_rate: float = 0.04
    calculate_sharpe: bool = True
    calculate_sortino: bool = True
    calculate_max_drawdown: bool = True
    calculate_calmar: bool = True


@dataclass
class TradingConfig:
    """Live/paper trading configuration."""

    mode: str = "paper"
    max_symbols: int = 10
    position_sizing: str = "kelly"
    rebalance_frequency: str = "daily"
    max_portfolio_risk: float = 0.02
    max_daily_loss: float = 0.05
    max_positions: int = 20
    enforce_budget_guardrail: bool = True
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    alpaca_base_url: str = field(
        default_factory=lambda: os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    )


@dataclass
class MonitoringConfig:
    """Monitoring and alerting configuration."""

    prometheus_port: int = 9090
    grafana_port: int = 3000
    mlflow_port: int = 5000
    enable_email_alerts: bool = False
    enable_slack_alerts: bool = False
    enable_telegram_alerts: bool = False
    alert_email: str = field(default_factory=lambda: os.getenv("ALERT_EMAIL", ""))
    slack_webhook: str = field(default_factory=lambda: os.getenv("SLACK_WEBHOOK", ""))
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))


class ConfigManager:
    """Central configuration manager for ChronoX Finance & Trading."""

    def __init__(self, config_path: Optional[str] = None):
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)

        self.config_data: Dict[str, Any] = {}
        if config_path:
            self.config_data = self._load_config_file(config_path)

        self.opencode_go = self._init_section("opencode_go", OpenCodeGoConfig)
        self.finance = self._init_section("finance", FinanceConfig)
        self.polygon = self._init_section("polygon", PolygonConfig)
        self.timescale = self._init_section("timescale", TimescaleConfig)
        self.training = self._init_section("training", TrainingConfig)
        self.backtest = self._init_section("backtest", BacktestConfig)
        self.trading = self._init_section("trading", TradingConfig)
        self.monitoring = self._init_section("monitoring", MonitoringConfig)

    def _load_config_file(self, path: str) -> Dict[str, Any]:
        """Load YAML or JSON configuration and resolve environment placeholders."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(config_path, "r", encoding="utf-8") as f:
            if path.endswith((".yaml", ".yml")):
                raw = yaml.safe_load(f) or {}
            elif path.endswith(".json"):
                raw = json.load(f)
            else:
                raise ValueError(f"Unsupported config format: {path}")

        if not isinstance(raw, dict):
            raise ValueError(f"Configuration root must be a mapping: {path}")
        return expand_env_vars(raw)

    def _init_section(self, key: str, cls: type) -> Any:
        raw = self.config_data.get(key)
        if isinstance(raw, dict):
            return cls(**_filter_dataclass_kwargs(cls, raw))
        return cls()

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """Export configuration as dictionary (redacts secrets by default)."""
        data = {
            "opencode_go": asdict(self.opencode_go),
            "finance": asdict(self.finance),
            "polygon": asdict(self.polygon),
            "timescale": asdict(self.timescale),
            "training": asdict(self.training),
            "backtest": asdict(self.backtest),
            "trading": asdict(self.trading),
            "monitoring": asdict(self.monitoring),
        }
        return data if include_secrets else redact_secrets(data)

    def to_safe_dict(self) -> Dict[str, Any]:
        """Export configuration with all secrets masked."""
        return self.to_dict(include_secrets=False)

    def save(self, path: str) -> None:
        """Save redacted configuration snapshot to YAML or JSON."""
        config_dict = self.to_safe_dict()
        with open(path, "w", encoding="utf-8") as f:
            if path.endswith((".yaml", ".yml")):
                yaml.safe_dump(config_dict, f, sort_keys=False)
            elif path.endswith(".json"):
                json.dump(config_dict, f, indent=2)
            else:
                raise ValueError(f"Unsupported format: {path}")


_config_instance: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    """Get global configuration instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigManager()
    return _config_instance


def set_config(config: ConfigManager) -> None:
    """Set global configuration instance."""
    global _config_instance
    _config_instance = config
