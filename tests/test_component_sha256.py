# -*- coding: utf-8 -*-
"""tests/test_component_sha256.py — D5 组件包整包 sha256 校验（warn-only / strict）

全部本地：起一个 127.0.0.1 的 http.server 喂假 zip / .sha256 / manifest.json，
不访问外网；解压器用记录器替代，不真正解压假 zip。

覆盖：
  • 期望哈希三种来源的优先级与容错：flavor.sha256 → <zip>.sha256 → manifest.json
  • 正确哈希：校验通过 + 安装流程不变（标记照写、解压器拿到最终 zip）
  • 错误哈希（warn-only）：写 WARN（含 expected/got）后继续安装
  • 错误哈希（strict）：删 zip/分卷并抛 RuntimeError（不再解压）
  • 拿不到期望值：warn 跳过；strict 也不删包（不因「没有哈希」删文件）
  • 分卷：拼接后的整包哈希校验（与发布整包一致）
  • 发布侧旁路文件：build_components.write_sidecars 的 <zip>.sha256 / SHA256SUMS
"""
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_components  # noqa: E402
import downloader  # noqa: E402
import hashlib  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Server:
    """最小本地静态文件服务器（支持 GET/HEAD，不打印日志）。"""

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):       # noqa: D401 - 静默
            pass

    def __init__(self, root: Path):
        handler = functools.partial(self._Handler, directory=str(root))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


