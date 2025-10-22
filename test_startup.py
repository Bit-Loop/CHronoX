#!/usr/bin/env python3
"""
Simple test to verify ChronoX can start up.
Tests basic imports without PyTorch/heavy dependencies.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

def test_basic_imports():
    """Test basic Python imports."""
    print("🧪 Testing Basic Dependencies")
    print("=" * 80)
    
    tests = []
    
    # Core Python libraries
    try:
        import numpy as np
        tests.append(("✅", "numpy", f"version {np.__version__}"))
    except Exception as e:
        tests.append(("❌", "numpy", str(e)[:50]))
    
    try:
        import pandas as pd
        tests.append(("✅", "pandas", f"version {pd.__version__}"))
    except Exception as e:
        tests.append(("❌", "pandas", str(e)[:50]))
    
    try:
        import scipy
        tests.append(("✅", "scipy", f"version {scipy.__version__}"))
    except Exception as e:
        tests.append(("❌", "scipy", str(e)[:50]))
    
    try:
        import polars as pl
        tests.append(("✅", "polars", f"version {pl.__version__}"))
    except Exception as e:
        tests.append(("❌", "polars", str(e)[:50]))
    
    try:
        import pyarrow as pa
        tests.append(("✅", "pyarrow", f"version {pa.__version__}"))
    except Exception as e:
        tests.append(("❌", "pyarrow", str(e)[:50]))
    
    # Database clients
    try:
        import psycopg2
        tests.append(("✅", "psycopg2", f"PostgreSQL adapter"))
    except Exception as e:
        tests.append(("❌", "psycopg2", str(e)[:50]))
    
    try:
        import asyncpg
        tests.append(("✅", "asyncpg", f"Async PostgreSQL"))
    except Exception as e:
        tests.append(("❌", "asyncpg", str(e)[:50]))
    
    try:
        import redis
        tests.append(("✅", "redis", "Redis client"))
    except Exception as e:
        tests.append(("❌", "redis", str(e)[:50]))
    
    try:
        from sqlalchemy import __version__ as sa_version
        tests.append(("✅", "sqlalchemy", f"version {sa_version}"))
    except Exception as e:
        tests.append(("❌", "sqlalchemy", str(e)[:50]))
    
    # Technical indicators
    try:
        import ta
        tests.append(("✅", "ta", "Technical Analysis library"))
    except Exception as e:
        tests.append(("❌", "ta", str(e)[:50]))
    
    # Configuration
    try:
        import pydantic
        tests.append(("✅", "pydantic", f"version {pydantic.__version__}"))
    except Exception as e:
        tests.append(("❌", "pydantic", str(e)[:50]))
    
    try:
        import yaml
        tests.append(("✅", "yaml", "YAML support"))
    except Exception as e:
        tests.append(("❌", "yaml", str(e)[:50]))
    
    # Testing
    try:
        import pytest
        tests.append(("✅", "pytest", f"version {pytest.__version__}"))
    except Exception as e:
        tests.append(("❌", "pytest", str(e)[:50]))
    
    # Code quality
    try:
        import black
        tests.append(("✅", "black", "Code formatter"))
    except Exception as e:
        tests.append(("❌", "black", str(e)[:50]))
    
    # Print results
    for status, module, description in tests:
        print(f"{status} {module:15s} {description}")
    
    print("\n" + "=" * 80)
    
    # Summary
    passed = sum(1 for t in tests if t[0] == "✅")
    failed = sum(1 for t in tests if t[0] == "❌")
    
    print(f"\nPassed: {passed}/{len(tests)}")
    print(f"Failed: {failed}/{len(tests)}")
    
    if failed == 0:
        print("\n🎉 All basic dependencies installed!")
        return 0
    else:
        print("\n⚠️  Some dependencies missing.")
        return 1


def test_project_structure():
    """Test that key project directories exist."""
    print("\n\n📁 Testing Project Structure")
    print("=" * 80)
    
    directories = [
        "data",
        "data/ingestion",
        "data/preprocessing",
        "data/storage",
        "models",
        "models/transformers",
        "models/rl_agents",
        "models/ensembles",
        "training",
        "backtesting",
        "monitoring",
        "utils",
        "scripts",
        "tests",
    ]
    
    tests = []
    for dir_path in directories:
        full_path = project_root / dir_path
        if full_path.exists():
            tests.append(("✅", dir_path, f"{len(list(full_path.glob('*.py')))} Python files"))
        else:
            tests.append(("❌", dir_path, "Missing"))
    
    for status, dir_name, description in tests:
        print(f"{status} {dir_name:30s} {description}")
    
    passed = sum(1 for t in tests if t[0] == "✅")
    total = len(tests)
    
    print(f"\nDirectories found: {passed}/{total}")
    
    return 0 if passed == total else 1


def main():
    """Main entry point."""
    print("\n" + "=" * 80)
    print("ChronoX Startup Test")
    print("=" * 80 + "\n")
    
    try:
        result1 = test_basic_imports()
        result2 = test_project_structure()
        
        if result1 == 0 and result2 == 0:
            print("\n✨ ChronoX is ready to go!")
            print("\nNext steps:")
            print("1. Install PyTorch: pip install torch")
            print("2. Run specific module tests")
            print("3. Start data ingestion")
            return 0
        else:
            return 1
            
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted")
        return 130
    except Exception as e:
        print(f"\n❌ Error during testing: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
