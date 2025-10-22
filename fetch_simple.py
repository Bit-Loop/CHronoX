#!/usr/bin/env python3
"""
Simplified Data Fetcher for ChronoX

Fetches the specific stocks and crypto requested by the user.
"""

import os
import sys
from datetime import datetime, timedelta
import time
import logging
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data.ingestion.polygon.aggregates import AggregatesClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Symbols to fetch
STOCKS = ['NVDA', 'INTC', 'AMD', 'BBAI']
CRYPTO = ['BTCUSD', 'ETHUSD', 'XRPUSD', 'SOLUSD', 'ADAUSD']  # Polygon format

# Date range: Last 5 years
END_DATE = datetime.now()
START_DATE = END_DATE - timedelta(days=5*365)

# Database params
DB_PARAMS = {
    'host': os.getenv('TIMESCALE_HOST', 'localhost'),
    'port': int(os.getenv('TIMESCALE_PORT', 5433)),
    'database': os.getenv('TIMESCALE_DB', 'chronox'),
    'user': os.getenv('TIMESCALE_USER', 'postgres'),
    'password': os.getenv('TIMESCALE_PASSWORD', 'chronox_db_password')
}


def fetch_daily_bars(api_client, ticker, start_date, end_date):
    """Fetch daily bars for entire period"""
    logger.info(f"Fetching daily bars for {ticker}")
    
    bars = api_client.get_daily_bars(
        ticker=ticker,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        adjusted=True
    )
    
    logger.info(f"  Got {len(bars)} daily bars")
    return bars


def fetch_minute_bars(api_client, ticker, start_date, end_date):
    """Fetch 1-minute bars in monthly chunks"""
    all_bars = []
    current = start_date
    
    logger.info(f"Fetching 1-minute bars for {ticker} (this may take a while...)")
    
    while current < end_date:
        chunk_end = min(current + timedelta(days=30), end_date)
        
        logger.info(f"  Chunk: {current.date()} to {chunk_end.date()}")
        
        try:
            bars = api_client.get_minute_bars(
                ticker=ticker,
                start_date=current.strftime('%Y-%m-%d'),
                end_date=chunk_end.strftime('%Y-%m-%d'),
                adjusted=True
            )
            
            if bars:
                all_bars.extend(bars)
                logger.info(f"    Got {len(bars)} bars (total: {len(all_bars)})")
            
            time.sleep(0.3)  # Rate limiting
            
        except Exception as e:
            logger.error(f"    Error: {e}")
        
        current = chunk_end + timedelta(days=1)
    
    logger.info(f"  Total 1-minute bars: {len(all_bars)}")
    return all_bars


def insert_bars(conn, ticker, timeframe, bars):
    """Insert bars into database"""
    if not bars:
        return
    
    values = []
    for bar in bars:
        timestamp = datetime.fromtimestamp(bar['t'] / 1000)
        values.append((
            timestamp,
            ticker,
            timeframe,
            bar['o'],
            bar['h'],
            bar['l'],
            bar['c'],
            bar['v'],
            bar.get('vw'),
            bar.get('n')
        ))
    
    cursor = conn.cursor()
    
    try:
        execute_values(
            cursor,
            """
            INSERT INTO market_data 
            (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
            VALUES %s
            ON CONFLICT (time, ticker, timeframe) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                vwap = EXCLUDED.vwap,
                transactions = EXCLUDED.transactions
            """,
            values
        )
        conn.commit()
        logger.info(f"✅ Inserted {len(values)} bars for {ticker} {timeframe}")
        
    except Exception as e:
        logger.error(f"❌ Error inserting: {e}")
        conn.rollback()
    finally:
        cursor.close()


def main():
    # Initialize API client
    api_key = os.getenv('POLYGON_API_KEY')
    if not api_key:
        logger.error("POLYGON_API_KEY not set")
        return
    
    from data.ingestion.polygon.client import PolygonClient
    polygon_client = PolygonClient(api_key=api_key)
    api_client = AggregatesClient(polygon_client=polygon_client)
    
    # Connect to database
    conn = psycopg2.connect(**DB_PARAMS)
    
    logger.info("=" * 80)
    logger.info("ChronoX Simple Data Fetcher")
    logger.info("=" * 80)
    logger.info(f"Date range: {START_DATE.date()} to {END_DATE.date()}")
    logger.info(f"Stocks: {STOCKS}")
    logger.info(f"Crypto: {CRYPTO}")
    logger.info("=" * 80)
    
    # Fetch stocks
    logger.info("\n📈 FETCHING STOCKS")
    for ticker in STOCKS:
        logger.info(f"\n[{ticker}]")
        
        # Daily bars
        daily_bars = fetch_daily_bars(api_client, ticker, START_DATE, END_DATE)
        if daily_bars:
            insert_bars(conn, ticker, 'daily', daily_bars)
        time.sleep(0.3)
        
        # 1-minute bars
        minute_bars = fetch_minute_bars(api_client, ticker, START_DATE, END_DATE)
        if minute_bars:
            insert_bars(conn, ticker, '1min', minute_bars)
    
    # Fetch crypto
    logger.info("\n\n₿  FETCHING CRYPTO")
    for ticker in CRYPTO:
        polygon_ticker = f"X:{ticker}"
        logger.info(f"\n[{ticker}]")
        
        # Daily bars
        daily_bars = fetch_daily_bars(api_client, polygon_ticker, START_DATE, END_DATE)
        if daily_bars:
            insert_bars(conn, ticker, 'daily', daily_bars)
        time.sleep(0.3)
        
        # 1-minute bars  
        minute_bars = fetch_minute_bars(api_client, polygon_ticker, START_DATE, END_DATE)
        if minute_bars:
            insert_bars(conn, ticker, '1min', minute_bars)
    
    conn.close()
    
    logger.info("\n" + "=" * 80)
    logger.info("✅ DATA FETCH COMPLETE!")
    logger.info("=" * 80)


if __name__ == '__main__':
    main()
