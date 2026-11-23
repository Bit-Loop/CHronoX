#!/usr/bin/env python3
"""
Fetch S&P 500 Historical Data Using Polygon.io Flat Files

This script downloads bulk historical data from Polygon's S3 flat files,
which is MUCH faster than making 50,000+ API calls per symbol.

Benefits of Flat Files:
- 100-1000x faster than REST API calls
- No rate limiting (direct S3 downloads)
- Complete historical data in compressed format
- Parallel downloads (6 concurrent)
- Resume capability if interrupted
"""

import os
import sys
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from data.ingestion.polygon.flat_files import FlatFileDownloader
from data.ingestion.polygon.flat_file_parser import FlatFileParser
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# S&P 500 stocks (top sectors for quick start)
# Full list: https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
SP500_STOCKS = [
    # Technology (20 stocks)
    'AAPL', 'MSFT', 'NVDA', 'GOOGL', 'GOOG', 'AMZN', 'META', 'TSLA', 'AVGO', 'ORCL',
    'ADBE', 'CRM', 'AMD', 'CSCO', 'ACN', 'INTC', 'TXN', 'QCOM', 'NOW', 'IBM',
    
    # Financial Services (15 stocks)
    'JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'BLK', 'SPGI', 'AXP', 'USB',
    'PNC', 'TFC', 'COF', 'BK', 'STT',
    
    # Healthcare (15 stocks)
    'UNH', 'JNJ', 'LLY', 'ABBV', 'MRK', 'TMO', 'ABT', 'DHR', 'PFE', 'BMY',
    'AMGN', 'GILD', 'CVS', 'CI', 'ISRG',
    
    # Consumer Discretionary (15 stocks)
    'AMZN', 'TSLA', 'HD', 'MCD', 'NKE', 'SBUX', 'TJX', 'BKNG', 'LOW', 'MAR',
    'F', 'GM', 'ABNB', 'DG', 'ROST',
    
    # Communication Services (10 stocks)
    'META', 'GOOGL', 'GOOG', 'NFLX', 'DIS', 'CMCSA', 'VZ', 'T', 'TMUS', 'CHTR',
    
    # Industrials (15 stocks)
    'BA', 'CAT', 'UPS', 'HON', 'RTX', 'GE', 'MMM', 'LMT', 'DE', 'UNP',
    'FDX', 'NSC', 'EMR', 'ETN', 'ITW',
    
    # Consumer Staples (10 stocks)
    'WMT', 'PG', 'KO', 'PEP', 'COST', 'PM', 'MO', 'CL', 'MDLZ', 'KHC',
    
    # Energy (10 stocks)
    'XOM', 'CVX', 'COP', 'SLB', 'EOG', 'MPC', 'PSX', 'VLO', 'OXY', 'HAL',
    
    # Utilities (5 stocks)
    'NEE', 'DUK', 'SO', 'D', 'AEP',
    
    # Real Estate (5 stocks)
    'AMT', 'PLD', 'CCI', 'EQIX', 'PSA',
    
    # Materials (5 stocks)
    'LIN', 'APD', 'SHW', 'ECL', 'DD',
    
    # AI/Tech Focus (additional)
    'PLTR', 'BBAI', 'AI', 'SMCI', 'ARM'
]

# Remove duplicates and sort
SP500_STOCKS = sorted(list(set(SP500_STOCKS)))

# Crypto pairs
CRYPTO_PAIRS = ['X:BTCUSD', 'X:ETHUSD', 'X:XRPUSD', 'X:SOLUSD', 'X:ADAUSD']


def generate_flat_file_urls(start_year: int = 2020, end_year: int = 2025) -> List[Dict[str, str]]:
    """
    Generate Polygon.io flat file URLs for minute-level data.
    
    Polygon's flat files structure:
    - Grouped by year
    - One file per trading day
    - Compressed CSV format
    - Includes all tickers for that day
    
    Args:
        start_year: Starting year
        end_year: Ending year (inclusive)
    
    Returns:
        List of dicts with 'url' and 'filename' keys
    """
    file_urls = []
    base_url = "https://flatfiles.polygon.io/us_stocks_sip/minute_aggs_v1"
    
    # Note: Actual Polygon flat files are organized by date
    # Format: us_stocks_sip/minute_aggs_v1/{year}/{month}/{year}-{month}-{day}.csv.gz
    
    # Generate URLs for each year
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # We'll need to query the Polygon API for available files
            # For now, create placeholder structure
            file_info = {
                'url': f"{base_url}/{year}/{month:02d}/",
                'year': year,
                'month': month
            }
            file_urls.append(file_info)
    
    return file_urls


