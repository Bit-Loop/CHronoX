# ChronoX Trading Bot

**An AI-Powered Multi-Asset Trading System with Transformer Architecture**

ChronoX is a production-grade algorithmic trading bot that leverages state-of-the-art transformer models (Stockformer, MASTER), reinforcement learning (PPO/SAC), and sentiment analysis (FinBERT) to generate and execute trading signals across multiple asset classes.

## 🎯 Project Status

**Current Phase:** Phase 0-2 ✅ COMPLETE | Phase 3 🚧 In Progress

### Phase 0: Data Acquisition & Infrastructure ✅ COMPLETE
- ✅ Complete Polygon.io API client library (REST, WebSocket, S3)
- ✅ TimescaleDB integration for time-series data
- ✅ Data ingestion pipeline (REST API, WebSocket, **S3 flat files**)
- ✅ Data verification and quality checks
- ✅ S3 direct access for 100-1000x faster backfills
- ✅ Auto-cleanup temporary flat files
- ✅ Comprehensive documentation (_HOW_TO_USE.md, _QUICK_REFERENCE.md)

### Phase 1: Feature Engineering ✅ COMPLETE
- ✅ Multi-resolution data preprocessing (6 modules implemented)
- ✅ Technical indicators (18 functions: RSI, MACD, BB, ATR, ADX, etc.)
- ✅ Multi-timeframe aggregation (1min → 15min → 1hour → 12hour → daily)
- ✅ Market regime detection (trend/volatility/volume classification)
- ✅ Cross-scale features and volatility gating
- ✅ Fibonacci levels and retracement calculations

### Phase 2: ML Model Architecture ✅ COMPLETE
- ✅ Stockformer transformer (dual-head: price + signal classification)
- ✅ Temporal Fusion Transformer (TFT) with quantile predictions
- ✅ TimeScale Fusion (multi-scale TFT extension)
- ✅ FinBERT sentiment analysis integration
- ✅ Event impact analyzer
- ✅ RL trading environments (position sizing, leverage management)

### Phase 3: Training & Evaluation 🚧 IN PROGRESS
- ✅ TimeSeriesDataset and DataLoader implementations
- ✅ Multi-scale training pipeline framework
- ✅ Model evaluation metrics (Sharpe, Sortino, max drawdown)
- 🚧 First training runs on historical data
- ⏳ Hyperparameter tuning
- ⏳ Model checkpointing and MLflow tracking

### Phase 4: Reinforcement Learning ⏳ PENDING
- ✅ Trading environment (Gym-compatible)
- ✅ Position sizing framework
- ✅ Leverage management system
- ⏳ PPO/SAC agent implementation
- ⏳ Risk-adjusted reward functions (Sharpe-based)
- ⏳ Multi-asset portfolio optimization

### Phase 5-8: Production Systems ⏳ PENDING
- ⏳ Backtesting framework (Phase 5)
- ⏳ MLOps & orchestration (Phase 6-7)
- ⏳ Live trading integration (Phase 8)

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Data Layer (Phase 0-1)                  │
├─────────────────────────────────────────────────────────────┤
│  Polygon.io API → TimescaleDB (5yr history)                │
│  • OHLCV bars (1min, 5min, 15min, 1hr, 1day)              │
│  • Corporate actions, news, reference data                  │
│  • Real-time WebSocket streams                              │
│  • S3 flat files (100-1000x faster bulk downloads)         │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                   ML Pipeline (Phase 2-4)                   │
├─────────────────────────────────────────────────────────────┤
│  • Stockformer (price forecasting)                          │
│  • FinBERT (sentiment analysis)                             │
│  • RL Agents (PPO/SAC) for signal optimization              │
│  • Ensemble meta-learning                                   │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                Trading Engine (Phase 5-7)                   │
├─────────────────────────────────────────────────────────────┤
│  • Backtrader backtesting                                   │
│  • Alpaca API (paper/live trading)                          │
│  • Portfolio management & risk controls                      │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│              Monitoring & MLOps (Phase 6-8)                 │
├─────────────────────────────────────────────────────────────┤
│  • Airflow orchestration                                    │
│  • Prometheus metrics & Grafana dashboards                  │
│  • MLflow experiment tracking                               │
│  • Docker containerization                                  │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start (Phase 0)