class _Base(unittest.TestCase):
    FLAVOR = "cpu"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_sha_"))
        self.www = self.tmp / "www"
        self.www.mkdir()
        self.rt = self.tmp / "runtime"
        self.rt.mkdir()
        self.srv = _Server(self.www)
        self.zip_name = downloader._TORCH_FLAVORS[self.FLAVOR]["zip"]
        self.zip_url = f"{self.srv.url}/{self.zip_name}"
        self.extracted: list[Path] = []
        self.extracted_bytes: bytes | None = None
        self.log = mock.Mock()
        self._patches = [
            mock.patch.object(downloader, "RUNTIME_DIR", self.rt),
            mock.patch.object(downloader, "_TORCH_REL_BASE", self.srv.url),
            mock.patch.object(downloader, "_REMOTE_HASH_DOWN", False),
            mock.patch.object(downloader, "_extract_zip_checked", self._fake_extract),
            mock.patch.dict(downloader._TORCH_FLAVORS[self.FLAVOR], {"sha256": ""}),
            mock.patch("applog.log_download", self.log),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.srv.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_extract(self, zip_path, dest, progress_cb=None, cancel_check=None):
        self.extracted.append(Path(zip_path))
        self.extracted_bytes = Path(zip_path).read_bytes()

    # ── helpers ──────────────────────────────────────────────────
    def serve_zip(self, data: bytes):
        (self.www / self.zip_name).write_bytes(data)
        return data

    def serve_sha256(self, text: str):
        (self.www / (self.zip_name + ".sha256")).write_text(text, encoding="utf-8")

    def serve_manifest(self, payload: dict):
        (self.www / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def set_flavor_sha(self, sha: str):
        downloader._TORCH_FLAVORS[self.FLAVOR]["sha256"] = sha

    def logs(self) -> list[str]:
        return [c.args[0] if c.args else "" for c in self.log.call_args_list]

    def assert_log(self, needle: str):
        joined = "\n".join(self.logs())
        self.assertIn(needle, joined, f"日志里应包含 {needle!r}；实际：{joined}")

    def marker(self) -> Path:
        return self.rt / downloader._TORCH_MARKER

    def install(self, expect_raise=None):
        if expect_raise is not None:
            with self.assertRaises(expect_raise):
                downloader.download_torch(self.FLAVOR, force=True)
            return
        downloader.download_torch(self.FLAVOR, force=True)


FAKE_ZIP = b"FAKEZIP\x00" + bytes(range(64))


class SinglePartShaTest(_Base):
    def test_normalize_sha256_accepts_sha256sum_line(self):
        for flavor in ("cpu", "cu128"):
            self.assertIn("sha256", downloader._TORCH_FLAVORS[flavor],
                          "flavor 表应保留可选 sha256 字段（空=跳过）")
        h = _sha(FAKE_ZIP)
        self.assertEqual(downloader.normalize_sha256(f"{h}  {self.zip_name}\n"), h)
        self.assertEqual(downloader.normalize_sha256(h.upper()), h)
        self.assertIsNone(downloader.normalize_sha256("not-a-hash"))
        self.assertIsNone(downloader.normalize_sha256(""))
        self.assertIsNone(downloader.normalize_sha256(None))

    def test_correct_flavor_hash_passes_and_installs(self):
        data = self.serve_zip(FAKE_ZIP)
        self.set_flavor_sha(_sha(data))
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assertTrue(self.marker().is_file(), "校验通过后应照常安装")
        self.assertEqual(self.extracted, [self.rt / self.zip_name])
        self.assert_log("组件校验通过")
        self.assert_log("source=flavor")
        self.assertFalse(any("WARN 组件 sha256" in m for m in self.logs()))

    def test_wrong_flavor_hash_warn_only_still_installs(self):
        data = self.serve_zip(FAKE_ZIP)
        wrong = "0" * 64
        self.set_flavor_sha(wrong)
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assertTrue(self.marker().is_file(), "warn-only 不应阻断安装")
        self.assertEqual(self.extracted, [self.rt / self.zip_name])
        self.assert_log("WARN 组件 sha256 不匹配")
        self.assert_log(wrong[:16])
        self.assert_log(_sha(data)[:16])

    def test_wrong_flavor_hash_strict_deletes_zip_and_raises(self):
        self.serve_zip(FAKE_ZIP)
        self.set_flavor_sha("1" * 64)
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "strict"}):
            with self.assertRaises(RuntimeError) as ctx:
                downloader.download_torch(self.FLAVOR, force=True)
        self.assertIn("组件包校验失败", str(ctx.exception))
        self.assertFalse((self.rt / self.zip_name).exists(), "strict 应删除 zip")
        self.assertEqual(self.extracted, [], "校验失败不得解压")
        self.assertFalse(self.marker().is_file(), "校验失败不得写安装标记")
        self.assert_log("strict 模式已删除")

    def test_remote_sha256_correct_passes(self):
        data = self.serve_zip(FAKE_ZIP)
        self.serve_sha256(_sha(data) + "  " + self.zip_name + "\n")
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assertTrue(self.marker().is_file())
        self.assert_log("组件校验通过")
        self.assert_log("source=remote .sha256")

    def test_remote_sha256_invalid_content_warns_but_installs(self):
        self.serve_zip(FAKE_ZIP)
        self.serve_sha256("this is not a hash\n")
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assertTrue(self.marker().is_file(), "非法内容只 WARN 不阻断")
        self.assert_log("不是 64 位 hex")
        self.assert_log("组件校验跳过")

    def test_missing_hash_sources_warn_but_install(self):
        self.serve_zip(FAKE_ZIP)
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assertTrue(self.marker().is_file(), "拿不到期望值不应阻断")
        self.assert_log("拿不到期望 sha256")

    def test_manifest_flat_sha_passes(self):
        data = self.serve_zip(FAKE_ZIP)
        self.serve_manifest({self.zip_name: {"sha256": _sha(data), "size": len(data)}})
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assert_log("组件校验通过")
        self.assert_log("source=manifest.json")

    def test_manifest_nested_zips_sha_passes(self):
        data = self.serve_zip(FAKE_ZIP)
        self.serve_manifest({"zips": {self.zip_name: {"sha256": _sha(data)}}})
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assert_log("source=manifest.json")

    def test_priority_flavor_sha_beats_remote(self):
        data = self.serve_zip(FAKE_ZIP)
        self.serve_sha256(_sha(data))               # 远端正确
        self.set_flavor_sha("2" * 64)               # flavor 错误（应优先采用）
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assert_log("WARN 组件 sha256 不匹配")
        self.assert_log("source=flavor")
        self.assertFalse(any("组件校验通过" in m for m in self.logs()))

    def test_strict_without_expected_hash_does_not_delete(self):
        data = self.serve_zip(FAKE_ZIP)
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "strict"}):
            self.install()                          # 不抛：拿不到期望值只 WARN
        self.assertTrue(self.marker().is_file())
        self.assert_log("组件校验跳过")
        self.assertFalse(any("strict 模式已删除" in m for m in self.logs()))
        # 正常安装完成后 zip 也会被删，这里只确认没有因校验而提前删
        self.assertEqual(self.extracted, [self.rt / self.zip_name])


