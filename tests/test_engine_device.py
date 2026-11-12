# -*- coding: utf-8 -*-
"""tests/test_engine_device.py — 设备选择与显存护栏

真机实测（GTX 1660 SUPER 6144 MiB + cu128）：6.3 GB 权重**能**加载
（WDDM 允许超卖到共享内存，VRAM 峰值 5875/6144），但性能悬崖——
小图 32 s（CPU 30 s 持平）、长图 1017 s / **1.6 tok/s**，比 CPU fp32 还慢。
所以 auto 在显存不足时应直接用 CPU；用户显式选 CUDA 则尊重选择但记录日志。
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ocr_engine  # noqa: E402


class PickDeviceTest(unittest.TestCase):
    def test_cpu_preference_always_cpu(self):
        self.assertEqual(ocr_engine._pick_device("cpu", True, 24.0), ("cpu", ""))
        self.assertEqual(ocr_engine._pick_device("cpu", False, 0.0), ("cpu", ""))

    def test_auto_uses_cuda_when_enough_vram(self):
        self.assertEqual(ocr_engine._pick_device("auto", True, 8.0), ("cuda", ""))
        self.assertEqual(ocr_engine._pick_device("auto", True, 12.0), ("cuda", ""))

    def test_auto_falls_back_to_cpu_on_low_vram(self):
        """核心回归：6 GB 卡上 auto 不能选 CUDA（实测比 CPU 还慢）。"""
        for vram in (6.0, 6.4, 7.4):
            self.assertEqual(ocr_engine._pick_device("auto", True, vram),
                             ("cpu", "vram-low-auto"))

    def test_explicit_cuda_respected_with_note(self):
        """用户显式选 CUDA 时尊重选择，但要能记录「可能更慢」。"""
        self.assertEqual(ocr_engine._pick_device("cuda", True, 6.4),
                         ("cuda", "vram-low-forced"))

    def test_auto_without_cuda_is_cpu(self):
        self.assertEqual(ocr_engine._pick_device("auto", False, 0.0), ("cpu", ""))

    def test_cuda_pref_unavailable_signalled(self):
        """不可用时由 load() 转成具体错误，这里只给信号。"""
        self.assertEqual(ocr_engine._pick_device("cuda", False, 0.0),
                         ("cuda", "cuda-unavailable"))

    def test_unknown_vram_treated_as_enough(self):
        """读不到显存（0）时不要误判成不足，保持 CUDA。"""
        self.assertEqual(ocr_engine._pick_device("auto", True, 0.0), ("cuda", ""))

    def test_threshold_is_documented_value(self):
        self.assertEqual(ocr_engine._MIN_CUDA_VRAM_GB, 7.5)


class VramProbeTest(unittest.TestCase):
    def _fake_torch(self, available=True, total=6.4, boom=False):
        torch = types.SimpleNamespace()

        def _props(_i):
            if boom:
                raise RuntimeError("boom")
            return types.SimpleNamespace(total_memory=int(total * 1e9))
        torch.cuda = types.SimpleNamespace(is_available=lambda: available,
                                          get_device_properties=_props)
        return torch

    def test_reads_total_memory(self):
        self.assertEqual(ocr_engine._cuda_vram_gb(self._fake_torch(total=6.4)), 6.4)
        self.assertEqual(ocr_engine._cuda_vram_gb(self._fake_torch(total=24.0)), 24.0)

    def test_unavailable_returns_zero(self):
        self.assertEqual(ocr_engine._cuda_vram_gb(
            self._fake_torch(available=False)), 0.0)

    def test_probe_failure_returns_zero(self):
        self.assertEqual(ocr_engine._cuda_vram_gb(self._fake_torch(boom=True)), 0.0)

    def test_missing_cuda_attr_returns_zero(self):
        self.assertEqual(ocr_engine._cuda_vram_gb(types.SimpleNamespace()), 0.0)


if __name__ == "__main__":
    unittest.main()
