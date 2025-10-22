"""
Polygon.io Aggregates (OHLCV) and Snapshot API Client

Handles fetching historical and real-time bar data, plus market snapshots.
"""

from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class AggregatesClient:
    """
    Client for fetching OHLCV aggregate bars from Polygon.io.
    
    Supports minute, hour, day, week, month, quarter, and year bars.
    """
    
    def __init__(self, polygon_client):
        """
        Initialize aggregates client.
        
        Args:
            polygon_client: Instance of PolygonClient
        """
        self.client = polygon_client
    
    def get_minute_bars(self, ticker: str, start_date: str, end_date: str, 
                       adjusted: bool = True, limit: int = 50000,
                       multiplier: int = 1) -> List[Dict]:
        """
        Fetch minute-level OHLCV bars for a ticker.
        
        Args:
            ticker: Stock symbol (e.g., 'AAPL')
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            adjusted: Apply split/dividend adjustments
            limit: Max bars per request (max 50000)
            multiplier: Bar size multiplier (1=1min, 5=5min, etc.)
        
        Returns:
            List of bar dicts with keys:
                - t: timestamp (epoch milliseconds)
                - o: open price
                - h: high price
                - l: low price
                - c: close price
                - v: volume
                - vw: volume-weighted average price
                - n: number of transactions
        """
        endpoint = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/minute/{start_date}/{end_date}"
        params = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": limit
        }
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', [])
            
            if results:
                logger.info(f"Fetched {len(results)} minute bars for {ticker}")
            else:
                logger.warning(f"No minute bars found for {ticker} ({start_date} to {end_date})")
            
            return results
        except Exception as e:
            logger.error(f"Failed to fetch minute bars for {ticker}: {str(e)}")
            return []
    
    def get_daily_bars(self, ticker: str, start_date: str, end_date: str,
                      adjusted: bool = True, multiplier: int = 1) -> List[Dict]:
        """
        Fetch daily OHLCV bars for a ticker.
        
        Args:
            ticker: Stock symbol
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            adjusted: Apply split/dividend adjustments
            multiplier: Bar size multiplier (1=1day, 7=1week, etc.)
        
        Returns:
            List of daily bar dicts
        """
        endpoint = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/day/{start_date}/{end_date}"
        params = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": 50000
        }
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', [])
            
            if results:
                logger.info(f"Fetched {len(results)} daily bars for {ticker}")
            else:
                logger.warning(f"No daily bars found for {ticker} ({start_date} to {end_date})")
            
            return results
        except Exception as e:
            logger.error(f"Failed to fetch daily bars for {ticker}: {str(e)}")
            return []
    
    def get_hourly_bars(self, ticker: str, start_date: str, end_date: str,
                       adjusted: bool = True, multiplier: int = 1) -> List[Dict]:
        """
        Fetch hourly OHLCV bars for a ticker.
        
        Args:
            ticker: Stock symbol
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            adjusted: Apply split/dividend adjustments
            multiplier: Bar size multiplier (1=1hour, 4=4hour, etc.)
        
        Returns:
            List of hourly bar dicts
        """
        endpoint = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/hour/{start_date}/{end_date}"
        params = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": 50000
        }
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', [])
            
            if results:
                logger.info(f"Fetched {len(results)} hourly bars for {ticker}")
            else:
                logger.warning(f"No hourly bars found for {ticker}")
            
            return results
        except Exception as e:
            logger.error(f"Failed to fetch hourly bars for {ticker}: {str(e)}")
            return []
    
    def chunk_date_range(self, start_date: str, end_date: str, 
                        chunk_days: int = 7) -> List[Tuple[str, str]]:
        """
        Split large date range into smaller chunks to avoid timeouts and API limits.
        
        For minute data: Polygon.io returns max 50,000 bars per request.
        At ~390 trading minutes per day, 7 days = ~2,730 minutes (safe buffer).
        
        Args:
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            chunk_days: Days per chunk (7 recommended for minute data)
        
        Returns:
            List of (start_date, end_date) tuples
        """
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        chunks = []
        current = start
        
        while current < end:
            chunk_end = min(current + timedelta(days=chunk_days), end)
            chunks.append((
                current.strftime('%Y-%m-%d'),
                chunk_end.strftime('%Y-%m-%d')
            ))
            current = chunk_end + timedelta(days=1)
        
        logger.info(f"Split date range into {len(chunks)} chunks of ~{chunk_days} days")
        return chunks
    
    def get_minute_bars_chunked(self, ticker: str, start_date: str, end_date: str,
                                adjusted: bool = True, chunk_days: int = 7) -> List[Dict]:
        """
        Fetch minute bars for long date ranges by chunking requests.
        
        Args:
            ticker: Stock symbol
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            adjusted: Apply split/dividend adjustments
            chunk_days: Days per chunk (7 recommended)
        
        Returns:
            Combined list of all minute bars
        """
        chunks = self.chunk_date_range(start_date, end_date, chunk_days)
        all_bars = []
        
        for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
            logger.info(f"Fetching chunk {i}/{len(chunks)}: {chunk_start} to {chunk_end}")
            bars = self.get_minute_bars(ticker, chunk_start, chunk_end, adjusted)
            all_bars.extend(bars)
        
        logger.info(f"Total bars fetched for {ticker}: {len(all_bars)}")
        return all_bars


