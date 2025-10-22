-- TimescaleDB Schema for ChronoX Trading Bot
-- PostgreSQL 14+ with TimescaleDB 2.11+ extension
-- 
-- Run this script to initialize the database:
-- psql -U postgres -d chronox -f scripts/init_timescale_schema.sql

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- =============================================================================
-- HYPERTABLES (Time-Series Optimized Tables)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. market_data - OHLCV bars for all timeframes
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_data (
    time TIMESTAMPTZ NOT NULL,
    ticker TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume BIGINT NOT NULL,
    vwap DOUBLE PRECISION,
    transactions INTEGER,
    
    -- Data integrity constraints
    CONSTRAINT market_data_ohlc_check CHECK (
        high >= low AND 
        high >= open AND 
        high >= close AND 
        low <= open AND 
        low <= close
    ),
    CONSTRAINT market_data_volume_check CHECK (volume >= 0)
);

-- Convert to hypertable (chunks by 1 day)
SELECT create_hypertable(
    'market_data', 
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Create indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_market_data_ticker_time 
    ON market_data (ticker, time DESC);

CREATE INDEX IF NOT EXISTS idx_market_data_timeframe 
    ON market_data (timeframe, time DESC);

CREATE INDEX IF NOT EXISTS idx_market_data_ticker_timeframe_time
    ON market_data (ticker, timeframe, time DESC);

-- Enable compression (10-20x space savings)
ALTER TABLE market_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ticker,timeframe',
    timescaledb.compress_orderby = 'time DESC'
);

-- Compress chunks older than 7 days
SELECT add_compression_policy('market_data', INTERVAL '7 days');

-- Retention policy: keep 5 years
SELECT add_retention_policy('market_data', INTERVAL '5 years');

COMMENT ON TABLE market_data IS 'OHLCV time-series data for all assets and timeframes';

-- -----------------------------------------------------------------------------
-- 2. corporate_actions - Dividends and stock splits
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corporate_actions (
    time TIMESTAMPTZ NOT NULL,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL, -- 'dividend' or 'split'
    amount DOUBLE PRECISION, -- Dividend amount per share
    ratio DOUBLE PRECISION, -- Split ratio (e.g., 2.0 for 2:1 split)
    declaration_date DATE,
    ex_date DATE,
    pay_date DATE,
    execution_date DATE,
    
    CONSTRAINT corporate_actions_type_check CHECK (action_type IN ('dividend', 'split'))
);

