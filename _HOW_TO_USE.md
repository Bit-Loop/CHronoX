# ChronoX Trading Bot - How To Use Guide

## 🚀 Quick Start

ChronoX is an AI-powered algorithmic trading bot that uses deep learning transformers to predict stock and cryptocurrency prices. This guide will walk you through setting up and running the system.

---

## 📋 Table of Contents

1. [System Requirements](#system-requirements)
2. [Installation](#installation)
3. [Database Setup](#database-setup)
4. [Data Fetching](#data-fetching)
5. [Running the System](#running-the-system)
6. [Available Actions](#available-actions)
7. [Configuration](#configuration)
8. [Monitoring](#monitoring)
9. [Troubleshooting](#troubleshooting)

---

## 💻 System Requirements

### Hardware (Tested Configuration)
- **CPU**: AMD Ryzen 9 9950X (16 cores / 32 threads) or equivalent
- **RAM**: 96 GB DDR5-6000 (minimum 32 GB recommended)
- **GPU**: NVIDIA RTX 5070 (12 GB VRAM) or any CUDA-capable GPU with 8+ GB VRAM
- **Storage**: Samsung 990 Pro NVMe (7 GB/s) - at least 500 GB free space

### Software
- **OS**: Linux (Ubuntu 22.04+ or Debian 12+)
- **Python**: 3.13.7
- **Docker**: Latest version
- **Docker Compose**: Latest version

### API Keys Required
- **Polygon.io API Key** (for market data) - Get from https://polygon.io
- **Alpaca API Key** (optional, for paper/live trading) - Get from https://alpaca.markets

---

## 🔧 Installation

### 1. Clone the Repository
```bash
cd /home/bitloop/Documents/GITHUB/ChronoX
```

### 2. Set Up Python Virtual Environment
```bash
# Virtual environment should already exist as .venv
source .venv/bin/activate
```

### 3. Verify Dependencies
All dependencies should already be installed:
- ✅ Base packages (numpy, pandas, scipy, etc.)
- ✅ ML packages (PyTorch 2.9.0 with CUDA 12.8, Transformers, etc.)
- ✅ Database drivers (psycopg2, asyncpg, redis)

To verify:
```bash
python test_startup.py  # Test base dependencies
python test_ml.py       # Test ML dependencies
```

### 4. Configure Environment Variables
Edit the `.env` file with your API keys:
```bash
nano .env
```

Required variables:
```bash
# Polygon.io API (REQUIRED)
POLYGON_API_KEY=your_polygon_api_key_here

# Database Configuration (Already set)
TIMESCALE_HOST=localhost
TIMESCALE_PORT=5433
TIMESCALE_DB=chronox
TIMESCALE_USER=postgres
TIMESCALE_PASSWORD=chronox_db_password

POSTGRES_HOST=localhost
POSTGRES_PORT=5434
POSTGRES_DB=chronox_metadata
POSTGRES_USER=chronox
POSTGRES_PASSWORD=chronox_db_pass_2024

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=chronox_redis_2024

# Alpaca API (Optional - for paper/live trading)
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

---

## 🗄️ Database Setup

### Start Database Services
ChronoX uses Docker to run three databases:
1. **TimescaleDB** (port 5433) - Time-series market data
2. **PostgreSQL** (port 5434) - Metadata and application state
3. **Redis** (port 6379) - Caching and message queue

Start all services:
```bash
docker-compose up -d
```

### Verify Databases are Running
```bash
docker ps
```

You should see three healthy containers:
```
chronox-timescaledb   (port 5433) - UP
chronox-postgres      (port 5434) - UP
chronox-redis         (port 6379) - UP
```

### Test Database Connection
```bash
# Test TimescaleDB
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox -c "SELECT version();"

# Test PostgreSQL
PGPASSWORD=chronox_db_pass_2024 psql -h localhost -p 5434 -U chronox -d chronox_metadata -c "SELECT version();"

# Test Redis
redis-cli -h localhost -p 6379 -a chronox_redis_2024 ping
```

---

## 📊 Data Fetching

### Option 1: Fetch Sample Data (Quick Test)
Fetch 1 month of data for testing:
```bash
source .venv/bin/activate
export $(cat .env | grep -v '^#' | xargs)
python fetch_test_1month.py
```

### Option 2: Fetch Full 5-Year Dataset (Production)
The system is currently configured to fetch:

**Stocks:**
- NVDA (NVIDIA)
- INTC (Intel)
- AMD (Advanced Micro Devices)
- BBAI (BigBear.ai)

**Crypto (via Polygon):**
- X:BTCUSD (Bitcoin)
- X:ETHUSD (Ethereum)
- X:XRPUSD (Ripple)
- X:SOLUSD (Solana)
- X:ADAUSD (Cardano)

Fetch all data (5 years, 1-minute resolution):
```bash
source .venv/bin/activate
export $(cat .env | grep -v '^#' | xargs)
python fetch_simple.py
```

**Note**: This will take several hours due to API rate limits. Progress is logged to console.

### Check Data Status
Monitor fetching progress:
```bash
# Check what's in the database
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox -c "
SELECT 
    ticker, 
    timeframe, 
    COUNT(*) as bars,
    MIN(time) as earliest,
    MAX(time) as latest
FROM market_data 
GROUP BY ticker, timeframe 
ORDER BY ticker, timeframe;
"
```

Example output:
```
 ticker | timeframe | bar_count |        earliest        |         latest         
--------+-----------+-----------+------------------------+------------------------
 AAPL   | 1day      |       251 | 2024-10-16 04:00:00+00 | 2025-10-16 04:00:00+00
 AAPL   | 1min      |    189293 | 2024-10-16 08:02:00+00 | 2025-10-16 23:59:00+00
 NVDA   | 1day      |       251 | 2024-10-16 04:00:00+00 | 2025-10-16 04:00:00+00
 NVDA   | 1min      |    246294 | 2024-10-16 08:00:00+00 | 2025-10-17 18:59:00+00
```

---

## 🎮 Running the System

### Load Environment Variables
Always load environment variables before running:
```bash
source .venv/bin/activate
export $(cat .env | grep -v '^#' | xargs)
```

### Display Available Commands
```bash
python main.py --action gui
```

Or see full help:
```bash
python main.py --help
```

---

## 📋 Available Actions

### 1. **Backfill Historical Data**
Download historical market data from Polygon.io:

```bash
python main.py --action backfill \
  --tickers AAPL,TSLA,NVDA,AMD,INTC \
  --start 2020-10-18 \
  --end 2025-10-18
```

Options:
- `--tickers`: Comma-separated list of stock symbols
- `--start`: Start date (YYYY-MM-DD)
- `--end`: End date (YYYY-MM-DD), defaults to today
- `--skip-minute`: Skip 1-minute data (faster, less storage)

### 2. **Train ML Models**
Train transformer models on historical data:

```bash
python main.py --action train \
  --model transformer \
  --symbols 15 \
  --timeframe 15min
```

Options:
- `--model`: Model type (`transformer`, `tft`, `rl`, `ensemble`)
- `--symbols`: Number of symbols to train on (10-15 for short-term, 5-10 for long-term)
- `--timeframe`: Training timeframe (`1min`, `15min`, `1hour`, `12hour`, `daily`)

**Hardware Scaling Guidelines:**
- **Short-term (15min)**: 10-15 symbols max (GPU with FP16)
- **Long-term (daily)**: 5-10 symbols max (GPU with FP16 + gradient checkpointing)
- **Inference**: 20-30 symbols max (GPU, no gradients)

### 3. **Run Backtest**
Test trading strategies on historical data:

```bash
python main.py --action backtest \
  --start 2020-01-01 \
  --end 2023-12-31 \
  --tickers AAPL,NVDA,TSLA
```

Options:
- `--start`: Backtest start date
- `--end`: Backtest end date
- `--tickers`: Specific symbols to backtest (optional, defaults to all trained symbols)

### 4. **Paper Trading** (Simulated Trading)
Run live trading simulation with fake money:

```bash
python main.py --action paper_trade --symbols 10
```

Options:
- `--symbols`: Number of symbols to trade (max 10 recommended)

### 5. **Live Trading** ⚠️ **DANGER: REAL MONEY**
Run live trading with real money (requires Alpaca API keys):

```bash
python main.py --action live_trade --symbols 5
```

**WARNING**: This trades with REAL MONEY. Only use after extensive backtesting and paper trading.

---

## ⚙️ Configuration

### Configuration File
Edit `config/default.yaml` to customize:

```yaml
# Polygon.io API rate limits
polygon:
  rate_limit_calls: 5
  rate_limit_period: 1  # seconds

# Training parameters
training:
  batch_size: 256
  learning_rate: 0.0001
  epochs: 100
  hidden_dim: 512
  num_layers: 8
  num_heads: 8
  dropout: 0.1
  use_amp: true  # Automatic Mixed Precision (saves 35-40% VRAM)

# Backtesting
backtest:
  initial_capital: 100000.0
  commission_rate: 0.001  # 0.1% per trade
  slippage_bps: 5.0  # 5 basis points

# Trading (Paper/Live)
trading:
  mode: "paper"  # Change to "live" for real trading
  max_portfolio_risk: 0.02  # 2% max loss per trade
  max_daily_loss: 0.05  # 5% circuit breaker
```

### Custom Configuration
Create a custom config file:
```bash
cp config/default.yaml config/production.yaml
nano config/production.yaml
```

Use it:
```bash
python main.py --config config/production.yaml --action train
```

---

## 📈 Monitoring

### Check Logs
```bash
# View real-time logs
tail -f logs/chronox_$(date +%Y%m%d).log

# Search for errors
grep ERROR logs/chronox_*.log
```

### Database Status
```bash
# Check table sizes
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox -c "
SELECT 
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
"

# Check compression status
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox -c "
SELECT 
    chunk_name,
    compression_status,
    before_compression_total_bytes,
    after_compression_total_bytes
FROM timescaledb_information.chunks
WHERE hypertable_name = 'market_data'
ORDER BY range_start DESC
LIMIT 10;
"
```

### GPU Monitoring
```bash
# Real-time GPU stats
watch -n 1 nvidia-smi

# GPU memory usage
nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv
```

---

## 🔧 Troubleshooting

### Database Connection Issues

**Problem**: `password authentication failed`
```bash
# Verify database password
grep TIMESCALE_PASSWORD .env

# Test connection
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox -c "SELECT 1;"
```

**Problem**: `port 5433 already in use`
```bash
# Check what's using the port
sudo lsof -i :5433

# Stop containers and restart
docker-compose down
docker-compose up -d
```

### API Rate Limiting

**Problem**: `Too many requests (429)`
```bash
# Polygon.io has rate limits. Adjust in config:
nano config/default.yaml

# Change:
polygon:
  rate_limit_calls: 5  # Lower this to 3 or 2
  rate_limit_period: 1
```

### CUDA/GPU Issues

**Problem**: `CUDA out of memory`
```bash
# Enable Automatic Mixed Precision in config/default.yaml:
training:
  use_amp: true  # Saves 35-40% VRAM
  use_gradient_checkpointing: true  # For very long sequences
  
# Reduce batch size:
training:
  batch_size: 128  # Try 128, 64, or 32
```

**Problem**: `No CUDA device found`
```bash
# Verify GPU is detected
nvidia-smi

# Test PyTorch CUDA
python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}'); print(f'Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"None\"}')"
```

### Data Fetching Stuck

**Problem**: Fetch process hangs or stalls
```bash
# Check if process is running
ps aux | grep fetch_simple.py

# Kill and restart
pkill -9 -f fetch_simple.py
python fetch_simple.py
```

---

## 📊 Example Workflow

### Complete End-to-End Example

```bash
# 1. Activate environment
source .venv/bin/activate
export $(cat .env | grep -v '^#' | xargs)

# 2. Start databases (if not running)
docker-compose up -d

# 3. Fetch 1 month of data for testing
python fetch_test_1month.py

# 4. Verify data was fetched
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox -c "
SELECT ticker, timeframe, COUNT(*) FROM market_data GROUP BY ticker, timeframe;
"

# 5. Run a backtest
python main.py --action backtest \
  --start 2024-09-18 \
  --end 2024-10-18 \
  --tickers NVDA

# 6. Train a transformer model
python main.py --action train \
  --model transformer \
  --symbols 5 \
  --timeframe 15min

# 7. Start paper trading
python main.py --action paper_trade --symbols 5
```

---

## 🎯 Performance Tips

### Optimize Data Fetching
- Use `--skip-minute` flag for backfill to save storage and time
- Fetch in batches (few stocks at a time) to avoid rate limits
- Use daily/hourly data for long-term analysis

### Optimize Training
- Use **FP16 (Automatic Mixed Precision)** for 35-40% VRAM savings
- Enable **gradient checkpointing** for sequences > 2000 timesteps
- Train on **15-minute or hourly** data for short-term predictions
- Train on **daily data** for long-term predictions
- Limit to **10-15 symbols** for 15min training, **5-10** for daily

### Optimize Storage
- Enable TimescaleDB compression (automatically done after 7 days)
- Archive old data to cold storage
- Use continuous aggregates for pre-computed features

---

## 📚 Additional Resources

- **Project Documentation**: See `DOCUMENTATION.md`
- **Architecture**: See `prefix_readme.md`
- **Phase Progress**: See `PHASE*_COMPLETE.md` and `PHASE*_PROGRESS.md`
- **Polygon.io API Docs**: https://polygon.io/docs
- **TimescaleDB Docs**: https://docs.timescale.com
- **PyTorch Docs**: https://pytorch.org/docs

---

## 🆘 Getting Help

### Check Logs
```bash
tail -f logs/chronox_$(date +%Y%m%d).log
```

### Run Diagnostics
```bash
# Test all imports
python test_imports.py

# Test ML dependencies
python test_ml.py

# Test database connection
python test_data_fetch.py
```

### Common Issues
1. **Port conflicts**: Change ports in `docker-compose.yml` and `.env`
2. **API key invalid**: Verify your Polygon.io API key at https://polygon.io
3. **Database errors**: Restart Docker containers with `docker-compose restart`
4. **CUDA errors**: Update NVIDIA drivers and verify with `nvidia-smi`

---

## ⚠️ Important Warnings

1. **NEVER use `--action live_trade` without extensive testing**
2. **Always backtest and paper trade first**
3. **Keep API keys secure - never commit `.env` file**
4. **Monitor GPU temperatures during training**
5. **Backup database regularly**
6. **Set reasonable risk limits in config**

---

## 📝 License & Disclaimer

**DISCLAIMER**: This software is for educational purposes only. Trading involves substantial risk of loss. The authors are not responsible for any financial losses incurred through use of this software.

---

**Version**: 1.0.0  
**Last Updated**: October 18, 2025  
**Maintained By**: ChronoX Team