class SnapshotClient:
    """
    Client for fetching real-time market snapshots from Polygon.io.
    
    Note: Starter plan has ~15 minute delay on snapshot data.
    """
    
    def __init__(self, polygon_client):
        """
        Initialize snapshot client.
        
        Args:
            polygon_client: Instance of PolygonClient
        """
        self.client = polygon_client
    
    def get_ticker_snapshot(self, ticker: str) -> Optional[Dict]:
        """
        Get current snapshot for a single ticker.
        
        Includes:
            - day: Today's OHLCV data
            - lastTrade: Most recent trade details
            - lastQuote: Most recent bid/ask
            - min: Today's minute-level aggregate
            - prevDay: Previous day's OHLCV
        
        Args:
            ticker: Stock symbol
        
        Returns:
            Snapshot dict or None if error
        """
        endpoint = f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
        
        try:
            data = self.client._get(endpoint)
            
            if 'ticker' in data:
                snapshot = data['ticker']
                logger.info(f"Fetched snapshot for {ticker}")
                return snapshot
            else:
                logger.warning(f"No snapshot data for {ticker}")
                return None
        except Exception as e:
            logger.error(f"Failed to fetch snapshot for {ticker}: {str(e)}")
            return None
    
    def get_all_tickers_snapshot(self, tickers: Optional[List[str]] = None) -> List[Dict]:
        """
        Get snapshots for all tickers or a list of tickers.
        
        WARNING: Without ticker filter, this returns snapshots for ALL stocks
        (thousands of tickers). Use with caution.
        
        Args:
            tickers: Optional list of tickers to filter (comma-separated will be used)
        
        Returns:
            List of snapshot dicts
        """
        endpoint = "/v2/snapshot/locale/us/markets/stocks/tickers"
        params = {}
        
        if tickers:
            params['tickers'] = ','.join(tickers)
            logger.info(f"Fetching snapshots for {len(tickers)} tickers")
        else:
            logger.warning("Fetching ALL tickers snapshot - this may return a lot of data")
        
        try:
            data = self.client._get(endpoint, params)
            snapshots = data.get('tickers', [])
            logger.info(f"Fetched {len(snapshots)} snapshots")
            return snapshots
        except Exception as e:
            logger.error(f"Failed to fetch all tickers snapshot: {str(e)}")
            return []
    
    def get_gainers_losers(self, direction: str = "gainers") -> List[Dict]:
        """
        Get top gainers or losers for the day.
        
        Args:
            direction: 'gainers' or 'losers'
        
        Returns:
            List of ticker snapshots sorted by performance
        """
        if direction not in ['gainers', 'losers']:
            logger.error(f"Invalid direction: {direction}. Use 'gainers' or 'losers'")
            return []
        
        endpoint = f"/v2/snapshot/locale/us/markets/stocks/{direction}"
        
        try:
            data = self.client._get(endpoint)
            tickers = data.get('tickers', [])
            logger.info(f"Fetched {len(tickers)} {direction}")
            return tickers
        except Exception as e:
            logger.error(f"Failed to fetch {direction}: {str(e)}")
            return []


if __name__ == "__main__":
    # Test the aggregates client
    import os
    from dotenv import load_dotenv
    from client import PolygonClient
    
    load_dotenv()
    api_key = os.getenv("POLYGON_API_KEY")
    
    if not api_key:
        print("ERROR: POLYGON_API_KEY not found in .env file")
        exit(1)
    
    # Initialize clients
    client = PolygonClient(api_key)
    agg_client = AggregatesClient(client)
    snap_client = SnapshotClient(client)
    
    # Test daily bars
    print("\n=== Testing Daily Bars ===")
    daily_bars = agg_client.get_daily_bars("AAPL", "2024-01-01", "2024-12-31")
    print(f"Fetched {len(daily_bars)} daily bars for AAPL")
    if daily_bars:
        print(f"Sample bar: {daily_bars[0]}")
    
    # Test minute bars (small range)
    print("\n=== Testing Minute Bars ===")
    minute_bars = agg_client.get_minute_bars("AAPL", "2024-10-01", "2024-10-05")
    print(f"Fetched {len(minute_bars)} minute bars for AAPL")
    
    # Test snapshot
    print("\n=== Testing Snapshot ===")
    snapshot = snap_client.get_ticker_snapshot("AAPL")
    if snapshot:
        print(f"AAPL snapshot: Day O={snapshot.get('day', {}).get('o')} C={snapshot.get('day', {}).get('c')}")
    
    # Test gainers
    print("\n=== Testing Gainers ===")
    gainers = snap_client.get_gainers_losers("gainers")
    if gainers:
        print(f"Top gainer: {gainers[0].get('ticker')} ({gainers[0].get('todaysChangePerc', 0):.2f}%)")
    
    client.close()
    print("\n✓ Aggregates client test complete!")