async def list_available_flat_files(api_key: str, start_date: str, end_date: str) -> List[Dict[str, str]]:
    """
    Query Polygon.io API to list available flat files.
    
    Args:
        api_key: Polygon.io API key
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    
    Returns:
        List of available flat file URLs
    """
    import aiohttp
    
    # Polygon Flat Files API endpoint
    # https://api.polygon.io/v1/flatfiles
    url = "https://api.polygon.io/v1/flatfiles"
    
    params = {
        'apiKey': api_key,
        'ticker.gte': 'A',  # All tickers starting from A
        'ticker.lte': 'Z',  # All tickers up to Z
        'date.gte': start_date,
        'date.lte': end_date,
        'limit': 1000
    }
    
    file_list = []
    
    try:
        async with aiohttp.ClientSession() as session:
            logger.info(f"Querying Polygon for flat files from {start_date} to {end_date}...")
            
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if 'results' in data:
                        for file_info in data['results']:
                            file_list.append({
                                'url': file_info.get('download_url'),
                                'filename': file_info.get('file_name'),
                                'date': file_info.get('date'),
                                'size': file_info.get('size')
                            })
                        
                        logger.info(f"Found {len(file_list)} flat files available")
                    else:
                        logger.warning("No flat files found in response")
                else:
                    logger.error(f"Failed to query flat files: HTTP {response.status}")
    
    except Exception as e:
        logger.error(f"Error querying flat files: {str(e)}")
    
    return file_list


async def main():
    """Main execution function."""
    
    print("\n" + "="*70)
    print("  POLYGON.IO FLAT FILE DOWNLOADER")
    print("  S&P 500 Stocks + Crypto Historical Data")
    print("="*70)
    
    # Get API key
    api_key = os.getenv('POLYGON_API_KEY')
    if not api_key:
        logger.error("POLYGON_API_KEY not found in environment variables")
        sys.exit(1)
    
    print(f"\n📊 Stocks to fetch: {len(SP500_STOCKS)} symbols")
    print(f"🪙 Crypto pairs: {len(CRYPTO_PAIRS)} pairs")
    print(f"\n⚠️  NOTE: Polygon.io flat files require a paid subscription plan")
    print(f"   Free tier users should use the regular API fetch script instead.")
    
    # Ask for confirmation
    print(f"\nThis will:")
    print(f"  1. Query Polygon.io for available flat files (2020-2025)")
    print(f"  2. Download compressed CSV files (~5-10 GB total)")
    print(f"  3. Extract and parse data for {len(SP500_STOCKS)} stocks")
    print(f"  4. Insert into TimescaleDB")
    
    response = input("\nProceed? (yes/no): ").strip().lower()
    if response not in ['yes', 'y']:
        print("❌ Cancelled by user")
        sys.exit(0)
    
    # Initialize downloader
    download_dir = "./data/flat_files"
    downloader = FlatFileDownloader(api_key=api_key, download_dir=download_dir)
    
    # Date range
    start_date = "2020-01-01"
    end_date = datetime.now().strftime("%Y-%m-%d")
    
    print(f"\n📡 Querying available flat files...")
    
    # Get available files
    file_list = await list_available_flat_files(api_key, start_date, end_date)
    
    if not file_list:
        print("\n⚠️  No flat files found!")
        print("\nPossible reasons:")
        print("  1. Your Polygon.io plan doesn't include flat file access")
        print("  2. Flat files API endpoint changed")
        print("  3. Date range has no data")
        print("\n💡 Suggestion: Use fetch_sp500_and_crypto.py instead (REST API)")
        sys.exit(1)
    
    print(f"\n✅ Found {len(file_list)} files to download")
    
    # Download files
    print(f"\n⬇️  Downloading flat files...")
    downloaded_files = await downloader.download_batch(file_list, skip_existing=True)
    
    print(f"\n✅ Downloaded {len(downloaded_files)} files")
    
    # Parse and insert data
    if downloaded_files:
        print(f"\n📦 Parsing downloaded files...")
        
        parser = FlatFileParser()
        
        # TODO: Implement parsing logic
        # - Extract CSV from gzip
        # - Filter for our stock symbols
        # - Convert to TimescaleDB format
        # - Bulk insert using COPY
        
        print(f"\n⚠️  Parsing implementation pending")
        print(f"   Files are ready at: {download_dir}")
    
    print("\n" + "="*70)
    print("  DOWNLOAD COMPLETE")
    print("="*70)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n❌ Interrupted by user (Ctrl+C)")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}", exc_info=True)
        sys.exit(1)
