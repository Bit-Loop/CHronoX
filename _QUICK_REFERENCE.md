# ChronoX Quick Reference

## ⚡ Quick Commands

### Setup & Start
```bash
# Activate environment and load env vars
source .venv/bin/activate && export $(cat .env | grep -v '^#' | xargs)

# Start databases
docker-compose up -d

# Check database status
docker ps
```

### Data Fetching
```bash
# Fetch test data (1 month)
python fetch_test_1month.py

# Fetch production data (5 years)
python fetch_simple.py

# Check data progress
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox -c \
"SELECT ticker, timeframe, COUNT(*) as bars FROM market_data GROUP BY ticker, timeframe;"
```

### Running ChronoX
```bash
# Show available commands
python main.py --action gui

# Backfill historical data
python main.py --action backfill --tickers NVDA,AMD,INTC --start 2020-10-18

# Train transformer model
python main.py --action train --model transformer --symbols 10 --timeframe 15min

# Run backtest
python main.py --action backtest --start 2024-01-01 --end 2024-12-31 --tickers NVDA

# Paper trading (simulated)
python main.py --action paper_trade --symbols 5
```

## 📊 Current Data Status

**Stocks**: NVDA, INTC, AMD, BBAI  
**Crypto**: BTC, ETH, XRP, SOL, ADA  
**Timeframes**: 1min, daily  
**Period**: 5 years (2020-10-18 to 2025-10-18)

## 🗄️ Database Ports

- **TimescaleDB**: localhost:5433 (user: postgres, password: chronox_db_password)
- **PostgreSQL**: localhost:5434 (user: chronox, password: chronox_db_pass_2024)
- **Redis**: localhost:6379 (password: chronox_redis_2024)

## 🔑 Environment Variables

```bash
# Required
POLYGON_API_KEY=HiOHFYhpVLC8H1qrb2DPy1vbUpHqe3iC

# Database (already configured)
TIMESCALE_HOST=localhost
TIMESCALE_PORT=5433
TIMESCALE_USER=postgres
TIMESCALE_PASSWORD=chronox_db_password

# Optional (for paper/live trading)
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
```

## 🚀 Hardware Limits

- **Short-term training (15min)**: 10-15 symbols
- **Long-term training (daily)**: 5-10 symbols
- **Real-time inference**: 20-30 symbols
- **Backtesting**: 100+ symbols (CPU)

## 🐛 Quick Troubleshooting

```bash
# Test imports
python test_startup.py
python test_ml.py

# Check GPU
nvidia-smi

# View logs
tail -f logs/chronox_$(date +%Y%m%d).log

# Restart databases
docker-compose restart

# Database query
PGPASSWORD=chronox_db_password psql -h localhost -p 5433 -U postgres -d chronox
```

## 📖 File Reference

- `_HOW_TO_USE.md` - Complete documentation
- `main.py` - Main entry point
- `fetch_simple.py` - Data fetching script
- `.env` - Environment variables
- `config/default.yaml` - System configuration
- `docker-compose.yml` - Database services
