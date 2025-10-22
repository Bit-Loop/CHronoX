#!/usr/bin/env python3
"""
Fetch Historical Market Data for ChronoX

This script fetches 5 years of 1-minute bar data for specified stocks and crypto.
Data is stored in TimescaleDB for efficient time-series processing.

Symbols to fetch:
- Stocks: S&P 500 stocks + NVDA, INTC, AMD, BBAI
- Crypto: BTC-USD, ETH-USD, XRP-USD, SOL-USD, ADA-USD

Time range: Last 5 years
Interval: 1-minute bars
"""

import os
import sys
import asyncio
from datetime import datetime, timedelta
from typing import List
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.ingestion.polygon import PolygonClient, AggregatesClient
from dotenv import load_dotenv
import psycopg2
from psycopg2 import sql

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('data_fetch.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# Define symbols to fetch
STOCKS = [
    # High-priority tech stocks
    'NVDA', 'INTC', 'AMD', 'BBAI',
    
    # Major S&P 500 stocks (top 50 by market cap)
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'TSLA', 'BRK.B', 'V', 'UNH', 'JNJ',
    'JPM', 'WMT', 'PG', 'MA', 'HD', 'CVX', 'LLY', 'ABBV', 'MRK', 'KO',
    'PEP', 'AVGO', 'COST', 'ADBE', 'TMO', 'MCD', 'CSCO', 'ACN', 'ABT', 'NKE',
    'DHR', 'QCOM', 'TXN', 'VZ', 'NEE', 'COP', 'PM', 'BMY', 'UNP', 'RTX',
    'HON', 'SBUX', 'LOW', 'UPS', 'LMT', 'INTU', 'AMT', 'SPGI', 'GS', 'AXP'
]

CRYPTO = [
    'BTC-USD',   # Bitcoin
    'ETH-USD',   # Ethereum  
    'XRP-USD',   # Ripple
    'SOL-USD',   # Solana
    'ADA-USD',   # Cardano
]


class DataFetcher:
    """Fetches and stores historical market data."""
    
    def __init__(self):
        """Initialize data fetcher with Polygon client and database connections."""
        api_key = os.getenv('POLYGON_API_KEY')
        if not api_key:
            raise ValueError("POLYGON_API_KEY not found in environment variables")
        
        polygon_client = PolygonClient(api_key=api_key)
        self.aggregates = AggregatesClient(polygon_client)
        
        # Database connection params
        self.db_params = {
            'host': os.getenv('TIMESCALE_HOST', 'localhost'),
            'port': int(os.getenv('TIMESCALE_PORT', 5433)),
            'database': os.getenv('TIMESCALE_DB', 'chronox_timeseries'),
            'user': os.getenv('TIMESCALE_USER', 'chronox'),
            'password': os.getenv('TIMESCALE_PASSWORD', 'chronox_db_pass_2024')
        }
        
        # Calculate date range (5 years back)
        self.end_date = datetime.now()
        self.start_date = self.end_date - timedelta(days=5*365)
        
        logger.info(f"Date range: {self.start_date.date()} to {self.end_date.date()}")
        logger.info(f"Stocks to fetch: {len(STOCKS)}")
        logger.info(f"Crypto to fetch: {len(CRYPTO)}")
    
    def connect_db(self):
        """Create database connection."""
        try:
            conn = psycopg2.connect(**self.db_params)
            logger.info("Connected to TimescaleDB")
            return conn
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise
    
    def store_bars(self, conn, symbol: str, bars: List[dict], is_crypto: bool = False):
        """
        Store OHLCV bars in TimescaleDB.
        
        Args:
            conn: Database connection
            symbol: Ticker symbol
            bars: List of bar dictionaries
            is_crypto: Whether this is crypto data
        """
        if not bars:
            logger.warning(f"No bars to store for {symbol}")
            return
        
        table = 'market_data'
        
        cursor = conn.cursor()
        
        try:
            # Determine timeframe based on bar frequency
            # For now, we'll infer it or use a default
            timeframe = 'daily'  # Will be updated to support multiple timeframes
            
            # Prepare insert query
            insert_query = """
                INSERT INTO market_data (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (time, ticker, timeframe) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    vwap = EXCLUDED.vwap,
                    transactions = EXCLUDED.transactions
            """
            
            # Prepare data for bulk insert
            data = []
            for bar in bars:
                # Convert epoch milliseconds to timestamp
                timestamp = datetime.fromtimestamp(bar['t'] / 1000)
                
                data.append((
                    timestamp,
                    symbol,
                    timeframe,  # Add timeframe
                    bar.get('o'),
                    bar.get('h'),
                    bar.get('l'),
                    bar.get('c'),
                    bar.get('v'),
                    bar.get('vw'),
                    bar.get('n')
                ))
            
            # Bulk insert
            cursor.executemany(insert_query, data)
            conn.commit()
            
            logger.info(f"Stored {len(bars)} bars for {symbol} ({timeframe})")
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to store bars for {symbol}: {e}")
            raise
        finally:
            cursor.close()
    
    def fetch_stock_data(self, conn, symbol: str):
        """
        Fetch 5 years of 1-minute stock data.
        
        Args:
            conn: Database connection
            symbol: Stock ticker
        """
        logger.info(f"Fetching stock data for {symbol}...")
        
        # Convert dates to string format
        start_str = self.start_date.strftime('%Y-%m-%d')
        end_str = self.end_date.strftime('%Y-%m-%d')
        
        try:
            # Fetch 1-minute bars
            # Note: Polygon has limits, so we'll chunk this into smaller date ranges
            # For now, fetch daily first, then can add minute bars
            
            logger.info(f"Fetching daily bars for {symbol}...")
            daily_bars = self.aggregates.get_daily_bars(
                ticker=symbol,
                start_date=start_str,
                end_date=end_str,
                adjusted=True
            )
            
            if daily_bars:
                self.store_bars(conn, symbol, daily_bars, is_crypto=False)
                logger.info(f"✅ Completed {symbol}: {len(daily_bars)} daily bars")
            else:
                logger.warning(f"⚠️ No data for {symbol}")
            
            # For 1-minute bars, we need to chunk by date ranges (API limits)
            # Polygon allows max 50,000 bars per request
            # 1 minute bars: ~390 bars per trading day, so ~77 days per chunk
            
            logger.info(f"Fetching 1-minute bars for {symbol} (this may take a while)...")
            
            # Split into 2-month chunks
            chunk_days = 60
            current_start = self.start_date
            total_minute_bars = 0
            
            while current_start < self.end_date:
                current_end = min(current_start + timedelta(days=chunk_days), self.end_date)
                
                chunk_start_str = current_start.strftime('%Y-%m-%d')
                chunk_end_str = current_end.strftime('%Y-%m-%d')
                
                logger.info(f"  Chunk: {chunk_start_str} to {chunk_end_str}")
                
                minute_bars = self.aggregates.get_minute_bars(
                    ticker=symbol,
                    start_date=chunk_start_str,
                    end_date=chunk_end_str,
                    adjusted=True,
                    limit=50000,
                    multiplier=1
                )
                
                if minute_bars:
                    self.store_bars(conn, symbol, minute_bars, is_crypto=False)
                    total_minute_bars += len(minute_bars)
                    logger.info(f"  Stored {len(minute_bars)} minute bars")
                
                current_start = current_end
            
            logger.info(f"✅ Completed {symbol}: {total_minute_bars} total minute bars")
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch {symbol}: {e}")
    
    def fetch_crypto_data(self, conn, symbol: str):
        """
        Fetch 5 years of 1-minute crypto data.
        
        Args:
            conn: Database connection
            symbol: Crypto ticker (e.g., 'X:BTCUSD')
        """
        # Polygon uses 'X:' prefix for crypto
        poly_symbol = f"X:{symbol.replace('-', '')}"
        
        logger.info(f"Fetching crypto data for {symbol} (Polygon: {poly_symbol})...")
        
        start_str = self.start_date.strftime('%Y-%m-%d')
        end_str = self.end_date.strftime('%Y-%m-%d')
        
        try:
            # Daily bars first
            logger.info(f"Fetching daily bars for {symbol}...")
            daily_bars = self.aggregates.get_daily_bars(
                ticker=poly_symbol,
                start_date=start_str,
                end_date=end_str,
                adjusted=True
            )
            
            if daily_bars:
                self.store_bars(conn, symbol, daily_bars, is_crypto=True)
                logger.info(f"✅ Completed {symbol}: {len(daily_bars)} daily bars")
            
            # 1-minute bars in chunks
            logger.info(f"Fetching 1-minute bars for {symbol}...")
            
            chunk_days = 60
            current_start = self.start_date
            total_minute_bars = 0
            
            while current_start < self.end_date:
                current_end = min(current_start + timedelta(days=chunk_days), self.end_date)
                
                chunk_start_str = current_start.strftime('%Y-%m-%d')
                chunk_end_str = current_end.strftime('%Y-%m-%d')
                
                logger.info(f"  Chunk: {chunk_start_str} to {chunk_end_str}")
                
                minute_bars = self.aggregates.get_minute_bars(
                    ticker=poly_symbol,
                    start_date=chunk_start_str,
                    end_date=chunk_end_str,
                    adjusted=True,
                    limit=50000,
                    multiplier=1
                )
                
                if minute_bars:
                    self.store_bars(conn, symbol, minute_bars, is_crypto=True)
                    total_minute_bars += len(minute_bars)
                    logger.info(f"  Stored {len(minute_bars)} minute bars")
                
                current_start = current_end
            
            logger.info(f"✅ Completed {symbol}: {total_minute_bars} total minute bars")
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch {symbol}: {e}")
    
    async def fetch_all(self):
        """Fetch all stock and crypto data."""
        logger.info("=" * 80)
        logger.info("ChronoX Data Fetcher Starting")
        logger.info("=" * 80)
        
        conn = self.connect_db()
        
        try:
            # Fetch stocks
            logger.info(f"\n📈 Fetching {len(STOCKS)} stocks...")
            for i, symbol in enumerate(STOCKS, 1):
                logger.info(f"\n[{i}/{len(STOCKS)}] Processing {symbol}")
                self.fetch_stock_data(conn, symbol)
            
            # Fetch crypto
            logger.info(f"\n💰 Fetching {len(CRYPTO)} cryptocurrencies...")
            for i, symbol in enumerate(CRYPTO, 1):
                logger.info(f"\n[{i}/{len(CRYPTO)}] Processing {symbol}")
                self.fetch_crypto_data(conn, symbol)
            
            logger.info("\n" + "=" * 80)
            logger.info("✅ Data fetch completed!")
            logger.info("=" * 80)
            
        finally:
            conn.close()
            logger.info("Database connection closed")


def main():
    """Main entry point."""
    try:
        fetcher = DataFetcher()
        asyncio.run(fetcher.fetch_all())
    except KeyboardInterrupt:
        logger.warning("\n⚠️ Fetch interrupted by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
