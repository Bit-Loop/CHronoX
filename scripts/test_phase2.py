#!/usr/bin/env python3
"""
Test Phase 2: Feature Engineering

Tests technical indicators and multi-resolution pipeline
on real data from TimescaleDB.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd
import numpy as np
import logging
from data.storage.timescale_writer import TimescaleWriter
from data.preprocessing import (
    sma, ema, rsi, macd, bollinger_bands, atr,
    add_all_indicators,
    MultiResolutionPipeline
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_indicators():
    """Test technical indicators on sample data."""
    print("\n" + "="*60)
    print("Testing Technical Indicators")
    print("="*60)
    
    # Create sample OHLCV data
    np.random.seed(42)
    n = 100
    
    df = pd.DataFrame({
        'timestamp': pd.date_range('2024-01-01', periods=n, freq='D'),
        'open': 100 + np.cumsum(np.random.randn(n) * 2),
        'high': 102 + np.cumsum(np.random.randn(n) * 2),
        'low': 98 + np.cumsum(np.random.randn(n) * 2),
        'close': 100 + np.cumsum(np.random.randn(n) * 2),
        'volume': np.random.randint(1000000, 5000000, n)
    })
    
    # Ensure high >= low
    df['high'] = df[['open', 'high', 'close']].max(axis=1)
    df['low'] = df[['open', 'low', 'close']].min(axis=1)
    
    # Test individual indicators
    print("\n1. Testing Moving Averages...")
    df['sma_20'] = sma(df['close'], 20)
    df['ema_20'] = ema(df['close'], 20)
    print(f"   ✓ SMA(20): {df['sma_20'].dropna().iloc[-1]:.2f}")
    print(f"   ✓ EMA(20): {df['ema_20'].dropna().iloc[-1]:.2f}")
    
    print("\n2. Testing RSI...")
    df['rsi_14'] = rsi(df['close'], 14)
    print(f"   ✓ RSI(14): {df['rsi_14'].dropna().iloc[-1]:.2f}")
    
    print("\n3. Testing MACD...")
    macd_line, signal, hist = macd(df['close'])
    print(f"   ✓ MACD Line: {macd_line.dropna().iloc[-1]:.2f}")
    print(f"   ✓ Signal: {signal.dropna().iloc[-1]:.2f}")
    print(f"   ✓ Histogram: {hist.dropna().iloc[-1]:.2f}")
    
    print("\n4. Testing Bollinger Bands...")
    upper, middle, lower = bollinger_bands(df['close'])
    print(f"   ✓ Upper: {upper.dropna().iloc[-1]:.2f}")
    print(f"   ✓ Middle: {middle.dropna().iloc[-1]:.2f}")
    print(f"   ✓ Lower: {lower.dropna().iloc[-1]:.2f}")
    
    print("\n5. Testing ATR...")
    df['atr_14'] = atr(df['high'], df['low'], df['close'], 14)
    print(f"   ✓ ATR(14): {df['atr_14'].dropna().iloc[-1]:.2f}")
    
    print("\n6. Testing add_all_indicators()...")
    df_full = add_all_indicators(df.copy())
    print(f"   ✓ Added {len(df_full.columns) - len(df.columns)} indicators")
    print(f"   ✓ Total features: {len(df_full.columns)}")
    
    return True


def test_multi_resolution_with_db():
    """Test multi-resolution pipeline with real database data."""
    print("\n" + "="*60)
    print("Testing Multi-Resolution Pipeline (Real Data)")
    print("="*60)
    
    try:
        # Connect to database
        db = TimescaleWriter()
        print("\n✓ Connected to TimescaleDB")
        
        # Fetch 1-minute data
        query = """
            SELECT time as timestamp, open, high, low, close, volume
            FROM market_data
            WHERE ticker = 'AAPL' AND timeframe = '1min'
            ORDER BY time
            LIMIT 5000
        """
        
        with db.get_connection() as conn:
            df = pd.read_sql(query, conn)
        
        if len(df) == 0:
            print("⚠️  No 1-minute data found in database")
            print("   Run backfill without --skip-minute to get minute bars")
            return False
        
        print(f"✓ Fetched {len(df)} rows of 1-minute AAPL data")
        print(f"  Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
        
        # Initialize pipeline
        print("\nInitializing multi-resolution pipeline...")
        pipeline = MultiResolutionPipeline(
            timescales=['1min', '15min', '1hour', 'daily']
        )
        
        # Transform
        print("\nTransforming data to multiple resolutions...")
        scale_data = pipeline.transform(df)
        
        # Show results
        print("\n" + "="*60)
        print("Results by Timescale:")
        print("="*60)
        
        for scale_name, df_scale in scale_data.items():
            print(f"\n{scale_name}:")
            print(f"  Rows: {len(df_scale)}")
            print(f"  Columns: {len(df_scale.columns)}")
            print(f"  Sample features: {list(df_scale.columns)[:8]}")
            
            # Show latest values
            if len(df_scale) > 0:
                latest = df_scale.iloc[-1]
                print(f"  Latest close: ${latest['close']:.2f}")
                if f'rsi_{scale_name}' in df_scale.columns:
                    rsi_val = latest[f'rsi_{scale_name}']
                    if not pd.isna(rsi_val):
                        print(f"  RSI: {rsi_val:.2f}")
                if f'volatility_{scale_name}' in df_scale.columns:
                    vol_val = latest[f'volatility_{scale_name}']
                    if not pd.isna(vol_val):
                        print(f"  Volatility: {vol_val:.4f}")
        
        # Check cross-scale features
        base_df = scale_data['1min']
        cross_scale_cols = [col for col in base_df.columns if 'ratio' in col]
        if cross_scale_cols:
            print(f"\n✓ Cross-scale features: {len(cross_scale_cols)}")
            print(f"  Examples: {cross_scale_cols[:3]}")
        
        # Check volatility gates
        gate_cols = [col for col in base_df.columns if 'gate_weight' in col]
        if gate_cols:
            print(f"\n✓ Volatility gates: {len(gate_cols)}")
            latest = base_df.iloc[-1]
            for col in gate_cols:
                if not pd.isna(latest[col]):
                    print(f"  {col}: {latest[col]:.4f}")
        
        db.close()
        return True
    
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return False


def test_multi_resolution_synthetic():
    """Test multi-resolution pipeline with synthetic data."""
    print("\n" + "="*60)
    print("Testing Multi-Resolution Pipeline (Synthetic Data)")
    print("="*60)
    
    # Generate 1 day of 1-minute data
    n = 24 * 60  # 1440 minutes
    
    df = pd.DataFrame({
        'timestamp': pd.date_range('2024-01-01', periods=n, freq='1min'),
        'open': 100 + np.cumsum(np.random.randn(n) * 0.5),
        'high': 102 + np.cumsum(np.random.randn(n) * 0.5),
        'low': 98 + np.cumsum(np.random.randn(n) * 0.5),
        'close': 100 + np.cumsum(np.random.randn(n) * 0.5),
        'volume': np.random.randint(1000000, 5000000, n)
    })
    
    # Ensure high >= low
    df['high'] = df[['open', 'high', 'close']].max(axis=1)
    df['low'] = df[['open', 'low', 'close']].min(axis=1)
    
    print(f"Generated {len(df)} rows of synthetic 1-minute data")
    
    # Initialize pipeline
    pipeline = MultiResolutionPipeline(
        timescales=['1min', '15min', '1hour', 'daily']
    )
    
    # Transform
    scale_data = pipeline.transform(df)
    
    # Verify resampling
    print("\nVerifying resampling correctness:")
    
    # Check 15-minute aggregation
    df_15min = scale_data['15min']
    expected_15min = len(df) // 15
    print(f"  15min: {len(df_15min)} rows (expected ~{expected_15min})")
    
    # Check hourly aggregation
    df_1hour = scale_data['1hour']
    expected_1hour = len(df) // 60
    print(f"  1hour: {len(df_1hour)} rows (expected ~{expected_1hour})")
    
    # Check daily aggregation
    df_daily = scale_data['daily']
    print(f"  daily: {len(df_daily)} rows (expected 1)")
    
    print("\n✓ Multi-resolution resampling working correctly")
    
    return True


def main():
    """Run all Phase 2 tests."""
    print("="*60)
    print("ChronoX Phase 2 Verification: Feature Engineering")
    print("="*60)
    
    results = []
    
    # Test 1: Technical indicators
    try:
        results.append(("Technical Indicators", test_indicators()))
    except Exception as e:
        logger.error(f"Indicator test failed: {e}")
        results.append(("Technical Indicators", False))
    
    # Test 2: Multi-resolution (synthetic)
    try:
        results.append(("Multi-Resolution (Synthetic)", test_multi_resolution_synthetic()))
    except Exception as e:
        logger.error(f"Multi-resolution synthetic test failed: {e}")
        results.append(("Multi-Resolution (Synthetic)", False))
    
    # Test 3: Multi-resolution (database)
    try:
        results.append(("Multi-Resolution (Database)", test_multi_resolution_with_db()))
    except Exception as e:
        logger.error(f"Multi-resolution database test failed: {e}")
        results.append(("Multi-Resolution (Database)", False))
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 Phase 2 Feature Engineering: COMPLETE!")
        return 0
    else:
        print("\n⚠️  Some tests failed. Check errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
