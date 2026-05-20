#!/usr/bin/env python3
"""
FTSeek-US Test Harness Runner
"""
import sys
import os
import pytest

# Add project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

def main():
    print("=" * 60)
    print("Starting FTSeek-US Test Harness...")
    print("=" * 60)
    
    harness_dir = os.path.join(os.path.dirname(__file__), "scenarios")
    
    # Run tests
    exit_code = pytest.main([
        harness_dir,
        "-v",
        "--tb=short"
    ])
    
    if exit_code == 0:
        print("\n✅ All Tests Passed!")
    else:
        print(f"\n❌ Tests Failed (Code: {exit_code})")
    
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
