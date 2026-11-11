"""根目录兼容入口，实现已移至 scripts/run_offline_tests.py。

保留本文件是为了让 CI、文档和历史命令继续可用；新增逻辑请改实现文件。
"""
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_SPEC = spec_from_file_location(
    "_livinghello_offline_runner",
    Path(__file__).resolve().parent / "scripts" / "run_offline_tests.py",
)
_RUNNER = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)

install_framework_stubs = _RUNNER.install_framework_stubs
main = _RUNNER.main

if __name__ == "__main__":
    sys.exit(main())
