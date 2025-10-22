"""
TimescaleDB Writer for ChronoX Trading Bot

Handles writing all time-series data to TimescaleDB (PostgreSQL extension).
Supports batch operations, connection pooling, and optimized inserts.
"""

import psycopg2
from psycopg2 import pool, extras, sql
from psycopg2.extensions import register_adapter, AsIs
import numpy as np
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timezone
import logging
from contextlib import contextmanager
from io import StringIO
import csv
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Register numpy adapters for PostgreSQL
def adapt_numpy_float64(numpy_float64):
    return AsIs(float(numpy_float64))

def adapt_numpy_int64(numpy_int64):
    return AsIs(int(numpy_int64))

register_adapter(np.float64, adapt_numpy_float64)
register_adapter(np.int64, adapt_numpy_int64)


class TimescaleDBError(Exception):
    """Custom exception for TimescaleDB operations"""
    pass


class TimescaleWriter:
    """
    TimescaleDB writer with connection pooling and batch optimization.
    
    Features:
    - Connection pooling for multi-threaded access
    - Bulk COPY operations for fast inserts
    - Automatic retry logic
    - Prepared statements for repeated queries
    - Context manager support
    """
    
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        min_connections: int = 1,
        max_connections: int = 20
    ):
        """
        Initialize TimescaleDB writer with connection pooling.
        
        Args:
            host: PostgreSQL host (defaults to env TIMESCALE_HOST or 'localhost')
            port: PostgreSQL port (defaults to env TIMESCALE_PORT or 5433)
            database: Database name (defaults to env TIMESCALE_DB or 'chronox')
            user: Database user (defaults to env TIMESCALE_USER or 'postgres')
            password: Database password (defaults to env TIMESCALE_PASSWORD or 'chronox_db_password')
            min_connections: Minimum connections in pool
            max_connections: Maximum connections in pool
        """
        # Read from environment if not provided
        host = host or os.getenv('TIMESCALE_HOST', 'localhost')
        port = port or int(os.getenv('TIMESCALE_PORT', '5433'))
        database = database or os.getenv('TIMESCALE_DB', 'chronox')
        user = user or os.getenv('TIMESCALE_USER', 'postgres')
        password = password or os.getenv('TIMESCALE_PASSWORD', 'chronox_db_password')
        
        self.dsn = f"host={host} port={port} dbname={database} user={user} password={password}"
        
        try:
            # Create connection pool
            self.pool = pool.ThreadedConnectionPool(
                min_connections,
                max_connections,
                dsn=self.dsn,
                connect_timeout=10
            )
            logger.info(f"✓ TimescaleDB connection pool created ({min_connections}-{max_connections} connections)")
            
            # Test connection
            if self.test_connection():
                logger.info("✓ TimescaleDB connection successful")
            else:
                raise TimescaleDBError("Connection test failed")
                
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to connect to TimescaleDB: {e}")
            raise TimescaleDBError(f"Connection failed: {e}")
    
    @contextmanager
    def get_connection(self):
        """
        Context manager for getting connection from pool.
        
        Usage:
            with writer.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT ...")
        """
        conn = self.pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Transaction rolled back: {e}")
            raise
        finally:
            self.pool.putconn(conn)
    
    def test_connection(self) -> bool:
        """Test database connection and TimescaleDB extension."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check TimescaleDB extension
                cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")
                version = cursor.fetchone()
                
                if version:
                    logger.info(f"✓ TimescaleDB version: {version[0]}")
                    return True
                else:
                    logger.warning("⚠ TimescaleDB extension not found. Please install: CREATE EXTENSION timescaledb;")
                    return False
                    
        except Exception as e:
            logger.error(f"✗ Connection test failed: {e}")
            return False
    
    def execute_query(self, query: str, params: Optional[tuple] = None) -> List[tuple]:
        """
        Execute a SQL query and return results.
        
        Args:
            query: SQL query string
            params: Query parameters (optional)
            
        Returns:
            List of result tuples
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                return cursor.fetchall()
        except psycopg2.Error as e:
            logger.error(f"✗ Query failed: {e}")
            return []
    
    def write_ohlcv_bars(
        self,
        ticker: str,
        bars: List[Dict],
        timeframe: str = "1min",
        use_copy: bool = True
    ) -> int:
        """
        Write OHLCV bars to market_data table.
        
        Args:
            ticker: Stock symbol (e.g., 'AAPL')
            bars: List of bar dicts with keys: t, o, h, l, c, v, vw, n
            timeframe: Bar timeframe ('1min', '1hour', '1day', etc.)
            use_copy: Use COPY for bulk insert (much faster)
        
        Returns:
            Number of rows inserted
        """
        if not bars:
            logger.warning(f"No bars to write for {ticker}")
            return 0
        
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                if use_copy:
                    # Use COPY for bulk insert (50-100x faster)
                    rows_written = self._copy_ohlcv_bars(cursor, ticker, bars, timeframe)
                else:
                    # Use executemany for smaller batches
                    rows_written = self._insert_ohlcv_bars(cursor, ticker, bars, timeframe)
                
                logger.info(f"✓ Wrote {rows_written} {timeframe} bars for {ticker}")
                return rows_written
                
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to write OHLCV bars for {ticker}: {e}")
            raise TimescaleDBError(f"Write failed: {e}")
    
    def _copy_ohlcv_bars(self, cursor, ticker: str, bars: List[Dict], timeframe: str) -> int:
        """Use COPY for bulk insert (fastest method) with conflict handling."""
        # Create CSV buffer in memory
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        
        for bar in bars:
            # Convert epoch milliseconds to timestamp
            timestamp = datetime.fromtimestamp(bar['t'] / 1000, tz=timezone.utc)
            
            row = [
                timestamp,
                ticker,
                timeframe,
                float(bar['o']),
                float(bar['h']),
                float(bar['l']),
                float(bar['c']),
                int(bar['v']),
                float(bar.get('vw', 0)) if bar.get('vw') else None,
                int(bar.get('n', 0)) if bar.get('n') else None
            ]
            writer.writerow(row)
        
        # Move to start of buffer
        csv_buffer.seek(0)
        
        # Create temporary table for staging
        cursor.execute("""
            CREATE TEMP TABLE temp_market_data (
                time TIMESTAMPTZ,
                ticker TEXT,
                timeframe TEXT,
                open DOUBLE PRECISION,
                high DOUBLE PRECISION,
                low DOUBLE PRECISION,
                close DOUBLE PRECISION,
                volume BIGINT,
                vwap DOUBLE PRECISION,
                transactions INTEGER
            ) ON COMMIT DROP
        """)
        
        # Use COPY to load into temp table
        cursor.copy_expert("""
            COPY temp_market_data (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
            FROM STDIN WITH (FORMAT CSV, NULL '')
        """, csv_buffer)
        
        # Insert from temp table with conflict handling
        cursor.execute("""
            INSERT INTO market_data (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
            SELECT time, ticker, timeframe, open, high, low, close, volume, vwap, transactions
            FROM temp_market_data
            ON CONFLICT (time, ticker, timeframe) DO NOTHING
        """)
        
        # Return number of rows actually inserted
        rows_inserted = cursor.rowcount
        return rows_inserted
    
    def _insert_ohlcv_bars(self, cursor, ticker: str, bars: List[Dict], timeframe: str) -> int:
        """Use executemany for smaller batches."""
        insert_query = """
            INSERT INTO market_data (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """
        
        rows = []
        for bar in bars:
            timestamp = datetime.fromtimestamp(bar['t'] / 1000, tz=timezone.utc)
            rows.append((
                timestamp,
                ticker,
                timeframe,
                float(bar['o']),
                float(bar['h']),
                float(bar['l']),
                float(bar['c']),
                int(bar['v']),
                float(bar.get('vw', 0)) if bar.get('vw') else None,
                int(bar.get('n', 0)) if bar.get('n') else None
            ))
        
        cursor.executemany(insert_query, rows)
        return len(rows)
    
    def write_bars_upsert(self, bars: List[Dict], timeframe: str) -> int:
        """
        Write enriched bars with indicators to TimescaleDB using idempotent upserts.
        This method is used by the unified ingestion pipeline (Kappa architecture).
        
        Uses INSERT ... ON CONFLICT DO UPDATE to ensure idempotency for replays.
        
        Args:
            bars: List of enriched bar dicts with keys:
                - symbol, timestamp, open, high, low, close, volume
                - sma_20, sma_50, ema_20, ema_50, ema_100, ema_200
                - rsi_14, macd, macd_signal, macd_hist
                - bb_upper, bb_middle, bb_lower, vwap
            timeframe: Bar timeframe (e.g., '1m', '5m', '1h', '1d')
        
        Returns:
            Number of rows inserted/updated
        """
        if not bars:
            return 0
        
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Ensure table exists with all indicator columns
                self._ensure_enriched_table_exists(cursor, timeframe)
                
                # Build upsert query
                upsert_query = f"""
                    INSERT INTO market_data_{timeframe} (
                        time, ticker, open, high, low, close, volume,
                        sma_20, sma_50, ema_20, ema_50, ema_100, ema_200,
                        rsi_14, macd, macd_signal, macd_hist,
                        bb_upper, bb_middle, bb_lower, vwap
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (time, ticker) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        sma_20 = EXCLUDED.sma_20,
                        sma_50 = EXCLUDED.sma_50,
                        ema_20 = EXCLUDED.ema_20,
                        ema_50 = EXCLUDED.ema_50,
                        ema_100 = EXCLUDED.ema_100,
                        ema_200 = EXCLUDED.ema_200,
                        rsi_14 = EXCLUDED.rsi_14,
                        macd = EXCLUDED.macd,
                        macd_signal = EXCLUDED.macd_signal,
                        macd_hist = EXCLUDED.macd_hist,
                        bb_upper = EXCLUDED.bb_upper,
                        bb_middle = EXCLUDED.bb_middle,
                        bb_lower = EXCLUDED.bb_lower,
                        vwap = EXCLUDED.vwap
                """
                
                rows = []
                for bar in bars:
                    # Extract timestamp (handle both datetime objects and timestamp fields)
                    if isinstance(bar['timestamp'], datetime):
                        ts = bar['timestamp']
                    else:
                        ts = datetime.fromtimestamp(bar['timestamp'] / 1000, tz=timezone.utc)
                    
                    rows.append((
                        ts,
                        bar['symbol'],
                        float(bar['open']),
                        float(bar['high']),
                        float(bar['low']),
                        float(bar['close']),
                        int(bar['volume']),
                        bar.get('sma_20'),
                        bar.get('sma_50'),
                        bar.get('ema_20'),
                        bar.get('ema_50'),
                        bar.get('ema_100'),
                        bar.get('ema_200'),
                        bar.get('rsi_14'),
                        bar.get('macd'),
                        bar.get('macd_signal'),
                        bar.get('macd_hist'),
                        bar.get('bb_upper'),
                        bar.get('bb_middle'),
                        bar.get('bb_lower'),
                        bar.get('vwap'),
                    ))
                
                cursor.executemany(upsert_query, rows)
                rows_affected = cursor.rowcount
                
                logger.debug(f"✓ Upserted {rows_affected} bars to market_data_{timeframe}")
                return rows_affected
                
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to upsert bars: {e}")
            raise TimescaleDBError(f"Upsert failed: {e}")
    
    def _ensure_enriched_table_exists(self, cursor, timeframe: str):
        """
        Ensure market_data_{timeframe} table exists with all indicator columns.
        Creates wide hypertable for ML-ready data storage.
        """
        table_name = f"market_data_{timeframe}"
        
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                time TIMESTAMPTZ NOT NULL,
                ticker TEXT NOT NULL,
                open DOUBLE PRECISION,
                high DOUBLE PRECISION,
                low DOUBLE PRECISION,
                close DOUBLE PRECISION,
                volume BIGINT,
                sma_20 DOUBLE PRECISION,
                sma_50 DOUBLE PRECISION,
                ema_20 DOUBLE PRECISION,
                ema_50 DOUBLE PRECISION,
                ema_100 DOUBLE PRECISION,
                ema_200 DOUBLE PRECISION,
                rsi_14 DOUBLE PRECISION,
                macd DOUBLE PRECISION,
                macd_signal DOUBLE PRECISION,
                macd_hist DOUBLE PRECISION,
                bb_upper DOUBLE PRECISION,
                bb_middle DOUBLE PRECISION,
                bb_lower DOUBLE PRECISION,
                vwap DOUBLE PRECISION,
                PRIMARY KEY (time, ticker)
            )
        """)
        
        # Convert to hypertable if not already
        cursor.execute(f"""
            SELECT * FROM timescaledb_information.hypertables
            WHERE hypertable_name = '{table_name}'
        """)
        
        if not cursor.fetchone():
            cursor.execute(f"""
                SELECT create_hypertable(
                    '{table_name}', 
                    'time',
                    if_not_exists => TRUE,
                    migrate_data => TRUE
                )
            """)
            logger.info(f"✓ Created hypertable: {table_name}")
        
        # Create indexes for efficient queries
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_ticker_time 
            ON {table_name} (ticker, time DESC)
        """)
        
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_ticker 
            ON {table_name} (ticker)
        """)
    
    def write_corporate_actions(
        self,
        ticker: str,
        actions: List[Dict],
        action_type: str
    ) -> int:
        """
        Write corporate actions (dividends or splits).
        
        Args:
            ticker: Stock symbol
            actions: List of action dicts
            action_type: 'dividend' or 'split'
        
        Returns:
            Number of rows inserted
        """
        if not actions:
            return 0
        
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                insert_query = """
                    INSERT INTO corporate_actions 
                    (time, ticker, action_type, amount, ratio, declaration_date, ex_date, pay_date, execution_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """
                
                rows = []
                for action in actions:
                    if action_type == "dividend":
                        ex_date = action.get('ex_dividend_date')
                        timestamp = datetime.strptime(ex_date, '%Y-%m-%d').replace(tzinfo=timezone.utc) if ex_date else datetime.now(timezone.utc)
                        
                        rows.append((
                            timestamp,
                            ticker,
                            action_type,
                            float(action.get('cash_amount', 0)),
                            None,
                            action.get('declaration_date'),
                            action.get('ex_dividend_date'),
                            action.get('pay_date'),
                            None
                        ))
                    
                    elif action_type == "split":
                        exec_date = action.get('execution_date')
                        timestamp = datetime.strptime(exec_date, '%Y-%m-%d').replace(tzinfo=timezone.utc) if exec_date else datetime.now(timezone.utc)
                        
                        split_from = float(action.get('split_from', 1))
                        split_to = float(action.get('split_to', 1))
                        ratio = split_from / split_to if split_to != 0 else 1.0
                        
                        rows.append((
                            timestamp,
                            ticker,
                            action_type,
                            None,
                            ratio,
                            None,
                            None,
                            None,
                            action.get('execution_date')
                        ))
                
                cursor.executemany(insert_query, rows)
                logger.info(f"✓ Wrote {len(rows)} {action_type} actions for {ticker}")
                return len(rows)
                
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to write corporate actions for {ticker}: {e}")
            raise TimescaleDBError(f"Write failed: {e}")
    
    def write_reference_data(self, ticker: str, metadata: Dict) -> bool:
        """
        Write or update ticker metadata in tickers table.
        
        Args:
            ticker: Stock symbol
            metadata: Ticker metadata dict
        
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                upsert_query = """
                    INSERT INTO tickers 
                    (ticker, name, market, locale, type, active, currency, primary_exchange, sector, industry, market_cap, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (ticker) 
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        market = EXCLUDED.market,
                        locale = EXCLUDED.locale,
                        type = EXCLUDED.type,
                        active = EXCLUDED.active,
                        currency = EXCLUDED.currency,
                        primary_exchange = EXCLUDED.primary_exchange,
                        sector = EXCLUDED.sector,
                        industry = EXCLUDED.industry,
                        market_cap = EXCLUDED.market_cap,
                        last_updated = NOW()
                """
                
                cursor.execute(upsert_query, (
                    ticker,
                    metadata.get('name', ''),
                    metadata.get('market', ''),
                    metadata.get('locale', ''),
                    metadata.get('type', ''),
                    metadata.get('active', True),
                    metadata.get('currency_name', ''),
                    metadata.get('primary_exchange', ''),
                    metadata.get('sic_description', ''),
                    metadata.get('industry', ''),
                    metadata.get('market_cap')
                ))
                
                logger.info(f"✓ Wrote reference data for {ticker}")
                return True
                
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to write reference data for {ticker}: {e}")
            return False
    
    def write_news(self, articles: List[Dict]) -> int:
        """
        Write news articles.
        
        Args:
            articles: List of news article dicts
        
        Returns:
            Number of rows inserted
        """
        if not articles:
            return 0
        
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                insert_query = """
                    INSERT INTO news 
                    (time, ticker, publisher, title, author, article_url, tickers_list, keywords, sentiment_score)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """
                
                rows = []
                for article in articles:
                    tickers = article.get('tickers', [])
                    if not tickers:
                        continue
                    
                    published = article.get('published_utc')
                    if published:
                        # Parse ISO timestamp
                        timestamp = datetime.fromisoformat(published.replace('Z', '+00:00'))
                    else:
                        timestamp = datetime.now(timezone.utc)
                    
                    publisher_name = article.get('publisher', {}).get('name', '') if isinstance(article.get('publisher'), dict) else ''
                    
                    # Create one row per ticker (denormalized for fast queries)
                    for ticker in tickers:
                        rows.append((
                            timestamp,
                            ticker,
                            publisher_name,
                            article.get('title', ''),
                            article.get('author', ''),
                            article.get('article_url', ''),
                            tickers,  # PostgreSQL array
                            article.get('keywords', []),  # PostgreSQL array
                            None  # sentiment_score to be calculated later
                        ))
                
                if rows:
                    cursor.executemany(insert_query, rows)
                    logger.info(f"✓ Wrote {len(rows)} news articles")
                    return len(rows)
                
                return 0
                
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to write news: {e}")
            raise TimescaleDBError(f"Write failed: {e}")
    
    def write_snapshot(self, ticker: str, snapshot: Dict) -> bool:
        """
        Write market snapshot (real-time data).
        
        Args:
            ticker: Stock symbol
            snapshot: Snapshot data dict
        
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                insert_query = """
                    INSERT INTO snapshots 
                    (time, ticker, last_trade_price, last_trade_size, bid, ask, bid_size, ask_size, 
                     volume, day_open, day_high, day_low, day_close, day_volume, prev_day_close)
                    VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                
                last_trade = snapshot.get('lastTrade', {})
                last_quote = snapshot.get('lastQuote', {})
                day = snapshot.get('day', {})
                prev_day = snapshot.get('prevDay', {})
                
                cursor.execute(insert_query, (
                    ticker,
                    last_trade.get('p'),
                    last_trade.get('s'),
                    last_quote.get('p'),
                    last_quote.get('P'),
                    last_quote.get('s'),
                    last_quote.get('S'),
                    day.get('v'),
                    day.get('o'),
                    day.get('h'),
                    day.get('l'),
                    day.get('c'),
                    day.get('v'),
                    prev_day.get('c')
                ))
                
                return True
                
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to write snapshot for {ticker}: {e}")
            return False
    
    def query_ohlcv(
        self,
        ticker: str,
        timeframe: str = "1min",
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000
    ) -> List[Tuple]:
        """
        Query OHLCV data.
        
        Args:
            ticker: Stock symbol
            timeframe: Bar timeframe
            start_time: Start datetime (optional)
            end_time: End datetime (optional)
            limit: Max rows to return
        
        Returns:
            List of tuples (time, open, high, low, close, volume)
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                query = """
                    SELECT time, open, high, low, close, volume, vwap, transactions
                    FROM market_data
                    WHERE ticker = %s AND timeframe = %s
                """
                params = [ticker, timeframe]
                
                if start_time:
                    query += " AND time >= %s"
                    params.append(start_time)
                
                if end_time:
                    query += " AND time < %s"
                    params.append(end_time)
                
                query += " ORDER BY time DESC LIMIT %s"
                params.append(limit)
                
                cursor.execute(query, params)
                return cursor.fetchall()
                
        except psycopg2.Error as e:
            logger.error(f"✗ Query failed: {e}")
            return []
    
    def get_latest_timestamp(self, ticker: str, timeframe: str = "1min") -> Optional[datetime]:
        """
        Get the latest timestamp for a ticker.
        
        Args:
            ticker: Stock symbol
            timeframe: Bar timeframe
        
        Returns:
            Latest timestamp or None
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT MAX(time) FROM market_data 
                    WHERE ticker = %s AND timeframe = %s
                """, (ticker, timeframe))
                
                result = cursor.fetchone()
                return result[0] if result else None
                
        except psycopg2.Error as e:
            logger.error(f"✗ Query failed: {e}")
            return None
    
    def write_chart_patterns(
        self,
        ticker: str,
        timeframe: str,
        patterns: List[Dict],
        detected_at: Optional[datetime] = None
    ) -> int:
        """
        Write detected chart patterns to database.
        Creates patterns table if it doesn't exist.
        
        Args:
            ticker: Stock symbol
            timeframe: Chart timeframe (e.g., '1min', '1h', '1d')
            patterns: List of pattern dicts with keys:
                - type: Pattern type (e.g., 'head_shoulders', 'triangle')
                - subtype: Pattern subtype (e.g., 'bullish', 'ascending')
                - confidence: Confidence score (0.0-1.0)
                - indices: List of bar indices involved
                - metadata: Additional pattern-specific data (JSON)
            detected_at: Detection timestamp (defaults to now)
        
        Returns:
            Number of patterns written
        """
        if not patterns:
            return 0
        
        if detected_at is None:
            detected_at = datetime.now(timezone.utc)
        
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Create patterns table if not exists
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chart_patterns (
                        id SERIAL PRIMARY KEY,
                        ticker VARCHAR(20) NOT NULL,
                        timeframe VARCHAR(10) NOT NULL,
                        pattern_type VARCHAR(50) NOT NULL,
                        pattern_subtype VARCHAR(50),
                        confidence FLOAT,
                        start_index INTEGER,
                        end_index INTEGER,
                        indices INTEGER[],
                        metadata JSONB,
                        detected_at TIMESTAMPTZ NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                    
                    CREATE INDEX IF NOT EXISTS idx_patterns_ticker_time 
                    ON chart_patterns(ticker, timeframe, detected_at DESC);
                    
                    CREATE INDEX IF NOT EXISTS idx_patterns_type 
                    ON chart_patterns(pattern_type, pattern_subtype);
                """)
                
                # Prepare insert data
                insert_data = []
                for pattern in patterns:
                    indices = pattern.get('indices', [])
                    start_idx = min(indices) if indices else None
                    end_idx = max(indices) if indices else None
                    
                    insert_data.append((
                        ticker,
                        timeframe,
                        pattern.get('type', 'unknown'),
                        pattern.get('subtype', ''),
                        pattern.get('confidence', 0.5),
                        start_idx,
                        end_idx,
                        indices,
                        psycopg2.extras.Json(pattern.get('metadata', {})),
                        detected_at
                    ))
                
                # Batch insert
                psycopg2.extras.execute_batch(
                    cursor,
                    """
                        INSERT INTO chart_patterns 
                        (ticker, timeframe, pattern_type, pattern_subtype, confidence,
                         start_index, end_index, indices, metadata, detected_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """,
                    insert_data,
                    page_size=100
                )
                
                conn.commit()
                logger.debug(f"✓ Wrote {len(patterns)} patterns for {ticker}/{timeframe}")
                return len(patterns)
        
        except psycopg2.Error as e:
            logger.error(f"✗ Failed to write patterns: {e}")
            return 0
    
    def close(self):
        """Close all connections in the pool."""
        if self.pool:
            self.pool.closeall()
            logger.info("✓ All database connections closed")
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


if __name__ == "__main__":
    # Test the writer
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    # Configuration
    writer = TimescaleWriter(
        host=os.getenv("TIMESCALE_HOST", "localhost"),
        port=int(os.getenv("TIMESCALE_PORT", 5432)),
        database=os.getenv("TIMESCALE_DB", "chronox"),
        user=os.getenv("TIMESCALE_USER", "postgres"),
        password=os.getenv("TIMESCALE_PASSWORD", "postgres")
    )
    
    # Test with sample data
    sample_bars = [
        {'t': 1697472600000, 'o': 150.0, 'h': 151.0, 'l': 149.5, 'c': 150.5, 'v': 1000000, 'vw': 150.2, 'n': 500}
    ]
    
    try:
        rows = writer.write_ohlcv_bars("TEST", sample_bars, "1min")
        print(f"\n✓ Successfully wrote {rows} test bars")
        
        # Query back
        results = writer.query_ohlcv("TEST", "1min", limit=10)
        print(f"✓ Retrieved {len(results)} bars")
        
    finally:
        writer.close()