### Prerequisites

- **Python 3.13+** (tested on 3.13.7)
- **Docker** (for TimescaleDB, PostgreSQL, Redis)
- **Polygon.io API Key** with S3 flat file access
- **96GB RAM** (recommended for full dataset)
- **500GB+ Storage** (for 5 years of minute-level data)
- **NVIDIA GPU** (optional, for ML training - RTX 4000+ series recommended)

### 1. Clone Repository

```bash
git clone https://github.com/yourusername/ChronoX.git
cd ChronoX
```

### 2. Set Up Environment

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env

# Edit .env and add your API keys and S3 credentials
nano .env  # or use your preferred editor

# Required environment variables:
# POLYGON_API_KEY=your_api_key_here
# POLYGON_S3_ACCESS_KEY=your_s3_access_key
# POLYGON_S3_SECRET_KEY=your_s3_secret_key
```

### 3. Start Databases

```bash
# Start TimescaleDB, PostgreSQL, and Redis
docker-compose up -d

# Verify containers are running
docker ps

# Should see:
# - chronox-timescaledb (port 5433)
# - chronox-postgres (port 5434)
# - chronox-redis (port 6379)
```

### 4. Test System Components

```bash
python test_system.py
```

Expected output:
```
======================================================================
  ChronoX System Test Suite
======================================================================

1. Testing Environment Variables
✅ POLYGON_API_KEY           = HiOHFYhp...
✅ POLYGON_S3_ACCESS_KEY     = 78f5261c...
✅ TIMESCALE_HOST            = localhost
✅ TIMESCALE_PORT            = 5433

2. Testing Database Connection
✅ Database Connected
   Version: PostgreSQL 16.10
   TimescaleDB: 2.22.1
✅ Market Data Table Ready

5. Testing GPU/CUDA
✅ PyTorch version: 2.9.0+cu128
✅ CUDA available: True
✅ GPU: NVIDIA GeForce RTX 5070 (11.5 GB)

🎉 All tests passed! ChronoX is ready to use.
```

### 5. Run Historical Data Backfill

**Option 1: Use S3 Flat Files (Recommended - 100-1000x faster)**

```bash
# Using main.py (recommended)
python main.py --action backfill --flatfiles --tickers AAPL,MSFT,NVDA --start 2020-01-01

# Full S&P 500 backfill (uses S3 flat files for speed)
python main.py --action backfill --flatfiles --tickers <SP500_LIST> --start 2020-01-01
```

**Option 2: Use REST API (Slower, no S3 credentials needed)**

```bash
# Backfill for specific tickers
python main.py --action backfill --tickers AAPL,TSLA,NVDA --start 2020-01-01

# Or use backfill script directly
python scripts/backfill_historical_data.py --tickers AAPL,TSLA,NVDA
```

**Performance Comparison:**

| Method | 1 Ticker (5 years) | 100 Tickers |
|--------|-------------------|-------------|
| REST API | ~30-60 minutes | ~50-100 hours |
| S3 Flat Files | ~3-5 minutes | ~5-10 hours |

### 6. Verify Data Quality

```bash
# Check database contents
PGPASSWORD=chronox_db_password psql -h 127.0.0.1 -p 5433 -U postgres -d chronox -c "
SELECT 
    ticker,
    timeframe,
    COUNT(*) as bar_count,
    MIN(time) as earliest,
    MAX(time) as latest
