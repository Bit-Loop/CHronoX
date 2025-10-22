#!/usr/bin/env python3
"""
Quick Install Script for Market Data Tab Dependencies

This script checks and installs all required dependencies for the
📈 Market Data tab in the ChronoX Backfill Visualizer.

Usage:
    python scripts/install_market_data_deps.py
"""

import subprocess
import sys
from pathlib import Path

def check_package(package_name):
    """Check if a Python package is installed"""
    try:
        __import__(package_name)
        return True
    except ImportError:
        return False

def install_packages(packages):
    """Install Python packages using pip"""
    print(f"\n📦 Installing {len(packages)} packages...")
    print(f"   Packages: {', '.join(packages)}\n")
    
    cmd = [sys.executable, "-m", "pip", "install"] + packages
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Installation failed:")
        print(e.stderr)
        return False

def main():
    print("="*80)
    print("ChronoX Market Data Tab - Dependency Installer")
    print("="*80)
    
    # Required packages
    required_packages = {
        'pandas': 'pandas',
        'numpy': 'numpy',
        'mplfinance': 'mplfinance',
        'websockets': 'websockets',
        'aiohttp': 'aiohttp',
        'dotenv': 'python-dotenv',
        'tradingpatterns': 'git+https://github.com/keithorange/PatternPy.git',
        'finplot': 'finplot'
    }
    
    print("\n🔍 Checking installed packages...\n")
    
    missing_packages = []
    installed_packages = []
    
    for import_name, pip_name in required_packages.items():
        if check_package(import_name):
            print(f"   ✓ {pip_name:20} - INSTALLED")
            installed_packages.append(pip_name)
        else:
            print(f"   ✗ {pip_name:20} - MISSING")
            missing_packages.append(pip_name)
    
    print(f"\n📊 Summary:")
    print(f"   Installed: {len(installed_packages)}/{len(required_packages)}")
    print(f"   Missing:   {len(missing_packages)}/{len(required_packages)}")
    
    if not missing_packages:
        print("\n✅ All dependencies are already installed!")
        print("\n🚀 You can now run: python scripts/backfill_visualizer.py")
        return 0
    
    print(f"\n⚠️  Missing packages: {', '.join(missing_packages)}")
    
    # Ask for confirmation
    response = input("\n📥 Install missing packages? [Y/n]: ").strip().lower()
    
    if response in ['', 'y', 'yes']:
        if install_packages(missing_packages):
            print("\n✅ Installation complete!")
            print("\n🚀 You can now run: python scripts/backfill_visualizer.py")
            print("   Then open the '📈 Market Data' tab")
            return 0
        else:
            print("\n❌ Installation failed. Please install manually:")
            print(f"   pip install {' '.join(missing_packages)}")
            return 1
    else:
        print("\n⏭️  Skipping installation.")
        print("\n💡 To install manually:")
        print(f"   pip install {' '.join(missing_packages)}")
        return 0

if __name__ == '__main__':
    sys.exit(main())
