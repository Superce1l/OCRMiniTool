# -*- coding: utf-8 -*-
"""tests/test_cli.py — 无头 CLI（cli.py）的纯函数与主流程

约束（与 tests/run_tests.py 的总约束一致）：
  • 不加载模型、不 import torch、不联网、不起 HTTP server；
  • 所有落盘都在临时目录里，不碰仓库的 outputs/ 与 settings.json；
  • 需要「推理」的用例一律注入假引擎（main(..., engine_factory=...)）。

覆盖：parse_pages 各种形式与越界、collect_inputs 目录/递归/过滤/空目录、
build_arg_parser 关键参数、--version、无模型退出码 2、--json 契约、
部分失败退出码 3、PDF 分块兜底、crop-mode 映射、status、--lang、--quiet。
"""
import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from version import APP_VERSION  # noqa: E402

# 1x1 透明 PNG（stdlib only，避免测试依赖 Pillow）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


def _make_png(parent, name="a.png") -> Path:
    p = Path(parent) / name
    p.write_bytes(PNG_1PX)
    return p


@contextlib.contextmanager
def _capture():
    """同时捕获 stdout/stderr（cli 在调用时才取 sys.stdout，故 redirect 有效）。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


_UNSET = object()


class StubEngine:
    """假引擎：不加载模型，只按预设脚本返回结果，并记录调用参数。"""

    def __init__(self, pages=None, fail_names=(), calls=None, ready=True):
        self.ready = ready
        self.pages = list(pages) if pages is not None else ["# 标题\n\n正文"]
        self.fail_names = set(fail_names)
        self.calls = [] if calls is None else calls
        self.loaded_device = None
        self.cancel_event = None

    def load(self, device_pref="auto", cb=None):
        self.loaded_device = device_pref
        return None

    def recognize(self, image_paths, prompt="", crop_mode=_UNSET):
        names = [Path(p).name for p in image_paths]
        kwargs = {"prompt": prompt}
        if crop_mode is not _UNSET:          # 只记录 CLI 真正传了的参数
            kwargs["crop_mode"] = crop_mode
        self.calls.append({"images": names, "kwargs": kwargs})
        if any(n in self.fail_names for n in names):
            raise RuntimeError("stub failure")
        # 页数默认与输入页数一致（多页合并请求的语义），显式 pages 多于一页时以它为准
        mds = list(self.pages)
        if len(mds) == 1 and len(image_paths) > 1:
            mds = mds * len(image_paths)
        return {
            "text": "\n\n".join(mds),
            "pages": mds,
            "elapsed": 0.5,
            "truncated": False,
            "pagesMismatch": False,
            "groups": 1,
        }


class CapEngine(StubEngine):
    """声明了未来引擎参数的假引擎：用于验证 CLI「支持就透传」的分支。"""

    def recognize(self, image_paths, prompt="", crop_mode=None,
                  max_page_tokens=0, max_merged_pages=0, drop_headers=False):
        self.calls.append({"images": [Path(p).name for p in image_paths],
                           "kwargs": {"prompt": prompt, "crop_mode": crop_mode,
                                      "max_page_tokens": max_page_tokens,
                                      "max_merged_pages": max_merged_pages,
                                      "drop_headers": drop_headers}})
        per = len(image_paths) or 1
        return {"text": "x", "pages": ["md"] * per, "elapsed": 0.1,
                "truncated": True, "pagesMismatch": False, "groups": 1}


# ══════════════════════════════════════════════════════════════════
class ParsePagesTest(unittest.TestCase):
    def test_single_page(self):
        self.assertEqual(cli.parse_pages("5"), [5])

    def test_closed_range(self):
        self.assertEqual(cli.parse_pages("1-3"), [1, 2, 3])

    def test_mixed_with_open_range(self):
        self.assertEqual(cli.parse_pages("1-3,5,10-", 12), [1, 2, 3, 5, 10, 11, 12])

    def test_open_range_needs_total(self):
        with self.assertRaises(cli.CliError):
            cli.parse_pages("10-")

    def test_clamped_to_total(self):
        # 「越界夹取」：超出的页号/区间端点夹到 total，去重后不重复
        self.assertEqual(cli.parse_pages("1-3,5", 3), [1, 2, 3])
        self.assertEqual(cli.parse_pages("10-", 5), [5])

    def test_dedup_and_order(self):
        self.assertEqual(cli.parse_pages("2,2,1-2"), [1, 2])

    def test_fullwidth_comma(self):
        self.assertEqual(cli.parse_pages("1，2"), [1, 2])

    def test_empty_means_no_filter(self):
        self.assertEqual(cli.parse_pages(""), [])
        self.assertEqual(cli.parse_pages(None), [])

    def test_invalid_forms_raise(self):
        for bad in ("abc", "3-1", "0", "-3", "1--2", "1-abc"):
            with self.subTest(spec=bad):
                with self.assertRaises(cli.CliError):
                    cli.parse_pages(bad)

    def test_zero_total_yields_empty(self):
        self.assertEqual(cli.parse_pages("1-3", 0), [])


class CollectInputsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.a = _make_png(self.root, "a.png")
        self.b = _make_png(self.root, "b.JPG")
        (self.root / "c.txt").write_text("x", encoding="utf-8")
        (self.root / "sub").mkdir()
        self.d = _make_png(self.root / "sub", "d.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_file(self):
        self.assertEqual(cli.collect_inputs(self.a), [self.a])

    def test_dir_non_recursive(self):
        self.assertEqual(cli.collect_inputs(self.root), [self.a, self.b])

    def test_dir_recursive_includes_subdir(self):
        got = cli.collect_inputs(self.root, recursive=True)
        self.assertEqual(got, [self.a, self.b, self.d])

    def test_extension_filter(self):
        self.assertEqual(cli.collect_inputs(self.root, exts={".txt"}),
                         [self.root / "c.txt"])

    def test_empty_dir_returns_empty(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(cli.collect_inputs(empty), [])

    def test_unsupported_single_file_raises(self):
        with self.assertRaises(cli.CliError):
            cli.collect_inputs(self.root / "c.txt")

    def test_missing_path_raises(self):
        with self.assertRaises(cli.CliError):
            cli.collect_inputs(self.root / "nope.png")

    def test_error_message_follows_lang(self):
        with self.assertRaises(cli.CliError) as ctx:
            cli.collect_inputs(self.root / "nope.png", lang="en")
        self.assertIn("not found", str(ctx.exception))


class ArgParserTest(unittest.TestCase):
    def test_defaults(self):
        a = cli.build_arg_parser().parse_args([])
        self.assertIsNone(a.input)
        self.assertIsNone(a.cmd)
        self.assertIsNone(a.format)
        self.assertEqual(a.device, "auto")
        # crop_mode 缺省必须是 None（= 沿用引擎旧行为），不能是 "auto"
        self.assertIsNone(a.crop_mode)
        self.assertFalse(a.recursive)
        self.assertFalse(a.json)
        self.assertEqual(a.pages, "")

    def test_all_options(self):
        a = cli.build_arg_parser().parse_args([
            "--input", "x", "--recursive", "--prompt", "p", "--out", "o",
            "--format", "json", "--device", "cpu", "--max-page-tokens", "100",
            "--max-merged-pages", "8", "--crop-mode", "gundam", "--drop-headers",
            "--pages", "1-3", "--json", "--quiet", "--verbose", "--lang", "en",
        ])
        self.assertEqual(a.input, "x")
        self.assertTrue(a.recursive)
        self.assertEqual(a.prompt, "p")
        self.assertEqual(a.out, "o")
        self.assertEqual(a.format, "json")
        self.assertEqual(a.device, "cpu")
        self.assertEqual(a.max_page_tokens, 100)
        self.assertEqual(a.max_merged_pages, 8)
        self.assertEqual(a.crop_mode, "gundam")
        self.assertTrue(a.drop_headers)
        self.assertEqual(a.pages, "1-3")
        self.assertTrue(a.json)
        self.assertTrue(a.quiet)
        self.assertTrue(a.verbose)
        self.assertEqual(a.lang, "en")

    def test_status_subcommand(self):
        a = cli.build_arg_parser().parse_args(["status"])
        self.assertEqual(a.cmd, "status")

    def test_status_accepts_common_flags_before_and_after(self):
        self.assertTrue(cli.build_arg_parser().parse_args(["status", "--json"]).json)
        a = cli.build_arg_parser().parse_args(["--json", "status"])
        self.assertEqual(a.cmd, "status")
        self.assertTrue(a.json)

    def test_bad_choice_is_param_error(self):
        with _capture() as (out, err):
            rc = cli.main(["--input", "x", "--crop-mode", "nope"])
        self.assertEqual(rc, cli.EXIT_PARAM)
        self.assertIn("invalid choice", err.getvalue())


class VersionTest(unittest.TestCase):
    def test_version_matches_app_version(self):
        with _capture() as (out, err):
            rc = cli.main(["--version"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(out.getvalue().strip(), APP_VERSION)
        self.assertEqual(err.getvalue(), "")


class EnvErrorTest(unittest.TestCase):
    def test_preflight_failure_returns_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with mock.patch.object(cli, "_preflight",
                                   return_value="模型文件不完整或未下载"):
                with _capture() as (out, err):
                    rc = cli.main(["--input", str(png), "--json"])
        self.assertEqual(rc, cli.EXIT_ENV)
        self.assertIn("模型文件不完整", err.getvalue())

    def test_load_failure_returns_2(self):
        class BadLoadEngine(StubEngine):
            def load(self, device_pref="auto", cb=None):
                raise RuntimeError("boom-load")

        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--json"],
                              engine_factory=lambda: BadLoadEngine())
        self.assertEqual(rc, cli.EXIT_ENV)
        self.assertIn("boom-load", err.getvalue())

    def test_not_ready_engine_returns_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--json"],
                              engine_factory=lambda: StubEngine(ready=False))
        self.assertEqual(rc, cli.EXIT_ENV)


class JsonModeTest(unittest.TestCase):
    def test_stdout_is_one_parseable_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--json"],
                              engine_factory=lambda: StubEngine(pages=["A", "B"]))
            payload = json.loads(out.getvalue())
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(len(payload["results"]), 1)
        item = payload["results"][0]
        for key in ("source", "pages", "elapsed", "truncated",
                    "pagesMismatch", "outPath", "error"):
            self.assertIn(key, item)
        self.assertEqual(item["groups"], 1)             # 引擎分组数（int）
        self.assertEqual([p["page"] for p in item["pages"]], [1, 2])
        self.assertIsNone(item["outPath"])

    def test_single_result_fields_mirrored_to_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with _capture() as (out, err):
                cli.main(["--input", str(png), "--json"],
                         engine_factory=lambda: StubEngine())
            payload = json.loads(out.getvalue())
        self.assertEqual(payload["source"], str(png))
        self.assertEqual(len(payload["pages"]), 1)
        self.assertIn("truncated", payload)
        self.assertIn("pagesMismatch", payload)

    def test_json_mode_writes_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            out_dir = Path(tmp) / "out"
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--json",
                               "--out", str(out_dir)],
                              engine_factory=lambda: StubEngine())
            self.assertEqual(rc, cli.EXIT_OK)
            self.assertFalse(out_dir.exists())

    def test_json_with_format_writes_file_and_keeps_stdout_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            out_dir = Path(tmp) / "out"
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--json", "--format", "md",
                               "--out", str(out_dir)],
                              engine_factory=lambda: StubEngine(pages=["# H\nbody"]))
            payload = json.loads(out.getvalue())
            files = sorted(p.name for p in out_dir.iterdir())
            content = (out_dir / "a.md").read_text(encoding="utf-8")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(files, ["a.md"])
        self.assertEqual(content.strip(), "# H\nbody")
        self.assertTrue(payload["results"][0]["outPath"].endswith("a.md"))

    def test_partial_failure_returns_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = _make_png(tmp, "good.png")
            bad = _make_png(tmp, "bad.png")
            with _capture() as (out, err):
                rc = cli.main(["--input", str(Path(tmp)), "--json"],
                              engine_factory=lambda: StubEngine(fail_names={"bad.png"}))
            payload = json.loads(out.getvalue())
        self.assertEqual(rc, cli.EXIT_PARTIAL)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["summary"]["failed"], 1)
        errs = [r for r in payload["results"] if r["error"]]
        self.assertEqual(len(errs), 1)
        self.assertIn("stub failure", errs[0]["error"])


class FormatTest(unittest.TestCase):
    def test_txt_strips_markdown_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            out_dir = Path(tmp) / "out"
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--format", "txt",
                               "--out", str(out_dir)],
                              engine_factory=lambda: StubEngine(pages=["## 标题\n**粗**"]))
            text = (out_dir / "a.txt").read_text(encoding="utf-8")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertNotIn("#", text)
        self.assertNotIn("**", text)
        self.assertIn("标题", text)

    def test_md_multipage_has_page_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            out_dir = Path(tmp) / "out"
            with _capture() as (out, err):
                cli.main(["--input", str(png), "--out", str(out_dir)],
                         engine_factory=lambda: StubEngine(pages=["一", "二"]))
            text = (out_dir / "a.md").read_text(encoding="utf-8")
        self.assertIn("<!-- page 1 -->", text)
        self.assertIn("<!-- page 2 -->", text)

    def test_format_json_writes_structured_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            out_dir = Path(tmp) / "out"
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--format", "json",
                               "--out", str(out_dir)],
                              engine_factory=lambda: StubEngine())
            data = json.loads((out_dir / "a.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(data["source"], str(png))
        self.assertEqual(data["pages"][0]["page"], 1)


class NonPdfPagesWarnTest(unittest.TestCase):
    def test_pages_on_image_warns_but_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--pages", "1", "--json"],
                              engine_factory=lambda: StubEngine())
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("--pages", err.getvalue())


class ChunkAndCapabilityTest(unittest.TestCase):
    def _fake_pdf(self, tmp):
        pdf = Path(tmp) / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub\n")
        return pdf

    def test_host_side_chunking_when_engine_lacks_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._fake_pdf(tmp)
            fake_pages = [(1, Path(tmp) / "p1.png"), (2, Path(tmp) / "p2.png"),
                          (3, Path(tmp) / "p3.png")]
            stub = StubEngine()
            with mock.patch.object(cli, "_expand_pdf", return_value=fake_pages):
                with _capture() as (out, err):
                    rc = cli.main(["--input", str(pdf), "--max-merged-pages", "2",
                                   "--json"],
                                  engine_factory=lambda: stub)
            payload = json.loads(out.getvalue())
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(len(stub.calls), 2)          # 2 页 + 1 页
        self.assertEqual([len(c["images"]) for c in stub.calls], [2, 1])
        self.assertEqual([p["page"] for p in payload["results"][0]["pages"]],
                         [1, 2, 3])

    def test_passthrough_when_engine_declares_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._fake_pdf(tmp)
            fake_pages = [(1, Path(tmp) / "p1.png"), (2, Path(tmp) / "p2.png")]
            engine = CapEngine()
            with mock.patch.object(cli, "_expand_pdf", return_value=fake_pages):
                with _capture() as (out, err):
                    rc = cli.main(["--input", str(pdf), "--max-merged-pages", "2",
                                   "--max-page-tokens", "500", "--drop-headers",
                                   "--json"],
                                  engine_factory=lambda: engine)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(len(engine.calls), 1)          # 引擎自己分块 → 只调一次
        self.assertEqual(engine.calls[0]["kwargs"]["max_merged_pages"], 2)
        self.assertEqual(engine.calls[0]["kwargs"]["max_page_tokens"], 500)
        self.assertIs(engine.calls[0]["kwargs"]["drop_headers"], True)

    def test_crop_mode_auto_passes_string_not_none(self):
        """--crop-mode auto 必须原样传 "auto"（引擎据此判断长截图），不得塌缩成 None。"""
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            stub = StubEngine()
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--crop-mode", "auto", "--json"],
                              engine_factory=lambda: stub)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(stub.calls[0]["kwargs"]["crop_mode"], "auto")

    def test_crop_mode_omitted_keeps_legacy_behavior(self):
        """不传 --crop-mode → 不给引擎 crop_mode kwarg（引擎默认 None=旧行为）。"""
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            stub = StubEngine()
            with _capture() as (out, err):
                cli.main(["--input", str(png), "--json"],
                         engine_factory=lambda: stub)
        self.assertNotIn("crop_mode", stub.calls[0]["kwargs"])

    def test_crop_mode_reaches_engine(self):
        for choice in ("gundam", "base"):
            with self.subTest(choice=choice):
                with tempfile.TemporaryDirectory() as tmp:
                    png = _make_png(tmp)
                    stub = StubEngine()
                    with _capture() as (out, err):
                        cli.main(["--input", str(png), "--crop-mode", choice,
                                  "--json"], engine_factory=lambda: stub)
                self.assertEqual(stub.calls[0]["kwargs"]["crop_mode"], choice)

    def test_unsupported_flags_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--json",
                               "--max-page-tokens", "100", "--drop-headers"],
                              engine_factory=lambda: StubEngine())
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("max-page-tokens", err.getvalue())
        self.assertIn("drop-headers", err.getvalue())


class StatusTest(unittest.TestCase):
    def test_status_human(self):
        with _capture() as (out, err):
            rc = cli.main(["status"])
        self.assertEqual(rc, cli.EXIT_OK)
        text = out.getvalue()
        self.assertIn(APP_VERSION, text)
        self.assertIn("models", text.replace("\\", "/"))

    def test_status_json(self):
        with _capture() as (out, err):
            rc = cli.main(["status", "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual(rc, cli.EXIT_OK)
        for key in ("ok", "version", "modelDir", "modelPresent", "torch",
                    "transformers", "pymupdf", "runtimeFlavor", "cuda"):
            self.assertIn(key, data)
        self.assertEqual(data["version"], APP_VERSION)


class LangTest(unittest.TestCase):
    def test_english_messages(self):
        with _capture() as (out, err):
            rc = cli.main(["--input", "definitely-missing.png", "--lang", "en"])
        self.assertEqual(rc, cli.EXIT_PARAM)
        self.assertIn("input not found", err.getvalue())

    def test_chinese_default(self):
        with _capture() as (out, err):
            rc = cli.main(["--input", "definitely-missing.png"])
        self.assertEqual(rc, cli.EXIT_PARAM)
        self.assertIn("输入不存在", err.getvalue())

    def test_env_var_lang(self):
        with mock.patch.dict("os.environ", {"OCRMT_LANG": "en"}):
            with _capture() as (out, err):
                rc = cli.main(["--input", "definitely-missing.png"])
        self.assertEqual(rc, cli.EXIT_PARAM)
        self.assertIn("input not found", err.getvalue())


class QuietTest(unittest.TestCase):
    def test_quiet_suppresses_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(tmp)
            with _capture() as (out, err):
                rc = cli.main(["--input", str(png), "--quiet", "--json"],
                              engine_factory=lambda: StubEngine())
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(err.getvalue(), "")
        json.loads(out.getvalue())

    def test_quiet_still_prints_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = _make_png(tmp, "bad.png")
            with _capture() as (out, err):
                rc = cli.main(["--input", str(bad), "--quiet", "--json"],
                              engine_factory=lambda: StubEngine(fail_names={"bad.png"}))
        self.assertEqual(rc, cli.EXIT_PARTIAL)
        self.assertIn("stub failure", err.getvalue())


class NoGuiTest(unittest.TestCase):
    def test_cli_source_has_no_gui_imports(self):
        """静态检查：cli.py 里不得出现 pywebview/clr/HTTP server 的 import。"""
        src = Path(cli.__file__).read_text(encoding="utf-8")
        for banned in ("import pywebview", "import clr", "from pywebview",
                       "import webview_server", "from webview_server",
                       "import app_webview", "from app_webview"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, src)

    def test_import_cli_pulls_no_gui_or_server(self):
        """子进程里只 import cli，确认 GUI/服务端模块没有被拉进来。"""
        import subprocess
        code = (
            "import sys; import cli; "
            "bad=[m for m in ('pywebview','clr','webview_server','app_webview') "
            "if m in sys.modules]; print(','.join(bad))"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           cwd=str(Path(cli.__file__).resolve().parent),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((r.stdout or "").strip(), "")

    def test_guide_facing_helpers_are_pure(self):
        # 纯函数在无引擎、无设置文件的情况下也应可调用
        self.assertEqual(cli._groups_count(3), 3)
        self.assertEqual(cli._groups_count(None), 0)
        self.assertEqual(cli._msg("en", "err.inputMissing").startswith("missing"),
                         True)
        self.assertTrue(callable(cli.build_arg_parser))


if __name__ == "__main__":
    unittest.main()
