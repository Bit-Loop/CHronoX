#!/usr/bin/env python3
"""
Test script to verify all ChronoX modules can be imported.
Run this after installing dependencies to ensure everything is set up correctly.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def test_imports():
    """Test importing all major modules."""
    print("Testing ChronoX module imports...\n")
    
    tests = []
    
    # Core utilities
    try:
        from config.settings import Settings
        tests.append(("✅", "config.settings", "Settings configuration"))
    except Exception as e:
        tests.append(("❌", "config.settings", f"Error: {str(e)[:50]}"))
    
    # Data ingestion
    try:
        from data.ingestion.base import BaseDataSource
        tests.append(("✅", "data.ingestion.base", "Base data source"))
    except Exception as e:
        tests.append(("❌", "data.ingestion.base", f"Error: {str(e)[:50]}"))
    
    # Data preprocessing
    try:
        from data.preprocessing.cleaner import DataCleaner
        tests.append(("✅", "data.preprocessing.cleaner", "Data cleaner"))
    except Exception as e:
        tests.append(("❌", "data.preprocessing.cleaner", f"Error: {str(e)[:50]}"))
    
    # Feature engineering
    try:
        from features.technical_indicators import TechnicalIndicators
        tests.append(("✅", "features.technical_indicators", "Technical indicators"))
    except Exception as e:
        tests.append(("❌", "features.technical_indicators", f"Error: {str(e)[:50]}"))
    
    # ML models
    try:
        from models.ml.base_model import BaseMLModel
        tests.append(("✅", "models.ml.base_model", "Base ML model"))
    except Exception as e:
        tests.append(("❌", "models.ml.base_model", f"Error: {str(e)[:50]}"))
    
    # RL agents
    try:
        from models.rl.base_agent import BaseRLAgent
        tests.append(("✅", "models.rl.base_agent", "Base RL agent"))
    except Exception as e:
        tests.append(("❌", "models.rl.base_agent", f"Error: {str(e)[:50]}"))
    
    # Backtesting
    try:
        from backtesting.engine import BacktestEngine
        tests.append(("✅", "backtesting.engine", "Backtest engine"))
    except Exception as e:
        tests.append(("❌", "backtesting.engine", f"Error: {str(e)[:50]}"))
    
    # Ensemble
    try:
        from ensemble.meta_learner import MetaLearner
        tests.append(("✅", "ensemble.meta_learner", "Meta learner"))
    except Exception as e:
        tests.append(("❌", "ensemble.meta_learner", f"Error: {str(e)[:50]}"))
    
    # Monitoring
    try:
        from monitoring.drift_detection import DriftDetector
        tests.append(("✅", "monitoring.drift_detection", "Drift detector"))
    except Exception as e:
        tests.append(("❌", "monitoring.drift_detection", f"Error: {str(e)[:50]}"))
    
    # Print results
    print("Import Test Results:")
    print("=" * 80)
    for status, module, description in tests:
        print(f"{status} {module:40s} {description}")
    
    print("\n" + "=" * 80)
    
    # Summary
    passed = sum(1 for t in tests if t[0] == "✅")
    failed = sum(1 for t in tests if t[0] == "❌")
    
    print(f"\nPassed: {passed}/{len(tests)}")
    print(f"Failed: {failed}/{len(tests)}")
    
    if failed == 0:
        print("\n🎉 All imports successful! ChronoX is ready to run.")
        return 0
    else:
        print("\n⚠️  Some imports failed. Please check the errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(test_imports())
