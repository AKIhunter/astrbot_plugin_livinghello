"""pytest 入口：先装宿主适配层，再收集用例。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_contract import install  # noqa: E402

install()
