"""
ChronoX Integrated Backfill with Real-Time Dashboard

Launches backfill pipeline and real-time dashboard concurrently.
Shares metrics between processes using multiprocessing.Manager.

Usage:
    python scripts/run_integrated_backfill.py --tickers AMD,NVDA --years 1 --port 8050
"""

import argparse
import logging
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path
from threading import Thread
from typing import Dict, List

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.realtime_dashboard import start_dashboard
from data.clients.polygon_client import PolygonClient
from data.storage.timescale_writer import TimescaleWriter
from scripts.backfill_historical_data import BackfillOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('./logs/integrated_backfill.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def run_backfill_with_metrics(
    tickers: List[str],
    years: int,
    api_key: str,
    shared_metrics: Dict,
    use_flatfiles: bool = True
):
    """
    Run backfill process and update shared metrics.
    
    Args:
        tickers: List of tickers to backfill
        years: Years of historical data
        api_key: Polygon API key
        shared_metrics: Multiprocessing.Manager dict for metrics
        use_flatfiles: Use S3 flat files (faster)
    """
    try:
        logger.info(f"Starting backfill for {', '.join(tickers)} ({years} years)")
        
        # Create database writer
        db_writer = TimescaleWriter()
        
        # Create backfill orchestrator
        orchestrator = BackfillOrchestrator(
            polygon_api_key=api_key,
            db_writer=db_writer,
            years_back=years,
            max_workers=4,
            use_flatfiles=use_flatfiles,
            debug=True  # Enable debug logging for detailed metrics
        )
        
        # Hook into queue metrics to share with dashboard
        def update_shared_metrics():
            """Periodically update shared metrics from backfill pipeline"""
            while True:
                try:
                    metrics = orchestrator.queue_metrics
                    
                    # Update shared dict (thread-safe via Manager)
                    shared_metrics['download_qsize'] = metrics.get('download_qsize', lambda: 0)()
                    shared_metrics['process_qsize'] = metrics.get('process_qsize', lambda: 0)()
                    shared_metrics['download_maxsize'] = metrics.get('download_maxsize', 0)
                    shared_metrics['process_maxsize'] = metrics.get('process_maxsize', 0)
                    
                    # These will be updated by the backfill process itself
                    # via orchestrator's internal stats tracking
                    
                    time.sleep(1)  # Update every second
                
                except Exception as e:
                    logger.error(f"Error updating shared metrics: {e}")
                    time.sleep(1)
        
        # Start metrics updater thread
        metrics_thread = Thread(target=update_shared_metrics, daemon=True)
        metrics_thread.start()
        
        # Run backfill
        results = orchestrator.run_full_backfill(tickers)
        
        logger.info("Backfill completed successfully")
        logger.info(f"Results: {results}")
        
        # Cleanup
        orchestrator.close()
        
    except KeyboardInterrupt:
        logger.info("Backfill interrupted by user")
    
    except Exception as e:
        logger.error(f"Backfill error: {e}", exc_info=True)


def main():
    """Main entry point for integrated backfill + dashboard"""
    parser = argparse.ArgumentParser(description='ChronoX Integrated Backfill with Real-Time Dashboard')
    parser.add_argument('--tickers', type=str, required=True, help='Comma-separated list of tickers (e.g., AMD,NVDA,INTC)')
    parser.add_argument('--years', type=int, default=1, help='Years of historical data (default: 1)')
    parser.add_argument('--port', type=int, default=8050, help='Dashboard port (default: 8050)')
    parser.add_argument('--no-flatfiles', action='store_true', help='Use REST API instead of S3 flat files')
    parser.add_argument('--dashboard-only', action='store_true', help='Start dashboard without backfill (for testing)')
    
    args = parser.parse_args()
    
    # Parse tickers
    tickers = [t.strip().upper() for t in args.tickers.split(',')]
    
    # Load API key
    api_key = os.getenv('POLYGON_API_KEY')
    if not api_key:
        logger.error("POLYGON_API_KEY not found in environment")
        sys.exit(1)
    
    # Create shared metrics dict
    manager = multiprocessing.Manager()
    shared_metrics = manager.dict()
    
    # Initialize metrics
    shared_metrics['download_qsize'] = 0
    shared_metrics['process_qsize'] = 0
    shared_metrics['download_maxsize'] = 0
    shared_metrics['process_maxsize'] = 0
    shared_metrics['downloaded'] = 0
    shared_metrics['processed'] = 0
    shared_metrics['failed'] = 0
    shared_metrics['throughput'] = 0.0
    shared_metrics['active_futures'] = 0
    
    # Start dashboard in separate process
    dashboard_process = multiprocessing.Process(
        target=start_dashboard,
        args=(tickers, api_key, args.port, shared_metrics),
        daemon=False
    )
    dashboard_process.start()
    
    logger.info(f"Dashboard started on http://localhost:{args.port}")
    logger.info("Waiting 3 seconds for dashboard to initialize...")
    time.sleep(3)
    
    if not args.dashboard_only:
        # Start backfill in separate process
        backfill_process = multiprocessing.Process(
            target=run_backfill_with_metrics,
            args=(tickers, args.years, api_key, shared_metrics, not args.no_flatfiles),
            daemon=False
        )
        backfill_process.start()
        
        logger.info("Backfill started")
        
        # Handle Ctrl+C gracefully
        def signal_handler(sig, frame):
            logger.info("Shutting down...")
            backfill_process.terminate()
            dashboard_process.terminate()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        
        # Wait for backfill to complete
        backfill_process.join()
        
        logger.info("Backfill process completed")
        logger.info("Dashboard will continue running. Press Ctrl+C to exit.")
        
        # Keep dashboard running
        try:
            dashboard_process.join()
        except KeyboardInterrupt:
            logger.info("Shutting down dashboard...")
            dashboard_process.terminate()
    
    else:
        # Dashboard only mode
        logger.info("Dashboard-only mode. Press Ctrl+C to exit.")
        
        def signal_handler(sig, frame):
            logger.info("Shutting down...")
            dashboard_process.terminate()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        
        try:
            dashboard_process.join()
        except KeyboardInterrupt:
            logger.info("Shutting down dashboard...")
            dashboard_process.terminate()


if __name__ == '__main__':
    main()
