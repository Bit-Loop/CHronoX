#!/usr/bin/env python3
"""Debug pattern detection"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from scripts.backfill_historical_data import get_aggregated_bars

# Get data
df = get_aggregated_bars('AMD', '1d', limit=500, with_indicators=False)
print(f"Fetched {len(df)} bars")

# Prepare for pattern library
df_pattern = df.copy()
df_pattern.columns = [c.capitalize() if c.lower() in ['open', 'high', 'low', 'close', 'volume'] else c for c in df_pattern.columns]

print(f"\nColumns: {list(df_pattern.columns)}")
print(f"Data shape: {df_pattern.shape}")

# Test each pattern function
from tradingpatterns.tradingpatterns import (
    detect_head_shoulder,
    detect_double_top_bottom,
    detect_triangle_pattern
)

print("\n=== Testing Head & Shoulders ===")
try:
    result = detect_head_shoulder(df_pattern)
    print(f"Result type: {type(result)}")
    print(f"Result: {result}")
except Exception as e:
    print(f"Error: {e}")

print("\n=== Testing Double Top/Bottom ===")
try:
    result = detect_double_top_bottom(df_pattern)
    print(f"Result type: {type(result)}")
    if isinstance(result, pd.DataFrame):
        print(f"DataFrame shape: {result.shape}")
        print(f"Columns: {list(result.columns)}")
        print(f"Has 'double_pattern' column: {'double_pattern' in result.columns}")
        if 'double_pattern' in result.columns:
            patterns = result[result['double_pattern'].notna()]
            print(f"Detected patterns: {len(patterns)}")
    else:
        print(f"Result: {result}")
except Exception as e:
    print(f"Error: {e}")

print("\n=== Testing Triangle ===")
try:
    result = detect_triangle_pattern(df_pattern)
    print(f"Result type: {type(result)}")
    if isinstance(result, pd.DataFrame):
        print(f"DataFrame shape: {result.shape}")
        print(f"Columns: {list(result.columns)}")
    else:
        print(f"Result: {result}")
except Exception as e:
    print(f"Error: {e}")
