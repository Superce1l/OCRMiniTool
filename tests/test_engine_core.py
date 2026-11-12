# -*- coding: utf-8 -*-
"""tests/test_engine_core.py — ocr_engine 的取消钩子 / 幂等加载 / 探测缓存

覆盖 docs/explore/01-engine-perf.md §7 的引擎侧低风险改动：
  A5  stream_end 收尾调用不再抛 OcrCancelledError（不掩盖 finally 里的真实异常）
  A10 load() 幂等：设备/目录未变不重复加载 6.69 GB；设备或目录变化、unload 后照常重建
  A11 device_info() 5s 缓存，且「torch 可见性 / 组件安装标记」变化（含 runtime
      组件中途装好）时立即失效——否则界面一直显示「未装 torch」

不依赖 torch/transformers：用 sys.modules['torch'] 桩 + 假 transformers 模块。
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import applog                      # noqa: E402
import downloader                  # noqa: E402
import ocr_engine                  # noqa: E402
from ocr_engine import EngineError, OcrCancelledError, OcrEngine  # noqa: E402

_ORIG_TORCH = object()


class _FakeTensor:
    def cuda(self, *args, **kwargs):        # CPU 补丁把 .cuda() 变成 no-op
        return self


class _FakeCuda:
    def __init__(self, available: bool = False):
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def get_device_name(self, index=0):
        return "Fake GPU"

    def get_device_properties(self, index=0):
        return types.SimpleNamespace(total_memory=8 * 10 ** 9)


def make_fake_torch(available: bool = False):
    m = types.ModuleType("torch")
    m.__version__ = "2.8.0"
    m.Tensor = _FakeTensor
    m.bfloat16 = "bfloat16"
    m.cuda = _FakeCuda(available)
    m.version = types.SimpleNamespace(cuda="12.8" if available else None)
    m.inference_mode = contextlib.nullcontext
    return m


class _FakeTokenizer:
    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        return cls()


class _FakeTextStreamer:
    def __init__(self, *args, **kwargs):
        self.text = ""

    def put(self, value):
        self.text += str(value)

    def end(self):
        pass

    def on_finalized_text(self, text, stream_end=False):
        pass


class _FakeModel:
    def __init__(self, reply=None):
        self.reply = reply if reply is not None else "fake"
        self.to_device = None

    def eval(self):
        return self

    def to(self, device):
        self.to_device = device
        return self

    def infer(self, tokenizer, **kwargs):
        if callable(self.reply):
            return self.reply(**kwargs)
        return self.reply


class _FakeAutoModel:
    calls = 0

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        cls.calls += 1
        return _FakeModel()


class _FakeTransformers:
    __version__ = "4.52.1"
    AutoTokenizer = _FakeTokenizer
    AutoModel = _FakeAutoModel
    TextStreamer = _FakeTextStreamer


class _EngineTestBase(unittest.TestCase):
    """统一管理 torch 桩 / 假 transformers / 全局缓存的状态复原。"""

    def setUp(self):
        self._had_torch = "torch" in sys.modules
        self._old_torch = sys.modules.get("torch")
        sys.modules["torch"] = make_fake_torch(available=False)

        self._old_transformers = ocr_engine._transformers
        # 版本属性供 tr()/日志使用；直接注入可绕开「本机没装 transformers」
        ocr_engine._transformers = _FakeTransformers()

        self._old_quick_check = ocr_engine.quick_check
        ocr_engine.quick_check = lambda model_dir: True     # 目录内容与本用例无关

        self._old_orig_cuda = ocr_engine._ORIG_TENSOR_CUDA
        ocr_engine._ORIG_TENSOR_CUDA = None
        ocr_engine._invalidate_device_info_cache()
        ocr_engine.OcrEngine._torch_fail_at = 0.0
        _FakeAutoModel.calls = 0

        self.model_dir = Path(tempfile.mkdtemp(prefix="ocr_engine_test_"))

    def tearDown(self):
        if self._had_torch:
            if self._old_torch is not None:
                sys.modules["torch"] = self._old_torch
            else:
                sys.modules.pop("torch", None)
        else:
            sys.modules.pop("torch", None)
        ocr_engine._transformers = self._old_transformers
        ocr_engine.quick_check = self._old_quick_check
        ocr_engine._ORIG_TENSOR_CUDA = self._old_orig_cuda
        ocr_engine._invalidate_device_info_cache()
        ocr_engine.OcrEngine._torch_fail_at = 0.0
        if hasattr(ocr_engine, "_STREAM_HOOK"):
            ocr_engine._STREAM_HOOK.update(cancel_event=None, state=None)
        import shutil
        shutil.rmtree(self.model_dir, ignore_errors=True)


class StreamHookTest(_EngineTestBase):
    """A5：取消只发生在解码过程中。"""

    def _hook(self):
        base = _FakeTextStreamer
        return ocr_engine._make_hooked_streamer(base)()

    def test_cancel_mid_decode_raises(self):
        event = threading.Event()
        event.set()
        state = {"chars": 0}
        ocr_engine._STREAM_HOOK.update(cancel_event=event, state=state)
        hook = self._hook()
        import io
        with contextlib.redirect_stdout(io.StringIO()):    # 钩子会打印取消提示
            with self.assertRaises(OcrCancelledError):
                hook.on_finalized_text("正文")

    def test_cancel_at_stream_end_does_not_raise(self):
        """stream_end=True 是 finally 里的收尾调用，不能在这里抛。"""
        event = threading.Event()
        event.set()
        state = {"chars": 0}
        ocr_engine._STREAM_HOOK.update(cancel_event=event, state=state)
        hook = self._hook()
        hook.on_finalized_text("", stream_end=True)     # 不得抛
        # 收尾时仍要累计剩余可见文字（界面字数不能丢）
        hook.on_finalized_text("尾巴", stream_end=True)
        self.assertEqual(state["chars"], 2)

    def test_no_cancel_keeps_counting(self):
        state = {"chars": 0}
        ocr_engine._STREAM_HOOK.update(cancel_event=threading.Event(), state=state)
        hook = self._hook()
        hook.on_finalized_text("<PAGE><BLOCK>ab")
        self.assertEqual(state["chars"], 2)            # 标签不计入可见字数


class LoadIdempotentTest(_EngineTestBase):
    """A10：口径未变不重复加载；口径变化必须重建。"""

    def _engine(self):
        return OcrEngine(self.model_dir)

    def test_second_load_is_skipped(self):
        eng = self._engine()
        eng.load(device_pref="cpu")
        self.assertTrue(eng.ready)
        self.assertEqual(eng.device, "cpu")
        self.assertEqual(_FakeAutoModel.calls, 1)
        eng.load(device_pref="cpu")                    # 幂等：不再读权重
        self.assertEqual(_FakeAutoModel.calls, 1)
        self.assertTrue(eng.ready)

    def test_force_reloads(self):
        eng = self._engine()
        eng.load(device_pref="cpu")
        eng.load(device_pref="cpu", force=True)
        self.assertEqual(_FakeAutoModel.calls, 2)

    def test_model_dir_change_reloads(self):
        eng = self._engine()
        eng.load(device_pref="cpu")
        other = Path(tempfile.mkdtemp(prefix="ocr_engine_test_other_"))
        try:
            eng.model_dir = other
            eng.load(device_pref="cpu")
            self.assertEqual(_FakeAutoModel.calls, 2)
            self.assertEqual(eng._loaded_dir, other)
        finally:
            import shutil
            shutil.rmtree(other, ignore_errors=True)

    def test_unload_then_load_rebuilds(self):
        eng = self._engine()
        eng.load(device_pref="cpu")
        eng.unload()
        self.assertFalse(eng.ready)
        self.assertIsNone(eng._loaded_dir)
        eng.load(device_pref="cpu")
        self.assertEqual(_FakeAutoModel.calls, 2)

    def test_device_change_is_not_a_blind_return(self):
        """auto 在有 CUDA 时解析成 cuda：与已加载的 cpu 不同，必须重建。"""
        sys.modules["torch"] = make_fake_torch(available=True)
        eng = self._engine()
        eng.load(device_pref="cpu")
        self.assertEqual(eng.device, "cpu")
        eng.load(device_pref="auto")                   # 目标 cuda ≠ 现有 cpu
        self.assertEqual(_FakeAutoModel.calls, 2)
        self.assertEqual(eng.device, "cuda")

    def test_load_requires_model_files(self):
        ocr_engine.quick_check = lambda model_dir: False
        with self.assertRaises(EngineError):
            self._engine().load(device_pref="cpu")


class DeviceInfoCacheTest(_EngineTestBase):
    """A11：缓存命中/失效（TTL、torch 可见性、安装标记、显式作废）。"""

    def _cached_probe(self, eng, hits):
        info = {"torch": "2.8.0", "cuda": False, "cudaName": "", "vramGb": 0.0,
                "cudaBuild": "", "transformers": "4.52.1"}

        def probe():
            hits.append(time.time())
            return dict(info)
        eng._device_info_probe = probe             # 实例属性遮蔽方法
        return info

    def test_cache_hit_within_ttl(self):
        eng = OcrEngine(self.model_dir)
        hits = []
        self._cached_probe(eng, hits)
        eng.device_info()
        eng.device_info()
        eng.device_info()
        self.assertEqual(len(hits), 1)

    def test_ttl_expiry_reprobes(self):
        eng = OcrEngine(self.model_dir)
        hits = []
        self._cached_probe(eng, hits)
        old = ocr_engine._DEVICE_INFO_TTL
        ocr_engine._DEVICE_INFO_TTL = 0.0
        try:
            eng.device_info()
            eng.device_info()
        finally:
            ocr_engine._DEVICE_INFO_TTL = old
        self.assertEqual(len(hits), 2)

    def test_invalidate_forces_reprobe(self):
        eng = OcrEngine(self.model_dir)
        hits = []
        self._cached_probe(eng, hits)
        eng.device_info()
        ocr_engine._invalidate_device_info_cache()
        eng.device_info()
        self.assertEqual(len(hits), 2)

    def test_returned_dict_is_a_copy(self):
        eng = OcrEngine(self.model_dir)
        hits = []
        self._cached_probe(eng, hits)
        info = eng.device_info()
        info["cuda"] = True                        # 改返回值不得污染缓存
        again = eng.device_info()
        self.assertFalse(again["cuda"])
        self.assertEqual(len(hits), 1)

    def test_torch_visibility_in_key(self):
        eng = OcrEngine(self.model_dir)
        hits = []
        self._cached_probe(eng, hits)
        with mock.patch.object(ocr_engine, "_torch_visible", return_value=False):
            eng.device_info()
        with mock.patch.object(ocr_engine, "_torch_visible", return_value=True):
            eng.device_info()
        self.assertEqual(len(hits), 2)

    def test_component_install_invalidates_cache(self):
        """runtime 组件中途装好 → _ensure_runtime_path 必须作废探测缓存。"""
        eng = OcrEngine(self.model_dir)
        hits = []
        self._cached_probe(eng, hits)
        eng.device_info()                          # 先缓存「未装 torch」
        self.assertEqual(len(hits), 1)

        tmp = Path(tempfile.mkdtemp(prefix="ocr_runtime_test_"))
        (tmp / "torch").mkdir()
        (tmp / "torch_build.json").write_text(
            json.dumps({"flavor": "cpu", "torch": "2.8.0"}), encoding="utf-8")
        old_dl_runtime = downloader.RUNTIME_DIR
        old_eng_runtime = ocr_engine.RUNTIME_DIR
        downloader.RUNTIME_DIR = tmp
        ocr_engine.RUNTIME_DIR = tmp
        try:
            ocr_engine._ensure_runtime_path()      # 装上组件的判定点
            self.assertIsNone(ocr_engine._DEVICE_INFO_CACHE["value"])
            eng.device_info()                       # 失效后必然重新探测
            self.assertEqual(len(hits), 2)
        finally:
            downloader.RUNTIME_DIR = old_dl_runtime
            ocr_engine.RUNTIME_DIR = old_eng_runtime
            try:
                sys.path.remove(str(tmp))
            except ValueError:
                pass
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            ocr_engine._invalidate_device_info_cache()

    def test_marker_change_invalidates_without_helper(self):
        """安装标记 mtime/size 变化即视为环境变了（双保险：不依赖显式作废）。"""
        eng = OcrEngine(self.model_dir)
        hits = []
        self._cached_probe(eng, hits)
        with mock.patch.object(ocr_engine, "_torch_marker_sig", return_value=(1, 10)):
            eng.device_info()
        with mock.patch.object(ocr_engine, "_torch_marker_sig", return_value=(2, 10)):
            eng.device_info()
        self.assertEqual(len(hits), 2)

    def test_frozen_negative_cache_does_not_crash(self):
        """回归：旧代码在冻结环境引用模块级名 device_info._torch_fail_at → NameError。"""
        eng = OcrEngine(self.model_dir)
        had = hasattr(sys, "frozen")
        old = getattr(sys, "frozen", None)
        setattr(sys, "frozen", True)
        saved_torch = sys.modules.pop("torch", None)     # 走「torch 不可导入」路径
        fake_spec = types.SimpleNamespace(origin="C:/fake-venv/torch/__init__.py")
        try:
            with mock.patch("importlib.util.find_spec", return_value=fake_spec), \
                    mock.patch.object(ocr_engine, "_torch",
                                      side_effect=EngineError("boom")), \
                    mock.patch.object(applog, "log_error"):
                info = eng.device_info()               # 不得抛 NameError
                self.assertIsInstance(info, dict)
                self.assertIsNone(info["torch"])
                self.assertGreater(ocr_engine.OcrEngine._torch_fail_at, 0.0)
                ocr_engine._invalidate_device_info_cache()
                info2 = eng.device_info()              # 60s 负缓存：直接返回
                self.assertIsInstance(info2, dict)
                self.assertIsNone(info2["torch"])
        finally:
            if saved_torch is not None:
                sys.modules["torch"] = saved_torch
            if had:
                sys.frozen = old
            else:
                del sys.frozen


if __name__ == "__main__":
    unittest.main()