-- Convert to hypertable
SELECT create_hypertable(
    'corporate_actions', 
    'time',
    if_not_exists => TRUE
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_corporate_actions_ticker 
    ON corporate_actions (ticker, time DESC);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_type 
    ON corporate_actions (action_type, time DESC);

-- No retention policy (keep forever for historical accuracy)

COMMENT ON TABLE corporate_actions IS 'Stock splits and dividend payments';

-- -----------------------------------------------------------------------------
-- 3. news - News articles and sentiment
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news (
    time TIMESTAMPTZ NOT NULL,
    ticker TEXT NOT NULL,
    publisher TEXT,
    title TEXT NOT NULL,
    author TEXT,
    article_url TEXT,
    tickers_list TEXT[], -- Array of related tickers
    keywords TEXT[], -- Array of keywords
    sentiment_score DOUBLE PRECISION,
    
    CONSTRAINT news_sentiment_range CHECK (
        sentiment_score IS NULL OR 
        (sentiment_score >= -1 AND sentiment_score <= 1)
    )
);

-- Convert to hypertable (chunks by 1 day)
SELECT create_hypertable(
    'news', 
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_news_ticker_time 
    ON news (ticker, time DESC);

CREATE INDEX IF NOT EXISTS idx_news_publisher 
    ON news (publisher, time DESC);

CREATE INDEX IF NOT EXISTS idx_news_sentiment 
    ON news (sentiment_score, time DESC) 
    WHERE sentiment_score IS NOT NULL;

-- GIN index for array searches
CREATE INDEX IF NOT EXISTS idx_news_tickers_list 
    ON news USING GIN (tickers_list);

CREATE INDEX IF NOT EXISTS idx_news_keywords 
    ON news USING GIN (keywords);

-- Enable compression
ALTER TABLE news SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ticker,publisher',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('news', INTERVAL '7 days');

-- Retention policy: keep 6 months
SELECT add_retention_policy('news', INTERVAL '6 months');

COMMENT ON TABLE news IS 'News articles with sentiment analysis';

-- -----------------------------------------------------------------------------
-- 4. snapshots - Real-time market snapshots
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snapshots (
    time TIMESTAMPTZ NOT NULL,
    ticker TEXT NOT NULL,
    last_trade_price DOUBLE PRECISION,
    last_trade_size INTEGER,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    bid_size INTEGER,
    ask_size INTEGER,
    volume BIGINT,
    day_open DOUBLE PRECISION,
    day_high DOUBLE PRECISION,
    day_low DOUBLE PRECISION,
    day_close DOUBLE PRECISION,
    day_volume BIGINT,
    prev_day_close DOUBLE PRECISION
);

-- Convert to hypertable (chunks by 1 hour for high-frequency data)
SELECT create_hypertable(
    'snapshots', 
    'time',
    chunk_time_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_time 
    ON snapshots (ticker, time DESC);

-- Enable compression
ALTER TABLE snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ticker',
    timescaledb.compress_orderby = 'time DESC'
);

-- Compress after 1 day
SELECT add_compression_policy('snapshots', INTERVAL '1 day');

-- Retention policy: keep 30 days
SELECT add_retention_policy('snapshots', INTERVAL '30 days');

COMMENT ON TABLE snapshots IS 'Real-time market snapshots (15-min delayed on Starter plan)';

-- -----------------------------------------------------------------------------
-- 5. indicators - Technical indicators
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS indicators (
    time TIMESTAMPTZ NOT NULL,
    ticker TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    indicator_type TEXT NOT NULL, -- 'SMA', 'EMA', 'MACD', 'RSI', etc.
    value DOUBLE PRECISION NOT NULL,
    signal DOUBLE PRECISION,
    metadata JSONB -- For storing indicator-specific parameters
);

-- Convert to hypertable
SELECT create_hypertable(
    'indicators', 
    'time',
    if_not_exists => TRUE
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_indicators_ticker_type 
    ON indicators (ticker, indicator_type, time DESC);

CREATE INDEX IF NOT EXISTS idx_indicators_timeframe 
    ON indicators (timeframe, time DESC);

-- GIN index for JSONB queries
CREATE INDEX IF NOT EXISTS idx_indicators_metadata 
    ON indicators USING GIN (metadata);

-- Enable compression
ALTER TABLE indicators SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ticker,indicator_type,timeframe',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('indicators', INTERVAL '7 days');

-- Retention policy: keep 1 year
SELECT add_retention_policy('indicators', INTERVAL '1 year');

COMMENT ON TABLE indicators IS 'Technical indicators (SMA, EMA, MACD, RSI, etc.)';

-- =============================================================================
-- REGULAR TABLES (Reference Data)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 6. tickers - Ticker metadata
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickers (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT,
    locale TEXT,
    type TEXT,
    active BOOLEAN DEFAULT TRUE,
    currency TEXT,
    primary_exchange TEXT,
    sector TEXT,
    industry TEXT,
    market_cap BIGINT,
    last_updated TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers (active);
CREATE INDEX IF NOT EXISTS idx_tickers_sector ON tickers (sector);
CREATE INDEX IF NOT EXISTS idx_tickers_exchange ON tickers (primary_exchange);
CREATE INDEX IF NOT EXISTS idx_tickers_type ON tickers (type);

COMMENT ON TABLE tickers IS 'Ticker reference data and metadata';

-- -----------------------------------------------------------------------------
-- 7. exchanges - Exchange reference
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exchanges (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mic TEXT, -- Market Identifier Code
    operating_mic TEXT,
    participant_id TEXT,
    type TEXT,
    url TEXT,
    last_updated TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE exchanges IS 'Stock exchange reference data';

-- =============================================================================
-- CONTINUOUS AGGREGATES (Auto-Updating Materialized Views)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Daily OHLCV aggregated from minute data
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS daily_market_data
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time) AS day,
    ticker,
    FIRST(open, time) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, time) AS close,
    SUM(volume) AS volume,
    AVG(vwap) AS vwap,
    SUM(transactions) AS transactions
FROM market_data
WHERE timeframe = '1min'
GROUP BY day, ticker
WITH NO DATA;

-- Create index on continuous aggregate
CREATE INDEX IF NOT EXISTS idx_daily_market_data_ticker_day 
    ON daily_market_data (ticker, day DESC);

-- Refresh policy: update every hour
SELECT add_continuous_aggregate_policy('daily_market_data',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);

COMMENT ON MATERIALIZED VIEW daily_market_data IS 'Daily OHLCV auto-aggregated from minute data';

-- -----------------------------------------------------------------------------
-- Hourly OHLCV aggregated from minute data
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_market_data
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS hour,
    ticker,
    FIRST(open, time) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, time) AS close,
    SUM(volume) AS volume,
    AVG(vwap) AS vwap,
    SUM(transactions) AS transactions
FROM market_data
WHERE timeframe = '1min'
GROUP BY hour, ticker
WITH NO DATA;

-- Create index on continuous aggregate
CREATE INDEX IF NOT EXISTS idx_hourly_market_data_ticker_hour 
    ON hourly_market_data (ticker, hour DESC);

-- Refresh policy: update every 15 minutes
SELECT add_continuous_aggregate_policy('hourly_market_data',
    start_offset => INTERVAL '7 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '15 minutes'
);

COMMENT ON MATERIALIZED VIEW hourly_market_data IS 'Hourly OHLCV auto-aggregated from minute data';

-- =============================================================================
-- HELPER FUNCTIONS
-- =============================================================================

-- Function to get the latest price for a ticker
CREATE OR REPLACE FUNCTION get_latest_price(p_ticker TEXT)
RETURNS DOUBLE PRECISION AS $$
    SELECT close
    FROM market_data
    WHERE ticker = p_ticker AND timeframe = '1min'
    ORDER BY time DESC
    LIMIT 1;
$$ LANGUAGE SQL STABLE;

COMMENT ON FUNCTION get_latest_price IS 'Get the most recent closing price for a ticker';

-- Function to calculate simple returns
CREATE OR REPLACE FUNCTION calculate_returns(
    p_ticker TEXT,
    p_timeframe TEXT DEFAULT '1day',
    p_periods INT DEFAULT 1
)
RETURNS TABLE(
    time TIMESTAMPTZ,
    close DOUBLE PRECISION,
    returns DOUBLE PRECISION
) AS $$
    SELECT 
        time,
        close,
        (close / LAG(close, p_periods) OVER (ORDER BY time) - 1) AS returns
    FROM market_data
    WHERE ticker = p_ticker AND timeframe = p_timeframe
    ORDER BY time DESC;
$$ LANGUAGE SQL STABLE;

COMMENT ON FUNCTION calculate_returns IS 'Calculate period-over-period returns for a ticker';

-- =============================================================================
-- GRANT PERMISSIONS (for application user)
-- =============================================================================

-- Create application user if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_user WHERE usename = 'chronox_app') THEN
        CREATE USER chronox_app WITH PASSWORD 'chronox_secure_password';
    END IF;
END
$$;

-- Grant permissions
GRANT USAGE ON SCHEMA public TO chronox_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO chronox_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO chronox_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO chronox_app;

-- Grant permissions on future objects
ALTER DEFAULT PRIVILEGES IN SCHEMA public 
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO chronox_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public 
    GRANT EXECUTE ON FUNCTIONS TO chronox_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public 
    GRANT USAGE, SELECT ON SEQUENCES TO chronox_app;

-- =============================================================================
-- SAMPLE QUERIES (for testing)
-- =============================================================================

-- Query 1: Get latest OHLCV for a ticker
-- SELECT * FROM market_data WHERE ticker = 'AAPL' AND timeframe = '1min' ORDER BY time DESC LIMIT 10;

-- Query 2: Get daily bars from continuous aggregate
-- SELECT * FROM daily_market_data WHERE ticker = 'AAPL' ORDER BY day DESC LIMIT 30;

-- Query 3: Join market data with dividends
-- SELECT 
--     m.time, m.ticker, m.close, ca.amount AS dividend
-- FROM market_data m
-- LEFT JOIN corporate_actions ca 
--     ON m.ticker = ca.ticker 
--     AND ca.action_type = 'dividend'
--     AND DATE(m.time) = ca.ex_date
-- WHERE m.ticker = 'AAPL' AND m.timeframe = '1day'
-- ORDER BY m.time DESC
-- LIMIT 100;

-- Query 4: Calculate 20-day returns using window function
-- SELECT 
--     time, ticker, close,
--     close / LAG(close, 20) OVER (PARTITION BY ticker ORDER BY time) - 1 AS returns_20d
-- FROM market_data
-- WHERE ticker = 'AAPL' AND timeframe = '1day'
-- ORDER BY time DESC
-- LIMIT 30;

-- Query 5: Get latest snapshot for all tickers
-- SELECT DISTINCT ON (ticker)
--     ticker, time, last_trade_price, volume
-- FROM snapshots
-- ORDER BY ticker, time DESC;

-- Query 6: Search news by keyword
-- SELECT * FROM news WHERE 'earnings' = ANY(keywords) ORDER BY time DESC LIMIT 10;

-- Query 7: Get tickers in a specific sector
-- SELECT * FROM tickers WHERE sector = 'Technology' AND active = TRUE ORDER BY market_cap DESC;

-- Query 8: Compression status
-- SELECT * FROM timescaledb_information.chunks WHERE hypertable_name = 'market_data' ORDER BY chunk_name;

-- Query 9: Database size
-- SELECT 
--     hypertable_name,
--     pg_size_pretty(hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)::regclass)) AS size
-- FROM timescaledb_information.hypertables
-- ORDER BY hypertable_name;

COMMIT;

-- Print success message
\echo 'TimescaleDB schema initialized successfully!'
\echo 'Tables created: market_data, corporate_actions, news, snapshots, indicators, tickers, exchanges'
\echo 'Continuous aggregates: daily_market_data, hourly_market_data'
\echo 'Compression policies: enabled on all hypertables'
\echo 'Retention policies: configured based on data type'
