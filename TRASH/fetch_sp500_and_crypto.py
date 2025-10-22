#!/usr/bin/env python3
"""
Fetch S&P 500 stocks + specific stocks + crypto data
5 years of 1-minute and daily data
"""

import os
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any
import psycopg2
from psycopg2.extras import execute_batch

# Add project root to path
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.ingestion.polygon.client import PolygonClient
from data.ingestion.polygon.aggregates import AggregatesClient

def get_sp500_symbols() -> List[str]:
    """
    Get S&P 500 stock symbols.
    Using a curated list of the current S&P 500 companies.
    """
    # Major S&P 500 stocks - top companies by market cap and liquidity
    # Full list would be 500 stocks, but we'll use the most liquid ones for better data quality
    sp500_symbols = [
        # Mega cap tech (Top 10)
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK.B", "V", "UNH",
        
        # Large cap tech & communication
        "AVGO", "ORCL", "CSCO", "ADBE", "CRM", "NFLX", "AMD", "INTC", "QCOM", "TXN",
        "AMAT", "MU", "LRCX", "KLAC", "SNPS", "CDNS", "MRVL", "FTNT", "PANW", "CRWD",
        
        # Financial services
        "JPM", "BAC", "WFC", "MS", "GS", "C", "BLK", "SCHW", "AXP", "SPGI",
        "CME", "ICE", "MCO", "AON", "MMC", "PGR", "TRV", "ALL", "AIG", "MET",
        
        # Healthcare
        "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "DHR", "PFE", "BMY", "AMGN",
        "GILD", "VRTX", "CVS", "CI", "ELV", "HUM", "UNH", "ISRG", "SYK", "BSX",
        
        # Consumer discretionary
        "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TJX", "LOW", "BKNG", "CMG",
        "MAR", "GM", "F", "ABNB", "EBAY", "ETSY", "DPZ", "YUM", "ROST", "ULTA",
        
        # Consumer staples
        "PG", "KO", "PEP", "COST", "WMT", "PM", "MDLZ", "MO", "CL", "KMB",
        "GIS", "K", "HSY", "CPB", "CAG", "SJM", "HRL", "MKC", "TSN", "KHC",
        
        # Energy
        "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "HAL",
        "BKR", "WMB", "KMI", "OKE", "LNG", "FANG", "DVN", "HES", "MRO", "APA",
        
        # Industrials
        "BA", "CAT", "GE", "UPS", "RTX", "HON", "UNP", "LMT", "DE", "MMM",
        "ETN", "ITW", "EMR", "GD", "NOC", "TDG", "CARR", "PCAR", "NSC", "CSX",
        
        # Materials
        "LIN", "APD", "SHW", "ECL", "DD", "NEM", "FCX", "NUE", "VMC", "MLM",
        
        # Real Estate
        "AMT", "PLD", "CCI", "EQIX", "PSA", "WELL", "DLR", "O", "SBAC", "AVB",
        
        # Utilities
        "NEE", "DUK", "SO", "D", "AEP", "SRE", "EXC", "XEL", "ED", "WEC",
        
        # Additional requested stocks
        "BBAI",  # BigBear.ai (already in list but ensuring it's there)
    ]
    
    # Remove duplicates and sort
    return sorted(list(set(sp500_symbols)))

def get_crypto_symbols() -> List[str]:
    """Get cryptocurrency pairs to fetch."""
    return [
        "X:BTCUSD",   # Bitcoin
        "X:ETHUSD",   # Ethereum
        "X:XRPUSD",   # Ripple
        "X:SOLUSD",   # Solana
        "X:ADAUSD",   # Cardano
    ]