class MultipartShaTest(_Base):
    FLAVOR = "cu128"

    def _serve_parts(self):
        data = FAKE_ZIP * 3
        half = len(data) // 2
        p1, p2 = data[:half], data[half:]
        (self.www / (self.zip_name + ".part01-of-2")).write_bytes(p1)
        (self.www / (self.zip_name + ".part02-of-2")).write_bytes(p2)
        return data

    def test_multipart_correct_whole_hash_passes(self):
        data = self._serve_parts()
        self.serve_sha256(_sha(data))
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assertTrue(self.marker().is_file())
        self.assertEqual(self.extracted, [self.rt / self.zip_name])
        self.assertEqual(self.extracted_bytes, data,
                         "拼接结果应为两卷完整内容")
        self.assert_log("组件校验通过")
        self.assert_log("source=remote .sha256")
        leftovers = [p.name for p in self.rt.iterdir() if ".part" in p.name]
        self.assertEqual(leftovers, [], "拼接/安装后不应残留分卷或暂存")

    def test_multipart_bad_hash_strict_deletes_and_raises(self):
        self._serve_parts()
        self.serve_sha256("3" * 64)
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "strict"}):
            with self.assertRaises(RuntimeError):
                downloader.download_torch(self.FLAVOR, force=True)
        self.assertFalse((self.rt / self.zip_name).exists(), "strict 应删除拼接后的 zip")
        self.assertEqual(self.extracted, [], "校验失败不得解压")
        self.assertFalse(self.marker().is_file())

    def test_multipart_missing_hash_warns_but_installs(self):
        self._serve_parts()
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        self.assertTrue(self.marker().is_file())
        self.assert_log("拿不到期望 sha256")

    def test_partial_hash_source_not_used_for_whole_zip(self):
        """分卷资产：<zip>.sha256 对应的是**整包**哈希，不是单卷哈希。"""
        data = self._serve_parts()
        half_sha = _sha(data[: len(data) // 2])      # 单卷哈希（不应被采用）
        self.serve_sha256(half_sha)
        with mock.patch.dict(os.environ, {"OCRMT_VERIFY_RUNTIME": "warn"}):
            self.install()
        # 因为发布侧给的是整包哈希，这里不匹配 → warn，但仍完成安装
        self.assertTrue(self.marker().is_file())
        self.assert_log("WARN 组件 sha256 不匹配")


class SidecarTest(unittest.TestCase):
    def test_write_sidecars_outputs_sha256_and_sums(self):
        with tempfile.TemporaryDirectory(prefix="ocr_sidecar_") as d:
            out = Path(d)
            zdata = b"zip-bytes"
            (out / "a.zip").write_bytes(zdata)
            manifest = {"a.zip": {"sha256": _sha(zdata), "size": len(zdata)}}
            (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            written = build_components.write_sidecars(out, manifest)

            sha_line = (out / "a.zip.sha256").read_text(encoding="utf-8")
            self.assertEqual(sha_line, f"{_sha(zdata)}  a.zip\n")
            sums = (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            self.assertIn(f"{_sha(zdata)}  a.zip", sums)
            man_sha = hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
            self.assertIn(f"{man_sha}  manifest.json", sums)
            self.assertEqual(sorted(p.name for p in written),
                             ["SHA256SUMS", "a.zip.sha256"])


if __name__ == "__main__":
    unittest.main()
