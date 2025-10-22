-- TimescaleDB Initialization Script for ChronoX
-- Creates hypertables for time-series market data

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Create schema for market data
CREATE SCHEMA IF NOT EXISTS market_data;
CREATE SCHEMA IF NOT EXISTS model_metrics;

-- =============================================================================
-- MARKET DATA TABLES
-- =============================================================================

-- OHLCV data table
CREATE TABLE IF NOT EXISTS market_data.ohlcv (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(20) NOT NULL,
    open DECIMAL(20, 8) NOT NULL,
    high DECIMAL(20, 8) NOT NULL,
    low DECIMAL(20, 8) NOT NULL,
    close DECIMAL(20, 8) NOT NULL,
    volume DECIMAL(20, 8) NOT NULL,
    quote_volume DECIMAL(20, 8),
    trades INTEGER,
    PRIMARY KEY (time, symbol, exchange)
);

-- Convert to hypertable (partitioned by time)
SELECT create_hypertable(
    'market_data.ohlcv',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Create indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_time 
    ON market_data.ohlcv (symbol, time DESC);
CREATE INDEX IF NOT EXISTS idx_ohlcv_exchange 
    ON market_data.ohlcv (exchange, time DESC);

-- Order book snapshot table
CREATE TABLE IF NOT EXISTS market_data.orderbook (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(20) NOT NULL,
    side VARCHAR(4) NOT NULL, -- 'bid' or 'ask'
    price DECIMAL(20, 8) NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    level INTEGER NOT NULL, -- order book level (0 = best, 1 = second best, etc.)
    PRIMARY KEY (time, symbol, exchange, side, level)
);

SELECT create_hypertable(
    'market_data.orderbook',
    'time',
    chunk_time_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_orderbook_symbol_time 
    ON market_data.orderbook (symbol, time DESC);

-- Trades table
CREATE TABLE IF NOT EXISTS market_data.trades (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(20) NOT NULL,
    trade_id VARCHAR(50),
    price DECIMAL(20, 8) NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    side VARCHAR(4), -- 'buy' or 'sell'
    is_buyer_maker BOOLEAN,
    PRIMARY KEY (time, symbol, exchange, trade_id)
);

SELECT create_hypertable(
    'market_data.trades',
    'time',
    chunk_time_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol_time 
    ON market_data.trades (symbol, time DESC);

-- Technical indicators table
CREATE TABLE IF NOT EXISTS market_data.indicators (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(20) NOT NULL,
    indicator_name VARCHAR(50) NOT NULL,
    value DECIMAL(20, 8) NOT NULL,
    metadata JSONB,
    PRIMARY KEY (time, symbol, exchange, indicator_name)
);

SELECT create_hypertable(
    'market_data.indicators',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_indicators_symbol_time 
    ON market_data.indicators (symbol, time DESC);
CREATE INDEX IF NOT EXISTS idx_indicators_name 
    ON market_data.indicators (indicator_name, time DESC);

-- Alternative data table (news, social sentiment, etc.)
CREATE TABLE IF NOT EXISTS market_data.alternative_data (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20),
    source VARCHAR(50) NOT NULL, -- 'news', 'twitter', 'reddit', etc.
    data_type VARCHAR(50) NOT NULL, -- 'sentiment', 'article', 'post', etc.
    content TEXT,
    sentiment_score DECIMAL(5, 4),
    metadata JSONB,
    PRIMARY KEY (time, source, data_type)
);

SELECT create_hypertable(
    'market_data.alternative_data',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_alt_data_symbol_time 
    ON market_data.alternative_data (symbol, time DESC) WHERE symbol IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_alt_data_source 
    ON market_data.alternative_data (source, time DESC);

-- =============================================================================
-- MODEL METRICS TABLES
-- =============================================================================

-- Model predictions table
CREATE TABLE IF NOT EXISTS model_metrics.predictions (
    time TIMESTAMPTZ NOT NULL,
    model_name VARCHAR(50) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    prediction_type VARCHAR(20) NOT NULL, -- 'price', 'direction', 'volatility', etc.
    prediction_value DECIMAL(20, 8) NOT NULL,
    confidence DECIMAL(5, 4),
    actual_value DECIMAL(20, 8),
    metadata JSONB,
    PRIMARY KEY (time, model_name, symbol, prediction_type)
);

SELECT create_hypertable(
    'model_metrics.predictions',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_predictions_model_time 
    ON model_metrics.predictions (model_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_symbol 
    ON model_metrics.predictions (symbol, time DESC);

-- Model performance metrics
CREATE TABLE IF NOT EXISTS model_metrics.performance (
    time TIMESTAMPTZ NOT NULL,
    model_name VARCHAR(50) NOT NULL,
    metric_name VARCHAR(50) NOT NULL, -- 'accuracy', 'mse', 'sharpe', etc.
    metric_value DECIMAL(20, 8) NOT NULL,
    window_size VARCHAR(20), -- '1h', '1d', '7d', etc.
    metadata JSONB,
    PRIMARY KEY (time, model_name, metric_name)
);

SELECT create_hypertable(
    'model_metrics.performance',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_performance_model_time 
    ON model_metrics.performance (model_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_performance_metric 
    ON model_metrics.performance (metric_name, time DESC);

-- Training runs table
CREATE TABLE IF NOT EXISTS model_metrics.training_runs (
    time TIMESTAMPTZ NOT NULL,
    model_name VARCHAR(50) NOT NULL,
    run_id VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL, -- 'started', 'completed', 'failed'
    duration_seconds INTEGER,
    final_loss DECIMAL(20, 8),
    final_accuracy DECIMAL(5, 4),
    hyperparameters JSONB,
    metadata JSONB,
    PRIMARY KEY (time, model_name, run_id)
);

SELECT create_hypertable(
    'model_metrics.training_runs',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_training_model_time 
    ON model_metrics.training_runs (model_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_training_status 
    ON model_metrics.training_runs (status, time DESC);

-- =============================================================================
-- CONTINUOUS AGGREGATES (Materialized Views)
-- =============================================================================

-- 5-minute OHLCV aggregates
CREATE MATERIALIZED VIEW IF NOT EXISTS market_data.ohlcv_5m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', time) AS bucket,
    symbol,
    exchange,
    FIRST(open, time) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, time) AS close,
    SUM(volume) AS volume,
    SUM(quote_volume) AS quote_volume,
    SUM(trades) AS trades
FROM market_data.ohlcv
GROUP BY bucket, symbol, exchange
WITH NO DATA;

-- Refresh policy: every 1 minute, aggregate last 1 hour of data
SELECT add_continuous_aggregate_policy('market_data.ohlcv_5m',
    start_offset => INTERVAL '1 hour',
    end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE
);

-- Hourly OHLCV aggregates
CREATE MATERIALIZED VIEW IF NOT EXISTS market_data.ohlcv_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    symbol,
    exchange,
    FIRST(open, time) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, time) AS close,
    SUM(volume) AS volume,
    SUM(quote_volume) AS quote_volume,
    SUM(trades) AS trades
FROM market_data.ohlcv
GROUP BY bucket, symbol, exchange
WITH NO DATA;

SELECT add_continuous_aggregate_policy('market_data.ohlcv_1h',
    start_offset => INTERVAL '1 day',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- Daily OHLCV aggregates
CREATE MATERIALIZED VIEW IF NOT EXISTS market_data.ohlcv_1d
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time) AS bucket,
    symbol,
    exchange,
    FIRST(open, time) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, time) AS close,
    SUM(volume) AS volume,
    SUM(quote_volume) AS quote_volume,
    SUM(trades) AS trades
FROM market_data.ohlcv
GROUP BY bucket, symbol, exchange
WITH NO DATA;

SELECT add_continuous_aggregate_policy('market_data.ohlcv_1d',
    start_offset => INTERVAL '7 days',
    end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Average sentiment by symbol and hour
CREATE MATERIALIZED VIEW IF NOT EXISTS market_data.sentiment_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    symbol,
    source,
    AVG(sentiment_score) AS avg_sentiment,
    COUNT(*) AS num_items,
    STDDEV(sentiment_score) AS sentiment_stddev
FROM market_data.alternative_data
WHERE sentiment_score IS NOT NULL AND symbol IS NOT NULL
GROUP BY bucket, symbol, source
WITH NO DATA;

SELECT add_continuous_aggregate_policy('market_data.sentiment_hourly',
    start_offset => INTERVAL '1 day',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- =============================================================================
-- RETENTION POLICIES
-- =============================================================================

-- Keep raw 1-minute OHLCV data for 90 days
SELECT add_retention_policy('market_data.ohlcv', INTERVAL '90 days', if_not_exists => TRUE);

-- Keep order book snapshots for 30 days
SELECT add_retention_policy('market_data.orderbook', INTERVAL '30 days', if_not_exists => TRUE);

-- Keep trades for 60 days
SELECT add_retention_policy('market_data.trades', INTERVAL '60 days', if_not_exists => TRUE);

-- Keep alternative data for 180 days
SELECT add_retention_policy('market_data.alternative_data', INTERVAL '180 days', if_not_exists => TRUE);

-- Keep model predictions for 365 days
SELECT add_retention_policy('model_metrics.predictions', INTERVAL '365 days', if_not_exists => TRUE);

-- =============================================================================
-- COMPRESSION POLICIES (reduces storage by 90%+)
-- =============================================================================

-- Compress OHLCV data older than 7 days
ALTER TABLE market_data.ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, exchange',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('market_data.ohlcv', INTERVAL '7 days', if_not_exists => TRUE);

-- Compress trades older than 3 days
ALTER TABLE market_data.trades SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, exchange',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('market_data.trades', INTERVAL '3 days', if_not_exists => TRUE);

-- Compress model predictions older than 30 days
ALTER TABLE model_metrics.predictions SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'model_name, symbol',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('model_metrics.predictions', INTERVAL '30 days', if_not_exists => TRUE);

-- =============================================================================
-- GRANT PERMISSIONS
-- =============================================================================

GRANT USAGE ON SCHEMA market_data TO chronox;
GRANT USAGE ON SCHEMA model_metrics TO chronox;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA market_data TO chronox;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA model_metrics TO chronox;

GRANT SELECT ON ALL TABLES IN SCHEMA _timescaledb_catalog TO chronox;
GRANT SELECT ON ALL TABLES IN SCHEMA _timescaledb_internal TO chronox;

-- =============================================================================
-- HELPER FUNCTIONS
-- =============================================================================

-- Function to get latest price for a symbol
CREATE OR REPLACE FUNCTION market_data.get_latest_price(
    p_symbol VARCHAR(20),
    p_exchange VARCHAR(20) DEFAULT NULL
)
RETURNS DECIMAL(20, 8) AS $$
DECLARE
    latest_price DECIMAL(20, 8);
BEGIN
    SELECT close INTO latest_price
    FROM market_data.ohlcv
    WHERE symbol = p_symbol
        AND (p_exchange IS NULL OR exchange = p_exchange)
    ORDER BY time DESC
    LIMIT 1;
    
    RETURN latest_price;
END;
$$ LANGUAGE plpgsql;

-- Function to calculate returns
CREATE OR REPLACE FUNCTION market_data.calculate_returns(
    p_symbol VARCHAR(20),
    p_exchange VARCHAR(20),
    p_start_time TIMESTAMPTZ,
    p_end_time TIMESTAMPTZ
)
RETURNS TABLE (
    time TIMESTAMPTZ,
    close_price DECIMAL(20, 8),
    log_return DECIMAL(20, 8),
    simple_return DECIMAL(20, 8)
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        o.time,
        o.close,
        LN(o.close / LAG(o.close) OVER (ORDER BY o.time)) AS log_return,
        (o.close - LAG(o.close) OVER (ORDER BY o.time)) / LAG(o.close) OVER (ORDER BY o.time) AS simple_return
    FROM market_data.ohlcv o
    WHERE o.symbol = p_symbol
        AND o.exchange = p_exchange
        AND o.time >= p_start_time
        AND o.time <= p_end_time
    ORDER BY o.time;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- INITIAL DATA QUALITY CHECKS
-- =============================================================================

-- Create view for data quality monitoring
CREATE OR REPLACE VIEW market_data.data_quality AS
SELECT
    symbol,
    exchange,
    DATE(time) AS date,
    COUNT(*) AS num_records,
    COUNT(DISTINCT time) AS unique_timestamps,
    MIN(time) AS first_timestamp,
    MAX(time) AS last_timestamp,
    AVG(volume) AS avg_volume,
    SUM(CASE WHEN close <= 0 THEN 1 ELSE 0 END) AS invalid_prices
FROM market_data.ohlcv
GROUP BY symbol, exchange, DATE(time)
ORDER BY date DESC, symbol;

-- Grant permissions
GRANT SELECT ON market_data.data_quality TO chronox;

COMMENT ON DATABASE chronox_timeseries IS 'ChronoX TimescaleDB - Time-series market data and model metrics';
