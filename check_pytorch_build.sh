#!/bin/bash
# Monitor PyTorch build progress

# Check for the latest build log
BUILD_LOG="/tmp/pytorch_build4.log"
if [ ! -f "$BUILD_LOG" ]; then
    BUILD_LOG="/tmp/pytorch_build3.log"
fi

echo "=== PyTorch Build Monitor ==="
echo "Build log: $BUILD_LOG"
echo ""

if pgrep -f "setup.py develop" > /dev/null; then
    echo "✅ Build is RUNNING"
    echo ""
    echo "Last 20 lines of build output:"
    tail -20 "$BUILD_LOG"
    echo ""
    echo "Build progress (compiled objects):"
    grep -o "\[[0-9]*/[0-9]*\]" "$BUILD_LOG" | tail -1
else
    echo "⏸️  Build process not found"
    echo ""
    echo "Checking if build completed:"
    if tail -50 "$BUILD_LOG" | grep -q "Successfully installed"; then
        echo "✅ BUILD COMPLETED SUCCESSFULLY!"
        echo ""
        echo "Test PyTorch with:"
        echo "  cd ~/Documents/GITHUB/ChronoX"
        echo "  .venv/bin/python -c 'import torch; print(torch.cuda.is_available())'"
    elif tail -50 "$BUILD_LOG" | grep -q "error:"; then
        echo "❌ BUILD FAILED"
        echo ""
        echo "Last error:"
        tail -50 "$BUILD_LOG" | grep -A 5 "error:"
    else
        echo "⏳ Status unclear, check log file"
    fi
fi

echo ""
echo "To monitor in real-time:"
echo "  tail -f $BUILD_LOG"
