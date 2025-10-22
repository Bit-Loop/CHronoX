#!/usr/bin/env python3
"""
ChronoX Trading Bot - Main Entry Point

This is the central orchestrator for the ChronoX trading bot system.
It coordinates all subsystems including:
- Data ingestion (historical & real-time)
- Feature engineering & preprocessing
- ML model training & inference
- Backtesting & performance evaluation
- Paper trading & live trading
- Monitoring & alerting

Hardware Context:
- CPU: AMD Ryzen 9 9950X (16 cores / 32 threads)
- RAM: 96 GB DDR5-6000
- GPU: NVIDIA RTX 5070 (12 GB VRAM; ~10 GB usable)
- Storage: Samsung 990 Pro NVMe (7 GB/s)

Scaling Recommendations:
- Feature generation: Up to 100 symbols (CPU + RAM)
- Short-term training (15 min): 10-15 symbols (GPU, FP16)
- Long-term training (daily): 5-10 symbols (GPU, FP16 + gradient checkpointing)
- Backtesting: Up to 100 symbols (CPU, multiprocessing)
- Real-time inference: 20-30 symbols (GPU, no gradients)

Usage:
    # Interactive mode (future GUI)
    python main.py

    # CLI mode with specific action
    python main.py --action backfill --tickers AAPL,TSLA,NVDA
    python main.py --action train --model transformer --symbols 15
    python main.py --action backtest --start 2020-01-01 --end 2023-12-31
    python main.py --action paper_trade --symbols 10
    python main.py --action live_trade --symbols 5

Author: ChronoX Team
Version: 0.1.0
Date: 2025-10-16
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, List
import os
from datetime import datetime

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Core imports
from config.config_manager import ConfigManager
from utils.logger import setup_logging

# Phase 0: Data Acquisition (COMPLETE)
from data.ingestion.polygon.client import PolygonClient
from data.storage.timescale_writer import TimescaleWriter

# Phase 1: Data Pipeline (TODO)
# from pipelines.data_ingestion_pipeline import DataIngestionPipeline
# from pipelines.quality_checks import DataQualityChecker

# Phase 2: Preprocessing & Feature Engineering (TODO)
# from data.preprocessing.feature_engineering import FeatureEngineer
# from data.preprocessing.multi_resolution_pipeline import MultiResolutionPipeline

# Phase 3: ML Models (TODO)
# from models.transformers.temporal_fusion_transformer import TemporalFusionTransformer
# from models.transformers.timescale_fusion import TimeScaleFusion
# from models.rl_agents.ppo_agent import PPOAgent

# Phase 4: Training (TODO)
# from training.train_orchestrator import TrainingOrchestrator
# from training.hyperparameter_tuner import HyperparameterTuner

# Phase 5: Backtesting (TODO)
# from backtesting.backtester import Backtester
# from backtesting.performance_metrics import PerformanceAnalyzer

# Phase 6: Trading (TODO)
# from trading.paper_trading_bot import PaperTradingBot
# from trading.live_trading_bot import LiveTradingBot
# from trading.risk_manager import RiskManager

# Phase 7: Monitoring (TODO)
# from monitoring.prometheus_exporter import PrometheusExporter
# from monitoring.alert_manager import AlertManager


class ChronoXOrchestrator:
    """
    Main orchestrator for the ChronoX trading bot.
    
    Coordinates all subsystems and manages the application lifecycle.
    Implements the observer pattern for inter-component communication.
    
    Attributes:
        config (ConfigManager): Configuration manager
        logger (logging.Logger): Application logger
        db_writer (TimescaleWriter): Database writer for market data
        polygon_client (PolygonClient): Polygon.io API client
        
    TODO:
        - data_pipeline: Data ingestion and quality checks
        - feature_engineer: Feature generation and preprocessing
        - model_trainer: ML model training orchestrator
        - backtester: Backtesting engine
        - trading_bot: Paper/live trading bot
        - risk_manager: Risk management system
        - monitor: Monitoring and alerting
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize ChronoX orchestrator.
        
        Args:
            config_path: Path to configuration file. If None, uses default config.
        """
        # Setup logging first
        self.logger = setup_logging(
            name="ChronoX",
            log_dir=PROJECT_ROOT / "logs",
            level=logging.INFO
        )
        self.logger.info("="*80)
        self.logger.info("ChronoX Trading Bot - Initializing")
        self.logger.info("="*80)
        
        # Load configuration
        self.config = ConfigManager(config_path)
        self.logger.info(f"Configuration loaded from: {config_path or 'default'}")
        
        # Initialize Phase 0 components (Data Acquisition)
        self._init_phase0()
        
        # TODO: Initialize remaining phases as they are implemented
        # self._init_phase1()  # Data Pipeline
        # self._init_phase2()  # Feature Engineering
        # self._init_phase3()  # ML Models
        # self._init_phase4()  # Training
        # self._init_phase5()  # Backtesting
        # self._init_phase6()  # Trading
        # self._init_phase7()  # Monitoring
        
        self.logger.info("ChronoX initialization complete")
    
    def _init_phase0(self):
        """Initialize Phase 0: Data Acquisition components."""
        self.logger.info("Initializing Phase 0: Data Acquisition")
        
        # Initialize Polygon.io API client
        api_key = os.getenv("POLYGON_API_KEY")
        if not api_key:
            self.logger.warning("POLYGON_API_KEY not found in environment")
            self.polygon_client = None
        else:
            self.polygon_client = PolygonClient(api_key)
            self.logger.info("✓ Polygon.io API client initialized")
        
        # Initialize TimescaleDB writer
        try:
            self.db_writer = TimescaleWriter()
            if self.db_writer.test_connection():
                self.logger.info("✓ TimescaleDB connection established")
            else:
                self.logger.error("✗ TimescaleDB connection failed")
                self.db_writer = None
        except Exception as e:
            self.logger.error(f"✗ TimescaleDB initialization failed: {e}")
            self.db_writer = None
    
    def run_backfill(
        self,
        tickers: List[str],
        start_date: str = "2020-01-01", #shouldnt be static...
        end_date: Optional[str] = None,
        skip_minute: bool = True,
        use_flatfiles: bool = False
    ):
        """
        Run historical data backfill.
        
        Args:
            tickers: List of stock symbols to backfill
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD). If None, uses today.
            skip_minute: If True, skip minute bars (only daily/hourly)
            use_flatfiles: If True, use Polygon flat files (100-1000x faster)
        
        Returns:
            dict: Backfill results with success/failure counts
        """
        self.logger.info(f"Starting backfill for {len(tickers)} tickers")
        self.logger.info(f"Date range: {start_date} to {end_date or 'today'}")
        self.logger.info(f"Using flat files: {use_flatfiles}")
        
        # Import backfill orchestrator
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from scripts.backfill_historical_data import BackfillOrchestrator
        
        # Get API key from environment
        api_key = os.getenv("POLYGON_API_KEY")
        if not api_key:
            self.logger.error("POLYGON_API_KEY not found in environment")
            return {"status": "failed", "error": "Missing POLYGON_API_KEY"}
        
        if not self.db_writer:
            self.logger.error("Database writer not initialized")
            return {"status": "failed", "error": "Database not connected"}
        
        try:
            # Initialize backfill orchestrator
            backfill = BackfillOrchestrator(
                polygon_api_key=api_key,
                db_writer=self.db_writer,
                years_back=5,  # Configurable if needed
                max_workers=4,
                use_flatfiles=use_flatfiles
            )
            
            # Execute full backfill for specified tickers
            results = backfill.run_full_backfill(tickers)
            
            self.logger.info("Backfill completed successfully")
            return results
            
        except Exception as e:
            self.logger.error(f"Backfill failed: {e}", exc_info=True)
            return {"status": "failed", "error": str(e)}
    
    def run_training(
        self,
        model_type: str = "transformer",
        symbols: int = 15,
        timeframe: str = "15min",
        epochs: int = 100
    ):
        """
        Run ML model training.
        
        Args:
            model_type: Type of model ('transformer', 'tft', 'rl')
            symbols: Number of symbols to train on (10-15 for short-term)
            timeframe: Timeframe ('1min', '15min', '1hour', '12hour', 'daily')
            epochs: Number of training epochs
        
        Returns:
            dict: Training results with metrics
        """
        self.logger.info(f"Starting {model_type} training")
        self.logger.info(f"Symbols: {symbols}, Timeframe: {timeframe}, Epochs: {epochs}")
        
        # TODO: Implement training orchestrator
        self.logger.warning("Training not yet implemented")
        return {"status": "not_implemented"}
    
    def run_backtest(
        self,
        start_date: str = "2020-01-01",
        end_date: str = "2023-12-31",
        tickers: Optional[List[str]] = None,
        strategy: str = "ml_ensemble"
    ):
        """
        Run backtesting simulation.
        
        Args:
            start_date: Backtest start date
            end_date: Backtest end date
            tickers: List of symbols. If None, uses default watchlist.
            strategy: Strategy to test ('ml_ensemble', 'momentum', 'mean_reversion')
        
        Returns:
            dict: Backtest results with performance metrics
        """
        self.logger.info(f"Starting backtest: {start_date} to {end_date}")
        self.logger.info(f"Strategy: {strategy}")
        
        # TODO: Implement backtesting engine
        self.logger.warning("Backtesting not yet implemented")
        return {"status": "not_implemented"}
    
    def run_paper_trading(self, symbols: int = 10):
        """
        Start paper trading bot.
        
        Args:
            symbols: Number of symbols to trade concurrently
        
        Returns:
            None (runs until interrupted)
        """
        self.logger.info(f"Starting paper trading with {symbols} symbols")
        
        # TODO: Implement paper trading bot
        self.logger.warning("Paper trading not yet implemented")
    
    def run_live_trading(self, symbols: int = 5):
        """
        Start live trading bot.
        
        ⚠️ WARNING: This trades with real money!
        
        Args:
            symbols: Number of symbols to trade concurrently
        
        Returns:
            None (runs until interrupted)
        """
        self.logger.warning("="*80)
        self.logger.warning("⚠️  LIVE TRADING MODE - REAL MONEY AT RISK")
        self.logger.warning("="*80)
        self.logger.info(f"Starting live trading with {symbols} symbols")
        
        # TODO: Implement live trading bot with circuit breakers
        self.logger.warning("Live trading not yet implemented")
    
    def shutdown(self):
        """Gracefully shutdown all components."""
        self.logger.info("Shutting down ChronoX...")
        
        # Close database connections
        if self.db_writer:
            self.db_writer.close()
            self.logger.info("✓ Database connection closed")
        
        # Close API connections
        if self.polygon_client:
            self.polygon_client.close()
            self.logger.info("✓ API client closed")
        
        # TODO: Shutdown other components as they are added
        
        self.logger.info("ChronoX shutdown complete")


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="ChronoX Trading Bot - AI-Powered Algorithmic Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        "--action",
        type=str,
        choices=["backfill", "train", "backtest", "paper_trade", "live_trade", "gui"],
        default="gui",
        help="Action to perform (default: gui)"
    )
    
    parser.add_argument(
        "--tickers",
        type=str,
        help="Comma-separated list of tickers (e.g., AAPL,TSLA,NVDA)"
    )
    
    parser.add_argument(
        "--symbols",
        type=int,
        default=15,
        help="Number of symbols to process (default: 15)"
    )
    
    parser.add_argument(
        "--start",
        type=str,
        default="2020-01-01",
        help="Start date for backfill/backtest (YYYY-MM-DD)"
    )
    
    parser.add_argument(
        "--end",
        type=str,
        help="End date for backfill/backtest (YYYY-MM-DD, default: today)"
    )
    
    parser.add_argument(
        "--model",
        type=str,
        choices=["transformer", "tft", "rl", "ensemble"],
        default="transformer",
        help="Model type for training (default: transformer)"
    )
    
    parser.add_argument(
        "--timeframe",
        type=str,
        choices=["1min", "15min", "1hour", "12hour", "daily"],
        default="15min",
        help="Timeframe for training (default: 15min)"
    )
    
    parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration file"
    )
    
    parser.add_argument(
        "--skip-minute",
        action="store_true",
        help="Skip minute bars during backfill (faster, less storage)"
    )
    
    parser.add_argument(
        "--flatfiles",
        action="store_true",
        help="Use Polygon flat files (S3) for faster backfill (100-1000x faster)"
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_arguments()
    
    # Initialize orchestrator
    orchestrator = ChronoXOrchestrator(config_path=args.config)
    
    try:
        # Execute requested action
        if args.action == "backfill":
            if not args.tickers:
                print("Error: --tickers required for backfill")
                sys.exit(1)
            
            tickers = [t.strip() for t in args.tickers.split(",")]
            orchestrator.run_backfill(
                tickers=tickers,
                start_date=args.start,
                end_date=args.end,
                skip_minute=args.skip_minute,
                use_flatfiles=args.flatfiles
            )
        
        elif args.action == "train":
            orchestrator.run_training(
                model_type=args.model,
                symbols=args.symbols,
                timeframe=args.timeframe
            )
        
        elif args.action == "backtest":
            tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
            orchestrator.run_backtest(
                start_date=args.start,
                end_date=args.end or datetime.now().strftime("%Y-%m-%d"),
                tickers=tickers
            )
        
        elif args.action == "paper_trade":
            orchestrator.run_paper_trading(symbols=args.symbols)
        
        elif args.action == "live_trade":
            # Require explicit confirmation for live trading
            confirmation = input(
                "\n⚠️  WARNING: You are about to start LIVE TRADING with REAL MONEY.\n"
                "Type 'I UNDERSTAND THE RISKS' to proceed: "
            )
            if confirmation == "I UNDERSTAND THE RISKS":
                orchestrator.run_live_trading(symbols=args.symbols)
            else:
                print("Live trading cancelled.")
        
        elif args.action == "gui":
            print("\n" + "="*80)
            print("ChronoX Trading Bot - GUI Mode")
            print("="*80)
            print("\nGUI not yet implemented. Use CLI mode:")
            print("\nExamples:")
            print("  python main.py --action backfill --tickers AAPL,TSLA,NVDA")
            print("  python main.py --action train --model transformer --symbols 15")
            print("  python main.py --action backtest --start 2020-01-01 --end 2023-12-31")
            print("  python main.py --action paper_trade --symbols 10")
            print("\nFor full help: python main.py --help")
            print("="*80 + "\n")
    
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    
    except Exception as e:
        orchestrator.logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    
    finally:
        orchestrator.shutdown()


if __name__ == "__main__":
    main()
