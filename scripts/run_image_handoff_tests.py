"""Explicit paired facade acceptance; isolated models, SQLite and platform fakes."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT.parent)]
if not os.environ.get("ASTRBOT_NATIVE_SOURCE"):
    raise SystemExit("Set ASTRBOT_NATIVE_SOURCE to the isolated AstrBot package snapshot")
from scripts.run_offline_tests import install_framework_stubs

install_framework_stubs()
result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromName("image_result_handoff"))
sys.exit(not result.wasSuccessful())
