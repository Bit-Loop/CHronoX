"""
InfluxDB Writer for ChronoX Trading Bot

Handles writing market data, corporate actions, reference data, and news to InfluxDB.
"""

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS, ASYNCHRONOUS, WriteOptions
from typing import List, Dict, Optional
from datetime import datetime
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class InfluxConfig:
    """Configuration for InfluxDB connection"""
    url: str
    token: str
    org: str
    bucket_market: str = "market_data_5y"
    bucket_indicators: str = "indicators_1y"
    bucket_corp: str = "corporate_actions"
    bucket_ref: str = "reference_data"
    bucket_news: str = "news_6mo"
    bucket_snapshots: str = "snapshots_30d"


class InfluxWriter:
    """
    Writer for ChronoX market data to InfluxDB.
    
    Provides methods for writing:
        - OHLCV bars (multiple timeframes)
        - Corporate actions (dividends, splits)
        - Reference data (ticker metadata)
        - News articles
        - Real-time snapshots
    """
    
    def __init__(self, config: InfluxConfig, async_write: bool = True):
        """
        Initialize InfluxDB writer.
        
        Args:
            config: InfluxDB configuration
            async_write: Use asynchronous writes for better performance
        """
        self.config = config
        self.client = InfluxDBClient(url=config.url, token=config.token, org=config.org)
        
        # Configure write API
        if async_write:
            write_options = WriteOptions(
                batch_size=5000,
                flush_interval=10_000,  # 10 seconds
                jitter_interval=2_000,
                retry_interval=5_000,
                max_retries=3,
                max_retry_delay=30_000,
                exponential_base=2
            )
            self.write_api = self.client.write_api(write_options=write_options)
        else:
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        
        self.async_write = async_write
        logger.info(f"InfluxDB writer initialized (async={async_write})")
    
    def write_ohlcv_bars(self, ticker: str, bars: List[Dict], 
                        timeframe: str = "1min",
                        bucket: Optional[str] = None) -> int:
        """
        Write OHLCV bars to InfluxDB.
        
        Args:
            ticker: Stock symbol
            bars: List of bar dicts with keys: t, o, h, l, c, v, vw, n
            timeframe: Bar timeframe (1min, 5min, 15min, 1hour, 1day, etc.)
            bucket: Override default bucket
        
        Returns:
            Number of bars written
        """
        if not bars:
            logger.warning(f"No bars to write for {ticker}")
            return 0
        
        bucket = bucket or self.config.bucket_market
        points = []
        
        for bar in bars:
            try:
                point = Point("market_data") \
                    .tag("ticker", ticker) \
                    .tag("timeframe", timeframe) \
                    .field("open", float(bar['o'])) \
                    .field("high", float(bar['h'])) \
                    .field("low", float(bar['l'])) \
                    .field("close", float(bar['c'])) \
                    .field("volume", float(bar['v']))
                
                # Optional fields
                if 'vw' in bar and bar['vw']:
                    point.field("vwap", float(bar['vw']))
                if 'n' in bar and bar['n']:
                    point.field("transactions", int(bar['n']))
                
                # Timestamp - support both milliseconds and seconds
                timestamp = int(bar['t'])
                if timestamp > 1e12:  # Likely milliseconds
                    point.time(timestamp, WritePrecision.MS)
                else:  # Likely seconds
                    point.time(timestamp, WritePrecision.S)
                
                points.append(point)
            
            except (KeyError, ValueError, TypeError) as e:
                logger.error(f"Error creating point from bar: {e}")
                continue
        
        if points:
            self.write_api.write(bucket=bucket, org=self.config.org, record=points)
            logger.info(f"Wrote {len(points)} {timeframe} bars for {ticker} to {bucket}")
        
        return len(points)
    
    def write_corporate_actions(self, ticker: str, actions: List[Dict], 
                                action_type: str,
                                bucket: Optional[str] = None) -> int:
        """
        Write corporate actions (dividends or splits) to InfluxDB.
        
        Args:
            ticker: Stock symbol
            actions: List of action dicts
            action_type: 'dividend' or 'split'
            bucket: Override default bucket
        
        Returns:
            Number of actions written
        """
        if not actions:
            logger.warning(f"No {action_type} actions to write for {ticker}")
            return 0
        
        bucket = bucket or self.config.bucket_corp
        points = []
        
        for action in actions:
            try:
                point = Point("corporate_actions") \
                    .tag("ticker", ticker) \
                    .tag("action_type", action_type)
                
                if action_type == "dividend":
                    point.field("amount", float(action.get('cash_amount', 0)))
                    point.field("currency", action.get('currency', 'USD'))
                    point.field("declaration_date", action.get('declaration_date', ''))
                    point.field("ex_date", action.get('ex_dividend_date', ''))
                    point.field("pay_date", action.get('pay_date', ''))
                    point.field("record_date", action.get('record_date', ''))
                    point.field("frequency", action.get('frequency', 0))
                    
                    # Use ex-dividend date as timestamp
                    timestamp_str = action.get('ex_dividend_date')
                
                elif action_type == "split":
                    split_from = float(action.get('split_from', 1))
                    split_to = float(action.get('split_to', 1))
                    ratio = split_to / split_from if split_from != 0 else 1.0
                    
                    point.field("split_from", split_from)
                    point.field("split_to", split_to)
                    point.field("ratio", ratio)
                    point.field("execution_date", action.get('execution_date', ''))
                    
                    # Use execution date as timestamp
                    timestamp_str = action.get('execution_date')
                
                else:
                    logger.error(f"Unknown action type: {action_type}")
                    continue
                
                # Convert date string to timestamp
                if timestamp_str:
                    try:
                        ts = datetime.strptime(timestamp_str, '%Y-%m-%d')
                        point.time(ts, WritePrecision.S)
                        points.append(point)
                    except ValueError as e:
                        logger.error(f"Invalid date format: {timestamp_str}")
            
            except (KeyError, ValueError, TypeError) as e:
                logger.error(f"Error creating point from action: {e}")
                continue
        
        if points:
            self.write_api.write(bucket=bucket, org=self.config.org, record=points)
            logger.info(f"Wrote {len(points)} {action_type} actions for {ticker} to {bucket}")
        
        return len(points)
    
    def write_reference_data(self, ticker: str, metadata: Dict,
                            bucket: Optional[str] = None) -> bool:
        """
        Write ticker reference data to InfluxDB.
        
        Args:
            ticker: Stock symbol
            metadata: Ticker metadata dict
            bucket: Override default bucket
        
        Returns:
            True if successful
        """
        bucket = bucket or self.config.bucket_ref
        
        try:
            point = Point("reference") \
                .tag("ticker", ticker) \
                .field("name", metadata.get('name', '')) \
                .field("market", metadata.get('market', '')) \
                .field("locale", metadata.get('locale', '')) \
                .field("type", metadata.get('type', '')) \
                .field("active", metadata.get('active', True)) \
                .field("currency", metadata.get('currency_name', '')) \
                .field("exchange", metadata.get('primary_exchange', '')) \
                .field("description", metadata.get('description', '')[:500])  # Truncate long descriptions
            
            # Optional fields
            if 'sic_description' in metadata:
                point.field("sector", metadata['sic_description'])
            if 'sic_code' in metadata:
                point.field("sic_code", metadata['sic_code'])
            if 'market_cap' in metadata:
                point.field("market_cap", float(metadata['market_cap']))
            if 'total_employees' in metadata:
                point.field("employees", int(metadata['total_employees']))
            if 'homepage_url' in metadata:
                point.field("homepage_url", metadata['homepage_url'])
            if 'list_date' in metadata:
                point.field("list_date", metadata['list_date'])
            
            # Use current time as timestamp
            point.time(datetime.utcnow(), WritePrecision.S)
            
            self.write_api.write(bucket=bucket, org=self.config.org, record=point)
            logger.info(f"Wrote reference data for {ticker} to {bucket}")
            return True
        
        except Exception as e:
            logger.error(f"Error writing reference data for {ticker}: {str(e)}")
            return False
    
    def write_news(self, articles: List[Dict],
                  bucket: Optional[str] = None) -> int:
        """
        Write news articles to InfluxDB.
        
        Args:
            articles: List of article dicts
            bucket: Override default bucket
        
        Returns:
            Number of articles written
        """
        if not articles:
            logger.warning("No articles to write")
            return 0
        
        bucket = bucket or self.config.bucket_news
        points = []
        
        for article in articles:
            tickers = article.get('tickers', [])
            if not tickers:
                continue
            
            # Create one point per ticker mentioned
            for ticker in tickers:
                try:
                    publisher = article.get('publisher', {})
                    
                    point = Point("news") \
                        .tag("ticker", ticker) \
                        .tag("publisher", publisher.get('name', 'Unknown')) \
                        .field("title", article.get('title', '')[:500]) \
                        .field("author", article.get('author', '')) \
                        .field("article_url", article.get('article_url', '')) \
                        .field("tickers_list", ','.join(tickers)) \
                        .field("keywords", ','.join(article.get('keywords', [])[:20]))  # Limit keywords
                    
                    # Optional description
                    if 'description' in article:
                        point.field("description", article['description'][:1000])
                    
                    # Timestamp from article
                    published = article.get('published_utc')
                    if published:
                        # Handle both ISO format and timestamp
                        if isinstance(published, str):
                            # Remove timezone suffix for parsing
                            published_clean = published.replace('Z', '+00:00')
                            try:
                                ts = datetime.fromisoformat(published_clean)
                            except ValueError:
                                ts = datetime.strptime(published, '%Y-%m-%dT%H:%M:%S')
                        else:
                            ts = datetime.fromtimestamp(published)
                        
                        point.time(ts, WritePrecision.S)
                        points.append(point)
                
                except Exception as e:
                    logger.error(f"Error creating point from article: {e}")
                    continue
        
        if points:
            self.write_api.write(bucket=bucket, org=self.config.org, record=points)
            logger.info(f"Wrote {len(points)} news points to {bucket}")
        
        return len(points)
    
    def write_snapshot(self, ticker: str, snapshot: Dict,
                      bucket: Optional[str] = None) -> bool:
        """
        Write market snapshot to InfluxDB.
        
        Args:
            ticker: Stock symbol
            snapshot: Snapshot dict from Polygon.io
            bucket: Override default bucket
        
        Returns:
            True if successful
        """
        bucket = bucket or self.config.bucket_snapshots
        
        try:
            day = snapshot.get('day', {})
            last_trade = snapshot.get('lastTrade', {})
            last_quote = snapshot.get('lastQuote', {})
            prev_day = snapshot.get('prevDay', {})
            
            point = Point("snapshot") \
                .tag("ticker", ticker)
            
            # Today's data
            if day:
                point.field("day_open", float(day.get('o', 0)))
                point.field("day_high", float(day.get('h', 0)))
                point.field("day_low", float(day.get('l', 0)))
                point.field("day_close", float(day.get('c', 0)))
                point.field("day_volume", float(day.get('v', 0)))
                point.field("day_vwap", float(day.get('vw', 0)))
            
            # Last trade
            if last_trade:
                point.field("last_price", float(last_trade.get('p', 0)))
                point.field("last_size", int(last_trade.get('s', 0)))
            
            # Last quote
            if last_quote:
                point.field("bid", float(last_quote.get('p', 0)))
                point.field("bid_size", int(last_quote.get('s', 0)))
                point.field("ask", float(last_quote.get('P', 0)))
                point.field("ask_size", int(last_quote.get('S', 0)))
            
            # Previous day for change calculation
            if prev_day:
                point.field("prev_close", float(prev_day.get('c', 0)))
                point.field("prev_volume", float(prev_day.get('v', 0)))
            
            # Calculate change percentage
            if day and prev_day:
                curr_close = float(day.get('c', 0))
                prev_close = float(prev_day.get('c', 0))
                if prev_close > 0:
                    change_pct = ((curr_close - prev_close) / prev_close) * 100
                    point.field("change_percent", change_pct)
            
            point.time(datetime.utcnow(), WritePrecision.S)
            
            self.write_api.write(bucket=bucket, org=self.config.org, record=point)
            logger.debug(f"Wrote snapshot for {ticker} to {bucket}")
            return True
        
        except Exception as e:
            logger.error(f"Error writing snapshot for {ticker}: {str(e)}")
            return False
    
    def flush(self):
        """Force flush any pending writes (for async mode)"""
        if self.async_write:
            self.write_api.flush()
            logger.info("Flushed pending writes")
    
    def close(self):
        """Close InfluxDB client connection"""
        try:
            self.flush()
            self.write_api.close()
            self.client.close()
            logger.info("InfluxDB client closed")
        except Exception as e:
            logger.error(f"Error closing InfluxDB client: {str(e)}")
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()


