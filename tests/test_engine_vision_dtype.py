# -*- coding: utf-8 -*-
"""tests/test_engine_vision_dtype.py — A12 视觉塔 dtype 开关与 dtype 桥接

覆盖：
  ① OCRMIT_VISION_DTYPE 归一（auto/fp32/bf16/空/非法）
  ② bf16 模式完全 no-op（子模块未被替换、参数仍是模型 dtype）
  ③ fp32 模式 sam/vision/projector 三个子模块被桥接，且 **projector 输出 dtype
     == 模型 dtype**（scatter 进 bf16 inputs_embeds 的边界）
  ④ 桥接对张量实参（含 kwargs / list）与返回值都做转换
  ⑤ auto 策略选择：CPU→_AUTO_VISION_DTYPE_CPU、CUDA→bf16

②③④ 需要 torch（nn.Module 构造）；门禁环境没有 torch，因此这些用例在无 torch
时 skip——用 PYTHONPATH 指向 .venv 后全量可跑（见文件末尾说明）。
①⑤ 与 import 期无关，任何环境都跑。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ocr_engine  # noqa: E402
from ocr_engine import OcrEngine  # noqa: E402

_HAS_TORCH = importlib.util.find_spec("torch") is not None
_requires_torch = unittest.skipUnless(
    _HAS_TORCH, "需要 torch（PYTHONPATH 指向 .venv 的 site-packages）")


class ModeNormalizeTest(unittest.TestCase):
    """① 设置值归一。"""

    def test_valid_modes(self):
        for raw, want in (("auto", "auto"), ("fp32", "fp32"), ("bf16", "bf16"),
                          ("FP32", "fp32"), ("  Bf16  ", "bf16")):
            self.assertEqual(ocr_engine._normalize_vision_dtype_mode(raw), want)

    def test_invalid_falls_back_to_auto(self):
        for raw in (None, "", "int8", "float32", "1", object()):
            self.assertEqual(ocr_engine._normalize_vision_dtype_mode(raw), "auto")

    def test_env_constant(self):
        self.assertEqual(ocr_engine.VISION_DTYPE_ENV, "OCRMIT_VISION_DTYPE")
        self.assertIn(ocr_engine._AUTO_VISION_DTYPE_CPU, ("fp32", "bf16"))


class AutoPolicyTest(unittest.TestCase):
    """⑤ auto 在 CPU/CUDA 上的选择。"""

    def test_auto_cpu_uses_measured_constant(self):
        want = ocr_engine._AUTO_VISION_DTYPE_CPU
        self.assertEqual(ocr_engine._resolve_vision_dtype("auto", "cpu"), want)
        self.assertEqual(ocr_engine._resolve_vision_dtype("AUTO", "cpu"), want)

    def test_auto_cuda_stays_bf16(self):
        for dev in ("cuda", "cuda:0", "mps", "xpu"):
            self.assertEqual(ocr_engine._resolve_vision_dtype("auto", dev), "bf16")

    def test_explicit_wins_over_device(self):
        self.assertEqual(ocr_engine._resolve_vision_dtype("fp32", "cuda"), "fp32")
        self.assertEqual(ocr_engine._resolve_vision_dtype("bf16", "cpu"), "bf16")

    def test_illegal_mode_behaves_like_auto(self):
        self.assertEqual(ocr_engine._resolve_vision_dtype("nonsense", "cpu"),
                         ocr_engine._resolve_vision_dtype("auto", "cpu"))


class _FakeVisionRoot:
    """② 之后用：三个视觉子模块 + bf16 的 image_newline 参数。"""

    @staticmethod
    def build(torch):
        nn = torch.nn

        class Sam(nn.Module):
            def __init__(self):
                super().__init__()
                self.lin = nn.Linear(4, 4, bias=False)

            def forward(self, x):
                return self.lin(x)

        class Vision(nn.Module):
            def __init__(self):
                super().__init__()
                self.lin = nn.Linear(4, 4, bias=False)

            def forward(self, x, patch_embeds=None):
                return self.lin(x), patch_embeds

        class Projector(nn.Module):
            def __init__(self):
                super().__init__()
                self.lin = nn.Linear(8, 4, bias=False)

            def forward(self, x):
                return self.lin(x)

        class Root(nn.Module):
            def __init__(self):
                super().__init__()
                self.sam_model = Sam().to(torch.bfloat16)
                self.vision_model = Vision().to(torch.bfloat16)
                self.projector = Projector().to(torch.bfloat16)
                self.image_newline = nn.Parameter(
                    torch.zeros(4, dtype=torch.bfloat16))

        return Root()


@_requires_torch
class BridgeTest(unittest.TestCase):
    def setUp(self):
        import torch
        self.torch = torch
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_vision_dtype_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = _FakeVisionRoot.build(torch)
        self.eng = OcrEngine(self.tmp)
        self.eng.model = self.root
        self.eng.device = "cpu"
        ocr_engine._ORIG_TENSOR_CUDA = None

    def tearDown(self):
        ocr_engine._ORIG_TENSOR_CUDA = None

    def _param_dtypes(self, module):
        return {p.dtype for p in module.parameters()}

    def test_bf16_is_complete_noop(self):
        """② 子模块对象未被替换，参数仍是 bf16。"""
        before = {n: id(getattr(self.root, n))
                  for n in ocr_engine._VISION_MODULE_NAMES}
        info = self.eng.set_vision_dtype("bf16")
        self.assertTrue(info["applied"])
        self.assertEqual(info["dtype"], "bf16")
        for name in ocr_engine._VISION_MODULE_NAMES:
            self.assertEqual(id(getattr(self.root, name)), before[name],
                             f"{name} 在 bf16 模式不应被替换")
            self.assertEqual(self._param_dtypes(getattr(self.root, name)),
                             {self.torch.bfloat16})
        self.assertEqual(self.eng.vision_dtype, "bf16")

    def test_fp32_bridges_all_three(self):
        """③ 三个子模块都被桥接，参数转 fp32，projector 出口=模型 dtype。"""
        info = self.eng.set_vision_dtype("fp32")
        self.assertTrue(info["applied"])
        self.assertEqual(info["dtype"], "fp32")
        bridge_cls = ocr_engine._dtype_bridge_cls()
        for name in ocr_engine._VISION_MODULE_NAMES:
            mod = getattr(self.root, name)
            self.assertIsInstance(mod, bridge_cls, f"{name} 应被 _DtypeBridge 包裹")
            self.assertEqual(self._param_dtypes(mod.inner), {self.torch.float32})
        proj = self.root.projector
        self.assertEqual(proj.out_dtype, self.eng._model_dtype())
        self.assertEqual(self.root.sam_model.out_dtype, self.torch.float32)
        self.assertEqual(self.root.vision_model.out_dtype, self.torch.float32)

    def test_fp32_chain_keeps_embedding_dtype(self):
        """③ 集成：bf16 输入 → 视觉塔 fp32 → projector 出口回 bf16 → scatter 不炸。"""
        torch = self.torch
        self.eng.set_vision_dtype("fp32")
        patches = torch.zeros(1, 4, dtype=torch.bfloat16)
        sam_out = self.root.sam_model(patches)
        self.assertEqual(sam_out.dtype, torch.float32)
        vis_out, passthrough = self.root.vision_model(patches, sam_out)
        self.assertEqual(vis_out.dtype, torch.float32)
        self.assertEqual(passthrough.dtype, torch.float32)
        cat = torch.cat([vis_out, sam_out], dim=-1)
        self.assertEqual(cat.dtype, torch.float32)
        proj = self.root.projector(cat)
        self.assertEqual(proj.dtype, self.torch.bfloat16)
        embeds = torch.zeros(1, 4, dtype=torch.bfloat16)
        embeds.masked_scatter_(torch.ones(1, 4, dtype=torch.bool), proj)
        self.assertEqual(embeds.dtype, torch.bfloat16)
        # 与 bf16 的 image_newline 拼接也不炸
        out = torch.cat([proj, self.root.image_newline[None, :]], dim=0)
        self.assertEqual(out.dtype, self.torch.bfloat16)

    def test_bridge_converts_tensor_args_and_kwargs(self):
        """④ 入口转 fp32（含 kwargs / list），出口转 out_dtype。"""
        torch = self.torch
        seen = {}

        class Probe(torch.nn.Module):
            def forward(self, *args, **kwargs):
                seen["args"] = [a.dtype if isinstance(a, torch.Tensor) else a
                                for a in args]
                seen["extra"] = kwargs["extra"].dtype
                seen["nested"] = [x.dtype for x in kwargs["nested"]]
                return args[0] * 1  # fp32

        bridge = ocr_engine._dtype_bridge_cls()(Probe(),
                                               torch.bfloat16)
        x = torch.zeros(2, dtype=torch.bfloat16)
        out = bridge(x, extra=torch.zeros(2, dtype=torch.bfloat16),
                     nested=[torch.zeros(1, dtype=torch.bfloat16)])
        self.assertEqual(seen["args"], [torch.float32])
        self.assertEqual(seen["extra"], torch.float32)
        self.assertEqual(seen["nested"], [torch.float32])
        self.assertEqual(out.dtype, torch.bfloat16)

    def test_switch_back_restores_originals(self):
        torch = self.torch
        original = {n: getattr(self.root, n) for n in ocr_engine._VISION_MODULE_NAMES}
        self.eng.set_vision_dtype("fp32")
        self.eng.set_vision_dtype("bf16")
        for name in ocr_engine._VISION_MODULE_NAMES:
            self.assertIs(getattr(self.root, name), original[name])
            self.assertEqual(self._param_dtypes(original[name]),
                             {torch.bfloat16})
        self.assertEqual(self.eng.vision_dtype, "bf16")

    def test_idempotent_switch_does_not_nest_bridges(self):
        bridge_cls = ocr_engine._dtype_bridge_cls()
        self.eng.set_vision_dtype("fp32")
        first = self.root.sam_model
        self.eng.set_vision_dtype("fp32")
        self.assertIs(self.root.sam_model, first)
        self.assertNotIsInstance(first.inner, bridge_cls)

    def test_no_model_is_safe(self):
        eng = OcrEngine(self.tmp)
        info = eng.set_vision_dtype("fp32")
        self.assertFalse(info["applied"])
        self.assertEqual(eng.vision_dtype, "bf16")

    def test_env_var_is_read_on_demand(self):
        old = os.environ.get(ocr_engine.VISION_DTYPE_ENV)
        os.environ[ocr_engine.VISION_DTYPE_ENV] = "fp32"
        try:
            info = self.eng.set_vision_dtype(None)
        finally:
            if old is None:
                os.environ.pop(ocr_engine.VISION_DTYPE_ENV, None)
            else:
                os.environ[ocr_engine.VISION_DTYPE_ENV] = old
        self.assertEqual(info["mode"], "fp32")
        self.assertEqual(info["dtype"], "fp32")
        self.assertEqual(self.eng.vision_dtype, "fp32")

    def test_apply_from_env_logs_strategy(self):
        import applog
        logs = []
        old = applog.log_model
        applog.log_model = logs.append
        try:
            self.eng._apply_vision_dtype_from_env()
        finally:
            applog.log_model = old
        self.assertTrue(any("视觉塔 dtype" in m for m in logs), logs)


class NoTorchContractTest(unittest.TestCase):
    """无 torch 时的导入契约：模块可 import，helper 不需要 torch。"""

    def test_helpers_are_torch_free(self):
        self.assertTrue(callable(ocr_engine._normalize_vision_dtype_mode))
        self.assertTrue(callable(ocr_engine._resolve_vision_dtype))
        self.assertTrue(callable(ocr_engine._dtype_bridge_cls))

    def test_engine_init_does_not_need_torch(self):
        tmp = Path(tempfile.mkdtemp(prefix="ocr_vision_dtype2_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        eng = OcrEngine(tmp)
        self.assertEqual(eng.vision_dtype, "bf16")


if __name__ == "__main__":
    unittest.main()
