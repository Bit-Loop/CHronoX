"""
Data Ingestion Pipeline for ChronoX Trading Bot

Orchestrates real-time and scheduled data ingestion from multiple sources:
- Polygon.io WebSocket (real-time delayed quotes)
- Polygon.io REST API (historical backfill)
- Data quality validation
- Multi-resolution aggregation
- Database persistence

Architecture:
    PolygonWebSocket → MessageQueue → ValidationPipeline → Aggregator → TimescaleDB
                                            ↓
                                    QualityChecks → Alerts

References:
- Apache Arrow for zero-copy data transfer: https://arrow.apache.org/
- Dask for parallel processing: https://docs.dask.org/
"""

import asyncio
import logging
from typing import List, Dict, Optional, Callable
from datetime import datetime, timedelta
from queue import Queue
from threading import Thread
import time

# Project imports
from data.ingestion.polygon.client import PolygonClient
from data.ingestion.polygon.websocket_client import PolygonWebSocketClient
from data.ingestion.polygon.aggregates import AggregatesClient
from data.storage.timescale_writer import TimescaleWriter
from config.config_manager import ConfigManager, get_config


class DataIngestionPipeline:
    """
    Manages real-time and historical data ingestion.
    
    Features:
    - WebSocket streaming for real-time data (15-min delayed)
    - REST API backfill for gaps and historical data
    - Message queue for buffering and rate limiting
    - Automatic reconnection and error recovery
    - Multi-symbol support (up to 100 symbols CPU-bound)
    
    Hardware Optimization:
    - CPU: Uses multiprocessing for parallel symbol processing
    - RAM: Buffers up to 10,000 messages before flushing
    - Storage: NVMe @ 7 GB/s enables fast bulk inserts
    
    Example:
        pipeline = DataIngestionPipeline(
            symbols=["AAPL", "TSLA", "NVDA"],
            db_writer=db_writer
        )
        
        # Start real-time streaming
        pipeline.start_streaming()
        
        # Backfill gaps
        pipeline.backfill_gaps(start_date="2024-01-01")
        
        # Shutdown gracefully
        pipeline.shutdown()
    """
    
    def __init__(
        self,
        symbols: List[str],
        db_writer: TimescaleWriter,
        config: Optional[ConfigManager] = None,
        buffer_size: int = 10000
    ):
        """
        Initialize data ingestion pipeline.
        
        Args:
            symbols: List of ticker symbols to ingest
            db_writer: TimescaleDB writer instance
            config: Configuration manager (uses global if None)
            buffer_size: Max messages to buffer before flushing
        """
        self.symbols = symbols
        self.db_writer = db_writer
        self.config = config or get_config()
        self.buffer_size = buffer_size
        
        # Setup logging
        self.logger = logging.getLogger(f"{__name__}.DataIngestionPipeline")
        self.logger.info(f"Initializing pipeline for {len(symbols)} symbols")
        
        # Initialize API clients
        self.polygon_client = PolygonClient(self.config.polygon.api_key)
        self.agg_client = AggregatesClient(self.polygon_client)
        self.ws_client: Optional[PolygonWebSocketClient] = None  # Initialized in start_streaming()
        
        # Message queue for buffering
        self.message_queue = Queue(maxsize=buffer_size)
        self.is_running = False
        
        # Worker threads
        self.ws_thread = None
        self.processor_thread = None
        
        # Statistics
        self.stats = {
            "messages_received": 0,
            "messages_processed": 0,
            "messages_failed": 0,
            "last_message_time": None,
            "symbols_active": set()
        }
    
    def start_streaming(self):
        """
        Start real-time WebSocket streaming.
        
        Subscribes to:
        - Trade updates (AM channel)
        - Quote updates (Q channel)
        - Aggregate bars (A channel)
        """
        if self.is_running:
            self.logger.warning("Pipeline already running")
            return
        
        self.logger.info("Starting real-time streaming...")
        self.is_running = True
        
        # Initialize WebSocket client
        self.ws_client = PolygonWebSocketClient(
            api_key=self.config.polygon.api_key
        )
        
        # Register callbacks for different event types
        # Polygon WebSocket sends events with 'ev' field: 'AM', 'A', 'T', 'Q'
        for event_type in ['AM', 'A', 'T', 'Q']:
            self.ws_client.register_callback(event_type, self._on_websocket_message)
        
        # Subscribe to all symbols
        channels = []
        for symbol in self.symbols:
            channels.extend([
                f"AM.{symbol}",  # Aggregate minute bars
                f"A.{symbol}",   # Aggregate second bars
                f"T.{symbol}",   # Trades
                f"Q.{symbol}"    # Quotes
            ])
        
        # Start WebSocket connection in separate thread
        self.ws_thread = Thread(
            target=self._run_websocket,
            args=(channels,),
            daemon=True
        )
        self.ws_thread.start()
        
        # Start message processor thread
        self.processor_thread = Thread(
            target=self._process_messages,
            daemon=True
        )
        self.processor_thread.start()
        
        self.logger.info(f"✓ Streaming started for {len(self.symbols)} symbols")
    
    def _run_websocket(self, channels: List[str]):
        """Run WebSocket client (called in thread)."""
        try:
            if self.ws_client is None:
                self.logger.error("WebSocket client not initialized")
                return
                
            self.ws_client.connect()
            self.ws_client.subscribe(channels)
            
            # Keep connection alive
            while self.is_running:
                time.sleep(1)
        
        except Exception as e:
            self.logger.error(f"WebSocket error: {e}", exc_info=True)
        
        finally:
            if self.ws_client:
                self.ws_client.close()
    
    def _on_websocket_message(self, message: Dict):
        """
        Callback for WebSocket messages.
        
        Args:
            message: Raw message from Polygon.io WebSocket
        """
        try:
            # Update statistics
            self.stats["messages_received"] += 1
            self.stats["last_message_time"] = datetime.now()
            
            # Extract symbol
            if "sym" in message:
                self.stats["symbols_active"].add(message["sym"])
            
            # Add to queue (non-blocking, drops if full)
            if not self.message_queue.full():
                self.message_queue.put(message)
            else:
                self.logger.warning("Message queue full, dropping message")
        
        except Exception as e:
            self.logger.error(f"Error handling message: {e}")
    
    def _process_messages(self):
        """
        Process messages from queue and write to database.
        
        Runs in separate thread to avoid blocking WebSocket.
        Uses bulk COPY operations for efficiency (50-100x faster than INSERT).
        """
        batch = []
        last_flush = time.time()
        flush_interval = 5.0  # Flush every 5 seconds
        
        while self.is_running:
            try:
                # Get message from queue (blocking with timeout)
                try:
                    message = self.message_queue.get(timeout=1.0)
                except:
                    # Timeout - flush if needed
                    if batch and (time.time() - last_flush) > flush_interval:
                        self._flush_batch(batch)
                        batch = []
                        last_flush = time.time()
                    continue
                
                # Add to batch
                batch.append(message)
                
                # Flush if batch full or time elapsed
                if len(batch) >= 100 or (time.time() - last_flush) > flush_interval:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = time.time()
                
                self.message_queue.task_done()
            
            except Exception as e:
                self.logger.error(f"Error processing messages: {e}", exc_info=True)
        
        # Final flush on shutdown
        if batch:
            self._flush_batch(batch)
    
    def _flush_batch(self, batch: List[Dict]):
        """
        Write batch of messages to database.
        
        Args:
            batch: List of WebSocket messages
        """
        if not batch:
            return
        
        try:
            # Group by message type
            trades = []
            quotes = []
            aggregates = []
            
            for msg in batch:
                ev = msg.get("ev", "")
                if ev == "T":
                    trades.append(msg)
                elif ev == "Q":
                    quotes.append(msg)
                elif ev in ["AM", "A"]:
                    aggregates.append(msg)
            
            # Write to database
            # TODO: Implement write methods for trades/quotes in TimescaleWriter
            if aggregates:
                # Convert Polygon format to OHLCV format
                ohlcv_bars = self._convert_aggregates(aggregates)
                for symbol, bars in ohlcv_bars.items():
                    self.db_writer.write_ohlcv_bars(symbol, bars, timeframe="1min")
            
            # Update statistics
            self.stats["messages_processed"] += len(batch)
            
            self.logger.debug(f"Flushed batch of {len(batch)} messages")
        
        except Exception as e:
            self.logger.error(f"Error flushing batch: {e}", exc_info=True)
            self.stats["messages_failed"] += len(batch)
    
    def _convert_aggregates(self, aggregates: List[Dict]) -> Dict[str, List[Dict]]:
        """
        Convert Polygon aggregate format to OHLCV format.
        
        Args:
            aggregates: List of aggregate messages
            
        Returns:
            Dict mapping symbol to list of OHLCV bars
        """
        result = {}
        
        for agg in aggregates:
            symbol = agg.get("sym")
            if not symbol:
                continue
            
            bar = {
                "t": agg.get("s"),  # Start timestamp (ms)
                "o": agg.get("o"),  # Open
                "h": agg.get("h"),  # High
                "l": agg.get("l"),  # Low
                "c": agg.get("c"),  # Close
                "v": agg.get("v"),  # Volume
                "vw": agg.get("vw"),  # VWAP
                "n": agg.get("n")  # Number of transactions
            }
            
            if symbol not in result:
                result[symbol] = []
            result[symbol].append(bar)
        
        return result
    
    def backfill_gaps(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ):
        """
        Backfill missing data using REST API.
        
        Args:
            start_date: Start date (YYYY-MM-DD). If None, uses last data point.
            end_date: End date (YYYY-MM-DD). If None, uses today.
        """
        self.logger.info("Starting gap detection and backfill...")
        
        # Set default end date to today if not provided
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        
        backfill_results = {}
        
        for symbol in self.symbols:
            try:
                backfill_start = None
                latest_str = None
                
                # 1. Query database for last timestamp per symbol
                latest = self.db_writer.get_latest_timestamp(symbol, '1min')
                
                if latest is None:
                    # No data exists for this symbol
                    if start_date is None:
                        self.logger.warning(f"No data for {symbol} and no start_date provided, skipping")
                        backfill_results[symbol] = {"status": "skipped", "reason": "no_data_no_start_date"}
                        continue
                    
                    # Backfill from start_date
                    self.logger.info(f"No data for {symbol}, backfilling from {start_date}")
                    backfill_start = start_date
                    bars = self._fetch_missing_bars(symbol, start_date, end_date)
                    
                else:
                    # Check if there's a gap between latest data and today
                    latest_str = latest.strftime("%Y-%m-%d")
                    gap_days = (datetime.now() - latest).days
                    
                    if gap_days <= 1:
                        # Data is up to date
                        self.logger.debug(f"{symbol} data is current (latest: {latest_str})")
                        backfill_results[symbol] = {"status": "current", "latest": latest_str}
                        continue
                    
                    # Gap detected - backfill from latest to end_date
                    self.logger.info(f"Gap detected for {symbol}: {gap_days} days from {latest_str}")
                    backfill_start = latest_str
                    bars = self._fetch_missing_bars(symbol, latest_str, end_date)
                
                # 3. Write to database using bulk COPY
                if bars:
                    count = self.db_writer.write_ohlcv_bars(symbol, bars, timeframe='1min', use_copy=True)
                    backfill_results[symbol] = {
                        "status": "success",
                        "bars_written": count,
                        "date_range": f"{backfill_start} to {end_date}"
                    }
                    self.logger.info(f"✓ Backfilled {count} bars for {symbol}")
                else:
                    backfill_results[symbol] = {"status": "no_data", "reason": "api_returned_empty"}
                    
            except Exception as e:
                self.logger.error(f"Error backfilling {symbol}: {e}", exc_info=True)
                backfill_results[symbol] = {"status": "error", "error": str(e)}
            
            # Small delay to respect API rate limits
            time.sleep(0.2)
        
        self.logger.info(f"Gap backfill complete: {len(backfill_results)} symbols processed")
        return backfill_results
    
    def _fetch_missing_bars(self, symbol: str, start_date: str, end_date: str) -> List[Dict]:
        """
        Fetch missing minute bars from Polygon REST API.
        
        Args:
            symbol: Ticker symbol
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            List of bar dictionaries
        """
        try:
            # 2. Fetch missing data from Polygon REST API using chunked method
            # to handle large date ranges (uses 7-day chunks to stay under 50k limit)
            bars = self.agg_client.get_minute_bars_chunked(
                ticker=symbol,
                start_date=start_date,
                end_date=end_date,
                adjusted=True,
                chunk_days=7
            )
            
            if bars:
                self.logger.debug(f"Fetched {len(bars)} bars for {symbol}")
            
            return bars or []
            
        except Exception as e:
            self.logger.error(f"Failed to fetch bars for {symbol}: {e}")
            return []
    
    def get_stats(self) -> Dict:
        """
        Get pipeline statistics.
        
        Returns:
            Dict with pipeline metrics
        """
        return {
            **self.stats,
            "queue_size": self.message_queue.qsize(),
            "is_running": self.is_running,
            "symbols_subscribed": len(self.symbols),
            "symbols_active": len(self.stats["symbols_active"])
        }
    
    def shutdown(self):
        """Gracefully shutdown pipeline."""
        self.logger.info("Shutting down pipeline...")
        
        self.is_running = False
        
        # Wait for threads to finish
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=5.0)
        
        if self.processor_thread and self.processor_thread.is_alive():
            self.processor_thread.join(timeout=5.0)
        
        # Close connections
        if self.ws_client:
            self.ws_client.close()
        
        self.polygon_client.close()
        
        self.logger.info("✓ Pipeline shutdown complete")


# Example usage
if __name__ == "__main__":
    from utils.logger import setup_logging
    from pathlib import Path
    
    # Setup logging
    logger = setup_logging(
        name="DataPipeline.Test",
        log_dir=Path("../logs"),
        level=logging.DEBUG
    )
    
    # Initialize database (requires running TimescaleDB)
    try:
        db_writer = TimescaleWriter()
        logger.info("✓ Connected to TimescaleDB")
    except Exception as e:
        logger.error(f"✗ Failed to connect to TimescaleDB: {e}")
        exit(1)
    
    # Initialize pipeline
    pipeline = DataIngestionPipeline(
        symbols=["AAPL", "TSLA", "NVDA"],
        db_writer=db_writer
    )
    
    try:
        # Start streaming
        pipeline.start_streaming()
        logger.info("Pipeline started, streaming real-time data...")
        
        # Run for 60 seconds
        for i in range(60):
            time.sleep(1)
            if i % 10 == 0:
                stats = pipeline.get_stats()
                logger.info(f"Stats: {stats}")
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    
    finally:
        pipeline.shutdown()
        db_writer.close()
