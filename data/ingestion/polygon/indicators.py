"""
Polygon.io Technical Indicators API Client

Handles fetching technical indicators (SMA, EMA, MACD, RSI).
"""

from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class IndicatorsClient:
    """
    Client for fetching technical indicators from Polygon.io.
    
    Note: Starter plan includes basic technical indicators.
    """
    
    def __init__(self, polygon_client):
        """
        Initialize indicators client.
        
        Args:
            polygon_client: Instance of PolygonClient
        """
        self.client = polygon_client
    
    def get_sma(self, ticker: str, 
               timespan: str = "day",
               window: int = 20,
               series_type: str = "close",
               adjusted: bool = True,
               order: str = "desc",
               limit: int = 5000) -> Dict:
        """
        Get Simple Moving Average (SMA) for a ticker.
        
        Args:
            ticker: Stock symbol
            timespan: Time period ('minute', 'hour', 'day', 'week', 'month', 'quarter', 'year')
            window: Window size for SMA calculation
            series_type: Price type ('close', 'open', 'high', 'low')
            adjusted: Apply split/dividend adjustments
            order: Sort order ('asc' or 'desc')
            limit: Max results
        
        Returns:
            Dict with:
                - results: List of SMA values with timestamp and value
                - status: Request status
                - next_url: Pagination URL if applicable
        """
        endpoint = f"/v1/indicators/sma/{ticker}"
        params = {
            "timespan": timespan,
            "window": window,
            "series_type": series_type,
            "adjusted": str(adjusted).lower(),
            "order": order,
            "limit": limit,
            "expand_underlying": "true"  # Include underlying OHLCV data
        }
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', {}).get('values', [])
            logger.info(f"Fetched {len(results)} SMA values for {ticker}")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch SMA for {ticker}: {str(e)}")
            return {}
    
    def get_ema(self, ticker: str,
               timespan: str = "day",
               window: int = 20,
               series_type: str = "close",
               adjusted: bool = True,
               order: str = "desc",
               limit: int = 5000) -> Dict:
        """
        Get Exponential Moving Average (EMA) for a ticker.
        
        Args:
            ticker: Stock symbol
            timespan: Time period
            window: Window size for EMA calculation
            series_type: Price type
            adjusted: Apply adjustments
            order: Sort order
            limit: Max results
        
        Returns:
            Dict with EMA results
        """
        endpoint = f"/v1/indicators/ema/{ticker}"
        params = {
            "timespan": timespan,
            "window": window,
            "series_type": series_type,
            "adjusted": str(adjusted).lower(),
            "order": order,
            "limit": limit,
            "expand_underlying": "true"
        }
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', {}).get('values', [])
            logger.info(f"Fetched {len(results)} EMA values for {ticker}")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch EMA for {ticker}: {str(e)}")
            return {}
    
    def get_macd(self, ticker: str,
                timespan: str = "day",
                short_window: int = 12,
                long_window: int = 26,
                signal_window: int = 9,
                series_type: str = "close",
                adjusted: bool = True,
                order: str = "desc",
                limit: int = 5000) -> Dict:
        """
        Get Moving Average Convergence Divergence (MACD) for a ticker.
        
        Args:
            ticker: Stock symbol
            timespan: Time period
            short_window: Short EMA period (default 12)
            long_window: Long EMA period (default 26)
            signal_window: Signal line period (default 9)
            series_type: Price type
            adjusted: Apply adjustments
            order: Sort order
            limit: Max results
        
        Returns:
            Dict with MACD results (value, signal, histogram)
        """
        endpoint = f"/v1/indicators/macd/{ticker}"
        params = {
            "timespan": timespan,
            "short_window": short_window,
            "long_window": long_window,
            "signal_window": signal_window,
            "series_type": series_type,
            "adjusted": str(adjusted).lower(),
            "order": order,
            "limit": limit,
            "expand_underlying": "true"
        }
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', {}).get('values', [])
            logger.info(f"Fetched {len(results)} MACD values for {ticker}")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch MACD for {ticker}: {str(e)}")
            return {}
    
    def get_rsi(self, ticker: str,
               timespan: str = "day",
               window: int = 14,
               series_type: str = "close",
               adjusted: bool = True,
               order: str = "desc",
               limit: int = 5000) -> Dict:
        """
        Get Relative Strength Index (RSI) for a ticker.
        
        Args:
            ticker: Stock symbol
            timespan: Time period
            window: Window size for RSI calculation (default 14)
            series_type: Price type
            adjusted: Apply adjustments
            order: Sort order
            limit: Max results
        
        Returns:
            Dict with RSI results (0-100 scale)
        """
        endpoint = f"/v1/indicators/rsi/{ticker}"
        params = {
            "timespan": timespan,
            "window": window,
            "series_type": series_type,
            "adjusted": str(adjusted).lower(),
            "order": order,
            "limit": limit,
            "expand_underlying": "true"
        }
        
        try:
            data = self.client._get(endpoint, params)
            results = data.get('results', {}).get('values', [])
            logger.info(f"Fetched {len(results)} RSI values for {ticker}")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch RSI for {ticker}: {str(e)}")
            return {}


if __name__ == "__main__":
    # Test the indicators client
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
    ind_client = IndicatorsClient(client)
    
    # Test SMA
    print("\n=== Testing SMA for AAPL ===")
    sma_data = ind_client.get_sma("AAPL", window=20, limit=10)
    if sma_data and 'results' in sma_data:
        values = sma_data['results'].get('values', [])
        if values:
            print(f"Latest SMA(20) values:")
            for val in values[:5]:
                print(f"  Timestamp: {val.get('timestamp')}, Value: {val.get('value', 0):.2f}")
    
    # Test EMA
    print("\n=== Testing EMA for AAPL ===")
    ema_data = ind_client.get_ema("AAPL", window=20, limit=10)
    if ema_data and 'results' in ema_data:
        values = ema_data['results'].get('values', [])
        if values:
            print(f"Latest EMA(20) values:")
            for val in values[:5]:
                print(f"  Timestamp: {val.get('timestamp')}, Value: {val.get('value', 0):.2f}")
    
    # Test MACD
    print("\n=== Testing MACD for AAPL ===")
    macd_data = ind_client.get_macd("AAPL", limit=10)
    if macd_data and 'results' in macd_data:
        values = macd_data['results'].get('values', [])
        if values:
            print(f"Latest MACD values:")
            for val in values[:5]:
                print(f"  Timestamp: {val.get('timestamp')}")
                print(f"    Value: {val.get('value', 0):.2f}")
                print(f"    Signal: {val.get('signal', 0):.2f}")
                print(f"    Histogram: {val.get('histogram', 0):.2f}")
    
    # Test RSI
    print("\n=== Testing RSI for AAPL ===")
    rsi_data = ind_client.get_rsi("AAPL", window=14, limit=10)
    if rsi_data and 'results' in rsi_data:
        values = rsi_data['results'].get('values', [])
        if values:
            print(f"Latest RSI(14) values:")
            for val in values[:5]:
                rsi_val = val.get('value', 0)
                print(f"  Timestamp: {val.get('timestamp')}, RSI: {rsi_val:.2f}", end="")
                if rsi_val > 70:
                    print(" [OVERBOUGHT]")
                elif rsi_val < 30:
                    print(" [OVERSOLD]")
                else:
                    print()
    
    client.close()
    print("\n✓ Indicators client test complete!")
