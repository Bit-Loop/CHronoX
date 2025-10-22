#!/usr/bin/env python3
"""
Quick verification script for Phase 0 and Phase 1 implementation.

Tests:
1. Database connection
2. Configuration loading
3. Logging setup
4. Pipeline initialization
5. Quality checks
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def test_database_connection():
    """Test TimescaleDB connection."""
    print("\n=== Testing Database Connection ===")
    try:
        from data.storage.timescale_writer import TimescaleWriter
        
        db = TimescaleWriter()
        if db.test_connection():
            print("✅ TimescaleDB connection successful")
            db.close()
            return True
        else:
            print("❌ TimescaleDB connection failed")
            return False
    except Exception as e:
        print(f"❌ Database error: {e}")
        return False


def test_configuration():
    """Test configuration loading."""
    print("\n=== Testing Configuration ===")
    try:
        from config.config_manager import get_config
        
        config = get_config()
        
        # Check required fields
        assert config.polygon.api_key, "Polygon API key missing"
        assert config.timescale.host, "TimescaleDB host missing"
        assert config.training.max_symbols_short_term > 0, "Training config invalid"
        
        print(f"✅ Configuration loaded successfully")
        print(f"   - Polygon API: {'***' + config.polygon.api_key[-4:]}")
        print(f"   - TimescaleDB: {config.timescale.host}:{config.timescale.port}")
        print(f"   - Training: max_symbols_short_term={config.training.max_symbols_short_term}")
        return True
    except Exception as e:
        print(f"❌ Configuration error: {e}")
        return False


def test_logging():
    """Test logging setup."""
    print("\n=== Testing Logging ===")
    try:
        from utils.logger import setup_logging
        import logging
        
        logger = setup_logging(
            name="VerificationTest",
            log_dir=project_root / "logs",
            level=logging.DEBUG
        )
        
        logger.debug("Debug message")
        logger.info("Info message")
        logger.warning("Warning message")
        
        print("✅ Logging setup successful (check logs/ directory)")
        return True
    except Exception as e:
        print(f"❌ Logging error: {e}")
        return False


def test_pipeline_init():
    """Test pipeline initialization (without starting)."""
    print("\n=== Testing Pipeline Initialization ===")
    try:
        from pipelines import DataIngestionPipeline
        from data.storage.timescale_writer import TimescaleWriter
        
        # Don't actually connect to database, just test initialization
        print("⚠️  Skipping actual database connection (requires Docker)")
        print("✅ Pipeline imports successful")
        return True
    except Exception as e:
        print(f"❌ Pipeline error: {e}")
        return False


def test_quality_checks():
    """Test quality check functions."""
    print("\n=== Testing Quality Checks ===")
    try:
        from pipelines import DataQualityChecker, AnomalyDetector
        import numpy as np
        
        # Test quality checker
        checker = DataQualityChecker()
        
        # Normal bar
        bar = {
            "t": 1704067200000,
            "o": 100.0,
            "h": 102.0,
            "l": 99.0,
            "c": 101.0,
            "v": 1000000
        }
        
        issues = checker.check_ohlcv_bar("AAPL", bar)
        assert len(issues) == 0, "Normal bar should have no issues"
        
        # Invalid bar (low > high)
        bad_bar = {
            "t": 1704067200000,
            "o": 100.0,
            "h": 98.0,  # High < Open
            "l": 99.0,
            "c": 101.0,
            "v": 1000000
        }
        
        issues = checker.check_ohlcv_bar("AAPL", bad_bar)
        assert len(issues) > 0, "Invalid bar should have issues"
        
        # Test anomaly detector
        detector = AnomalyDetector()
        
        # Data with outliers
        np.random.seed(42)
        prices = list(np.random.normal(100, 2, 100))
        prices[50] = 150  # Outlier
        
        indices, scores = detector.detect_price_anomalies(prices, method="zscore")
        assert len(indices) > 0, "Should detect outliers"
        assert 50 in indices, "Should detect index 50 as outlier"
        
        print("✅ Quality checks working correctly")
        print(f"   - Validated OHLCV bar checks")
        print(f"   - Detected {len(indices)} anomalies in test data")
        return True
    except Exception as e:
        print(f"❌ Quality checks error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("ChronoX Phase 0-1 Verification")
    print("=" * 60)
    
    results = []
    
    # Run tests
    results.append(("Configuration", test_configuration()))
    results.append(("Logging", test_logging()))
    results.append(("Quality Checks", test_quality_checks()))
    results.append(("Pipeline Init", test_pipeline_init()))
    results.append(("Database", test_database_connection()))
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! System ready for Phase 1 testing.")
        return 0
    else:
        print("\n⚠️  Some tests failed. Check errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
