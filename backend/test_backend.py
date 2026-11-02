"""Compatibility entrypoint for the deterministic suite (no live-site probes)."""

import sys
import unittest
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    unittest.main(module=None, argv=[sys.argv[0], "discover", "-s", str(root / "tests"), "-v"])