def fetch_data_for_symbol(
    ticker: str,
    start_date: str,
    end_date: str,
    agg_client: AggregatesClient,
    conn: Any,
    is_crypto: bool = False
) -> Dict[str, int]:
    """
    Fetch daily and 1-minute data for a single symbol.
    
    Returns:
        dict: {'daily': count, 'minute': count}
    """
    print(f"\n{'='*70}")
    print(f"📊 Fetching {ticker} ({'CRYPTO' if is_crypto else 'STOCK'})")
    print(f"{'='*70}")
    
    results = {'daily': 0, 'minute': 0}
    
    # Define insert query once
    insert_query = """
        INSERT INTO market_data (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ticker, timeframe, time) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            vwap = EXCLUDED.vwap,
            transactions = EXCLUDED.transactions;
    """
    
    try:
        # 1. Fetch daily bars
        print(f"\n1️⃣  Fetching daily bars...")
        daily_bars = agg_client.get_daily_bars(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date
        )
        
        if daily_bars and 'results' in daily_bars:
            bars = daily_bars['results']
            print(f"   Retrieved {len(bars)} daily bars from API")
            
            # Insert into database
            cursor = conn.cursor()
            
            data_to_insert = []
            for bar in bars:
                timestamp = datetime.fromtimestamp(bar['t'] / 1000.0)
                data_to_insert.append((
                    timestamp,
                    ticker,
                    'daily',
                    bar.get('o'),
                    bar.get('h'),
                    bar.get('l'),
                    bar.get('c'),
                    bar.get('v', 0),
                    bar.get('vw'),
                    bar.get('n', 0)
                ))
            
            if data_to_insert:
                execute_batch(cursor, insert_query, data_to_insert, page_size=1000)
                conn.commit()
                results['daily'] = len(data_to_insert)
                print(f"   ✅ Inserted {len(data_to_insert)} daily bars")
            
            cursor.close()
        
        # 2. Fetch 1-minute bars (this will take longer due to volume)
        print(f"\n2️⃣  Fetching 1-minute bars (this may take several minutes)...")
        minute_bars = agg_client.get_minute_bars(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date
        )
        
        if minute_bars and 'results' in minute_bars:
            bars = minute_bars['results']
            print(f"   Retrieved {len(bars)} 1-minute bars from API")
            
            # Insert into database in batches
            cursor = conn.cursor()
            data_to_insert = []
            
            for bar in bars:
                timestamp = datetime.fromtimestamp(bar['t'] / 1000.0)
                data_to_insert.append((
                    timestamp,
                    ticker,
                    '1min',
                    bar.get('o'),
                    bar.get('h'),
                    bar.get('l'),
                    bar.get('c'),
                    bar.get('v', 0),
                    bar.get('vw'),
                    bar.get('n', 0)
                ))
            
            if data_to_insert:
                # Insert in batches of 5000 for better performance
                batch_size = 5000
                total_batches = (len(data_to_insert) + batch_size - 1) // batch_size
                
                for i in range(0, len(data_to_insert), batch_size):
                    batch = data_to_insert[i:i + batch_size]
                    execute_batch(cursor, insert_query, batch, page_size=1000)
                    conn.commit()
                    batch_num = (i // batch_size) + 1
                    print(f"   Progress: Batch {batch_num}/{total_batches} ({len(batch)} bars)")
                
                results['minute'] = len(data_to_insert)
                print(f"   ✅ Inserted {len(data_to_insert):,} 1-minute bars")
            
            cursor.close()
        
        print(f"\n✅ {ticker} complete: {results['daily']} daily, {results['minute']:,} minute bars")
        
    except Exception as e:
        print(f"\n❌ Error fetching {ticker}: {e}")
        import traceback
        traceback.print_exc()
    
    return results

def main():
    """Main fetch process."""
    print("\n" + "="*70)
    print("  S&P 500 + Crypto Data Fetch")
    print("  5 Years of Daily + 1-Minute Data")
    print("="*70)
    
    # Get API key
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        print("❌ POLYGON_API_KEY not set in environment")
        return
    
    # Initialize clients
    polygon_client = PolygonClient(api_key)
    agg_client = AggregatesClient(polygon_client)
    
    # Database connection
    host = os.getenv("TIMESCALE_HOST", "localhost")
    port = os.getenv("TIMESCALE_PORT", "5433")
    user = os.getenv("TIMESCALE_USER", "postgres")
    password = os.getenv("TIMESCALE_PASSWORD", "chronox_db_password")
    database = os.getenv("TIMESCALE_DB", "chronox")
    
    conn = psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database
    )
    
    # Date range: 5 years
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5*365)
    
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    
    print(f"\n📅 Date Range: {start_str} to {end_str}")
    
    # Get all symbols
    stock_symbols = get_sp500_symbols()
    crypto_symbols = get_crypto_symbols()
    
    print(f"\n📈 Stocks to fetch: {len(stock_symbols)}")
    print(f"   Top 10: {', '.join(stock_symbols[:10])}")
    print(f"   ... and {len(stock_symbols) - 10} more")
    
    print(f"\n💰 Crypto to fetch: {len(crypto_symbols)}")
    print(f"   {', '.join([s.replace('X:', '') for s in crypto_symbols])}")
    
    print(f"\n⚠️  WARNING: This will fetch ~{len(stock_symbols) + len(crypto_symbols)} symbols")
    print(f"   Estimated time: 10-20 hours (due to API rate limits)")
    print(f"   Estimated data: ~50-100 GB")
    
    # Ask for confirmation
    response = input("\n Continue? (yes/no): ").strip().lower()
    if response != 'yes':
        print("❌ Fetch cancelled")
        return
    
    # Track results
    total_results = {'stocks': 0, 'crypto': 0, 'daily': 0, 'minute': 0}
    start_time = time.time()
    
    # Fetch stocks
    print("\n" + "="*70)
    print("  FETCHING STOCKS")
    print("="*70)
    
    for i, symbol in enumerate(stock_symbols, 1):
        print(f"\n[{i}/{len(stock_symbols)}] Processing {symbol}...")
        
        results = fetch_data_for_symbol(
            ticker=symbol,
            start_date=start_str,
            end_date=end_str,
            agg_client=agg_client,
            conn=conn,
            is_crypto=False
        )
        
        total_results['stocks'] += 1
        total_results['daily'] += results['daily']
        total_results['minute'] += results['minute']
        
        # Rate limiting: pause between symbols
        if i < len(stock_symbols):
            print(f"\n⏸️  Pausing 2 seconds (API rate limit)...")
            time.sleep(2)
    
    # Fetch crypto
    print("\n" + "="*70)
    print("  FETCHING CRYPTO")
    print("="*70)
    
    for i, symbol in enumerate(crypto_symbols, 1):
        print(f"\n[{i}/{len(crypto_symbols)}] Processing {symbol}...")
        
        results = fetch_data_for_symbol(
            ticker=symbol,
            start_date=start_str,
            end_date=end_str,
            agg_client=agg_client,
            conn=conn,
            is_crypto=True
        )
        
        total_results['crypto'] += 1
        total_results['daily'] += results['daily']
        total_results['minute'] += results['minute']
        
        # Rate limiting
        if i < len(crypto_symbols):
            print(f"\n⏸️  Pausing 2 seconds (API rate limit)...")
            time.sleep(2)
    
    # Close connection
    conn.close()
    
    # Print summary
    elapsed = time.time() - start_time
    print("\n" + "="*70)
    print("  FETCH COMPLETE")
    print("="*70)
    print(f"\n✅ Total stocks fetched: {total_results['stocks']}")
    print(f"✅ Total crypto fetched: {total_results['crypto']}")
    print(f"✅ Total daily bars: {total_results['daily']:,}")
    print(f"✅ Total 1-minute bars: {total_results['minute']:,}")
    print(f"\n⏱️  Total time: {elapsed/3600:.1f} hours")
    print("\n" + "="*70 + "\n")

if __name__ == "__main__":
    main()