FROM market_data
GROUP BY ticker, timeframe
ORDER BY ticker, timeframe;
"
```

## 📊 Database Schema

See [_HOW_TO_USE.md](_HOW_TO_USE.md) for complete usage documentation.

### TimescaleDB Tables

| Table | Description | Type |
|-------|-------------|------|
| `market_data` | OHLCV bars (hypertable on time) | Time-series |
| `corporate_actions` | Dividends, splits | Standard |
| `reference_data` | Ticker metadata | Standard |
| `news` | News articles with sentiment | Standard |

### Key Features

- **Hypertables**: Automatic partitioning by time
- **Compression**: 10-20x storage reduction (enabled after 7 days)
- **Continuous Aggregates**: Pre-computed rollups for faster queries
- **ACID Transactions**: Full PostgreSQL reliability

## 🗂️ Project Structure

```
ChronoX/
├── data/
│   ├── ingestion/
│   │   └── polygon/          # Polygon.io API clients
│   │       ├── client.py     # Base client with retry logic
│   │       ├── aggregates.py # OHLCV bars & snapshots
│   │       ├── corporate_actions.py
│   │       ├── reference.py
│   │       ├── news.py
│   │       ├── indicators.py
│   │       ├── websocket_client.py
│   │       ├── flat_files.py # Bulk download handler
│   │       └── flat_file_parser.py
│   ├── preprocessing/        # Data cleaning & feature engineering
│   └── storage/
│       └── influx_writer.py  # InfluxDB writer
│
├── models/
│   ├── transformers/         # Stockformer, MASTER architectures
│   ├── rl_agents/            # PPO, SAC implementations
│   └── ensembles/            # Meta-learning & signal fusion
│
├── training/                 # Training pipelines
├── inference/                # Real-time inference
├── backtesting/              # Backtrader integration
├── trading/                  # Live trading engine
│
├── monitoring/
│   └── exporters/            # Prometheus exporters
│
├── pipelines/
│   └── airflow_dags/         # Orchestration workflows
│
├── scripts/
│   ├── backfill_historical_data.py  # Master backfill orchestrator
│   ├── verify_data.py               # Data quality checks
│   └── test_phase0.py               # Component tests
│
├── tests/                    # Unit & integration tests
├── docs/                     # Documentation
├── configs/                  # Configuration files
└── docker/                   # Docker configurations
```

## 📈 Data Coverage

### Supported Markets
- **US Equities** (Stocks, ETFs)
- **Cryptocurrencies** (future phase)
- **Forex** (future phase)

### Data Granularity
- **Minute bars**: 5 years history
- **Daily bars**: 5 years history
- **Hourly bars**: 5 years history
- **News**: 6 months rolling window
- **Corporate actions**: Full history

### Data Sources
- **Primary**: Polygon.io (15-min delayed on Starter plan)
- **Backup**: Alpaca Markets API
- **Alternative**: Yahoo Finance (limited features)

## 🛠️ Technology Stack

### Data & Storage
- **InfluxDB 2.7**: Time-series database
- **Redis**: Caching & pub/sub
- **MongoDB**: Document storage (future)

### Machine Learning
- **PyTorch**: Deep learning framework
- **TensorFlow**: Alternative framework
- **Ray RLlib**: Reinforcement learning
- **Hugging Face Transformers**: Pre-trained models

### Trading & Backtesting
- **Backtrader**: Backtesting engine
- **Alpaca API**: Brokerage integration
- **CCXT**: Crypto exchange integration

### MLOps & Orchestration
- **Apache Airflow**: Workflow orchestration
- **MLflow**: Experiment tracking
- **DVC**: Data version control
- **Docker**: Containerization

### Monitoring
- **Prometheus**: Metrics collection
- **Grafana**: Visualization
- **ELK Stack**: Logging (future)

## 🎓 Development Roadmap

### Phase 0: Data Acquisition & Infrastructure ✅ COMPLETE
- ✅ Polygon.io integration (REST + WebSocket + S3)
- ✅ TimescaleDB time-series storage
- ✅ S3 flat file bulk downloads (100-1000x faster)
- ✅ Data quality verification
- ✅ 5-year historical backfill capability
- ✅ Comprehensive documentation

### Phase 1: Feature Engineering ✅ COMPLETE
- ✅ Multi-resolution data preprocessing (6 modules)
- ✅ Technical indicators (18 functions: RSI, MACD, BB, ATR, ADX, OBV, etc.)
- ✅ Multi-timeframe aggregation (1min → 15min → 1hour → 12hour → daily)
- ✅ Market regime detection (trend/volatility/volume classification)
- ✅ Cross-scale features and volatility gating
- ✅ Fibonacci levels and retracement calculations

### Phase 2: ML Model Architecture ✅ COMPLETE
- ✅ Stockformer (dual-head: price prediction + signal classification)
- ✅ Temporal Fusion Transformer (TFT) with quantile predictions
- ✅ TimeScale Fusion (multi-scale TFT extension)
- ✅ FinBERT sentiment analysis integration
- ✅ Event impact analyzer
- ✅ RL trading environments (position sizing, leverage management)

### Phase 3: Training & Evaluation 🚧 IN PROGRESS
- ✅ TimeSeriesDataset and DataLoader
- ✅ Multi-scale training pipeline framework
- ✅ Evaluation metrics (Sharpe, Sortino, Calmar ratios)
- 🚧 First training runs on historical data
- ⏳ Hyperparameter tuning (learning rate, batch size, dropout)
- ⏳ Model checkpointing and MLflow tracking
- ⏳ TensorBoard visualization

### Phase 4: RL Agent Training ⏳
- ✅ Trading environment (Gym-compatible)
- ✅ Position sizing framework
- ✅ Leverage management system
- ⏳ PPO/SAC agent implementation
- ⏳ Risk-adjusted reward functions (Sharpe-based)
- ⏳ Multi-asset portfolio optimization

### Phase 5: Backtesting & Validation ⏳
- ⏳ Backtrader integration
- ⏳ Walk-forward analysis
- ⏳ Monte Carlo simulation
- ⏳ Performance metrics & risk analysis
- ⏳ Sharpe/Sortino/Calmar ratios

### Phase 6: MLOps & Orchestration ⏳
- ⏳ Apache Airflow DAGs
- ⏳ Automated retraining pipelines
- ⏳ MLflow model versioning
- ⏳ CI/CD pipeline
- ⏳ A/B testing framework

### Phase 7: Paper Trading ⏳
- ⏳ Alpaca API integration
- ⏳ Order management system
- ⏳ Real-time risk controls
- ⏳ Performance tracking & reporting
- ⏳ Slippage & commission modeling

### Phase 8: Live Trading & Production ⏳
- ⏳ Production deployment architecture
- ⏳ Real-time monitoring & alerting
- ⏳ Automated failover systems
- ⏳ Disaster recovery procedures
- ⏳ Compliance & audit logging

## 📚 Documentation

- [_HOW_TO_USE.md](_HOW_TO_USE.md) - Complete usage guide (21 sections)
- [_QUICK_REFERENCE.md](_QUICK_REFERENCE.md) - Quick command reference
- See `docs/` folder for additional technical documentation

## 🧪 Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test module
pytest tests/test_polygon_client.py
```

## 🤝 Contributing

This is a personal project, but suggestions and bug reports are welcome via GitHub Issues.

## ⚠️ Disclaimer

**This software is for educational and research purposes only.**

- Not financial advice
- Use at your own risk
- Past performance ≠ future results
- Always test in paper trading first
- Never risk more than you can afford to lose

## 📄 License

MIT License - See [LICENSE](LICENSE) file for details

## 📞 Contact

For questions or collaboration inquiries, open a GitHub issue.

---

**Status**: Phase 0-2 Complete | Phase 3 In Progress | Last Updated: 2025-10-18
