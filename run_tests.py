"""
Run the offline test suite -- no KAM-XL hardware required, no
third-party packages required (standard library unittest only).

    python3 run_tests.py
"""

import sys
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS_DIR = ROOT / "tests"

# tests/*.py do "from fakes import ..." (not "from tests.fakes"), so
# tests/ itself needs to be importable directly.
sys.path.insert(0, str(TESTS_DIR))

loader = unittest.TestLoader()
suite = loader.discover(
    start_dir=str(TESTS_DIR),
    pattern="test_*.py"
)

result = unittest.TextTestRunner(verbosity=2).run(suite)

sys.exit(0 if result.wasSuccessful() else 1)