if __name__ == "__main__":
    # Test the InfluxDB writer
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    logging.basicConfig(level=logging.INFO)
    
    # Configure InfluxDB
    config = InfluxConfig(
        url=os.getenv("INFLUX_URL", "http://localhost:8086"),
        token=os.getenv("INFLUX_TOKEN"),
        org=os.getenv("INFLUX_ORG", "chronox"),
        bucket_market=os.getenv("INFLUX_BUCKET_MARKET", "market_data_5y")
    )
    
    # Test data
    test_bars = [
        {
            't': 1696176000000,  # Milliseconds
            'o': 175.50,
            'h': 176.25,
            'l': 175.30,
            'c': 176.00,
            'v': 1000000,
            'vw': 175.85,
            'n': 500
        }
    ]
    
    print("\n=== InfluxDB Writer Test ===")
    
    with InfluxWriter(config, async_write=False) as writer:
        # Test writing OHLCV bars
        print("\n=== Testing OHLCV Write ===")
        count = writer.write_ohlcv_bars("AAPL", test_bars, timeframe="1min")
        print(f"Wrote {count} bars")
        
        # Test writing reference data
        print("\n=== Testing Reference Data Write ===")
        test_metadata = {
            'name': 'Apple Inc.',
            'market': 'stocks',
            'locale': 'us',
            'type': 'CS',
            'active': True,
            'currency_name': 'USD',
            'primary_exchange': 'XNAS',
            'description': 'Apple Inc. designs, manufactures, and markets smartphones...',
            'market_cap': 2800000000000,
            'total_employees': 164000
        }
        success = writer.write_reference_data("AAPL", test_metadata)
        print(f"Reference data write: {'Success' if success else 'Failed'}")
    
    print("\n✓ InfluxDB writer test complete!")
