# -*- coding: utf-8 -*-
"""tests/run_tests.py — 一条命令跑完全部测试（零第三方依赖，不需要 pytest）

    python tests/run_tests.py            # 全部
    python tests/run_tests.py -v         # 逐条
    python tests/run_tests.py test_i18n  # 只跑某个模块

设计约束：不依赖 torch/transformers/pywebview，也不碰真实用户数据
（settings.json / outputs/ 在测试内都重定向到临时目录），因此可以在
任何 Python 3.10+ 上跑，包括没装推理引擎的 CI。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    names = [a for a in argv if not a.startswith("-")]
    loader = unittest.TestLoader()
    if names:
        suite = unittest.TestSuite()
        for name in names:
            suite.addTests(loader.loadTestsFromName(name))
    else:
        suite = loader.discover(str(HERE), pattern="test_*.py",
                                top_level_dir=str(HERE))
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1,
                                     buffer=False, durations=0)
    result = runner.run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print(f"\n{'通过' if result.wasSuccessful() else '失败'}：{total - bad}/{total} 用例通过")
    if result.failures or result.errors:
        for case, _ in (result.failures + result.errors):
            print(f"  失败：{case}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
