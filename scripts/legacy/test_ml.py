#!/usr/bin/env python3
"""
Test PyTorch installation and GPU availability.
"""

import sys


def test_pytorch():
    """Test PyTorch installation."""
    print("🔥 Testing PyTorch Installation")
    print("=" * 80)
    
    try:
        import torch
        print(f"✅ PyTorch version: {torch.__version__}")
        print(f"✅ PyTorch compiled with CUDA: {torch.version.cuda if hasattr(torch.version, 'cuda') else 'N/A'}")
        
        # Check CUDA availability
        cuda_available = torch.cuda.is_available()
        print(f"{'✅' if cuda_available else '⚠️ '} CUDA available: {cuda_available}")
        
        if cuda_available:
            print(f"✅ CUDA device count: {torch.cuda.device_count()}")
            print(f"✅ Current CUDA device: {torch.cuda.current_device()}")
            print(f"✅ CUDA device name: {torch.cuda.get_device_name(0)}")
        else:
            print("ℹ️  No GPU detected - will use CPU (this is fine for development)")
        
        # Test basic tensor operations
        x = torch.randn(3, 3)
        y = torch.randn(3, 3)
        z = x @ y
        print(f"✅ Basic tensor operations working")
        
        # Test autograd
        x = torch.ones(2, 2, requires_grad=True)
        y = x + 2
        z = y * y * 3
        out = z.mean()
        out.backward()
        print(f"✅ Autograd working")
        
        return True
        
    except Exception as e:
        print(f"❌ PyTorch test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_transformers():
    """Test Transformers library."""
    print("\n\n🤗 Testing Transformers Library")
    print("=" * 80)
    
    try:
        import transformers
        print(f"✅ Transformers version: {transformers.__version__}")
        
        # Test basic tokenizer
        from transformers import AutoTokenizer
        print(f"✅ Can import AutoTokenizer")
        
        return True
        
    except Exception as e:
        print(f"❌ Transformers test failed: {e}")
        return False


def test_ml_libraries():
    """Test other ML libraries."""
    print("\n\n🤖 Testing ML Libraries")
    print("=" * 80)
    
    tests = []
    
    # XGBoost
    try:
        import xgboost as xgb
        tests.append(("✅", "XGBoost", xgb.__version__))
    except Exception as e:
        tests.append(("❌", "XGBoost", str(e)[:50]))
    
    # LightGBM
    try:
        import lightgbm as lgb
        tests.append(("✅", "LightGBM", lgb.__version__))
    except Exception as e:
        tests.append(("❌", "LightGBM", str(e)[:50]))
    
    # CatBoost
    try:
        import catboost
        tests.append(("✅", "CatBoost", catboost.__version__))
    except Exception as e:
        tests.append(("❌", "CatBoost", str(e)[:50]))
    
    # Gymnasium
    try:
        import gymnasium as gym
        tests.append(("✅", "Gymnasium", gym.__version__))
    except Exception as e:
        tests.append(("❌", "Gymnasium", str(e)[:50]))
    
    # Stable Baselines3
    try:
        import stable_baselines3 as sb3
        tests.append(("✅", "Stable-Baselines3", sb3.__version__))
    except Exception as e:
        tests.append(("❌", "Stable-Baselines3", str(e)[:50]))
    
    # Optuna
    try:
        import optuna
        tests.append(("✅", "Optuna", optuna.__version__))
    except Exception as e:
        tests.append(("❌", "Optuna", str(e)[:50]))
    
    for status, name, info in tests:
        print(f"{status} {name:20s} {info}")
    
    passed = sum(1 for t in tests if t[0] == "✅")
    return passed == len(tests)


def main():
    """Main entry point."""
    print("\n" + "=" * 80)
    print("ChronoX ML Dependencies Test")
    print("=" * 80 + "\n")
    
    results = []
    
    try:
        results.append(("PyTorch", test_pytorch()))
        results.append(("Transformers", test_transformers()))
        results.append(("ML Libraries", test_ml_libraries()))
        
        print("\n" + "=" * 80)
        print("Summary")
        print("=" * 80)
        
        for name, result in results:
            status = "✅ PASS" if result else "❌ FAIL"
            print(f"{status} {name}")
        
        all_passed = all(r[1] for r in results)
        
        if all_passed:
            print("\n🎉 All ML dependencies installed and working!")
            print("\nReady for:")
            print("  - Deep learning model training")
            print("  - Transformer-based models")
            print("  - Reinforcement learning")
            print("  - Gradient boosting models")
            print("  - Hyperparameter optimization")
            return 0
        else:
            print("\n⚠️  Some tests failed. Check errors above.")
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
