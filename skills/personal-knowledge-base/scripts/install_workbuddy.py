#!/usr/bin/env python3
"""Compatibility entry point; invoke from an authenticated, clean Git clone."""
import sys
sys.dont_write_bytecode = True
from install_lifecycle import main, capability_probe, capability_ready

if __name__ == '__main__':
    sys.exit(main())
