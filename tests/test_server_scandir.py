# -*- coding: utf-8 -*-
"""tests/test_server_scandir.py — E5 目录扫描 + /api/recognize 路径直读

真起一个 127.0.0.1 随机端口的服务（同 test_server_smoke.py 的手法），所有用户
数据（settings/outputs/logs）重定向到临时目录。

覆盖：正常递归扫描/扩展名白名单/稳定排序/recursive=false/深度与数量双限/
非法与相对路径/符号链接不跟随/扫描结果不含文件内容、/api/recognize 的
{"paths":[...]} 直读、路径校验（存在性/绝对路径/白名单/数量上限）、以及
multipart 上传链路不被破坏。
"""
import http.client
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import webview_backend as wb  # noqa: E402
import webview_server as ws  # noqa: E402


def _rmtree(p: Path):
    for f in sorted(p.rglob("*"), reverse=True):
        try:
            f.unlink() if f.is_file() else f.rmdir()
        except OSError:
            pass
    try:
        p.rmdir()
    except OSError:
        pass


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post_multipart(port: int, path: str, body: bytes,
                    content_type: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST",
        headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _multipart(fields: dict, files: list) -> tuple[bytes, str]:
    boundary = "----scandirprobe"
    parts = []
    for k, v in fields.items():
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"{k}\"\r\n\r\n{v}\r\n").encode())
    for name, fname, content in files:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"{name}\"; filename=\"{fname}\"\r\n"
                      f"Content-Type: application/octet-stream\r\n\r\n").encode())
        parts.append(content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class ScanDirTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ocr_scan_"))
        cls._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", cls.tmp / "settings.json"),
            mock.patch.object(wb, "OUTPUT_DIR", cls.tmp / "outputs"),
            mock.patch.object(applog, "_DIR", cls.tmp / "logs"),
        ]
        for p in cls._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""
        (cls.tmp / "outputs").mkdir(exist_ok=True)
        cls.server = ws.WebViewServer(port=0)
        cls.server.start()
        cls.port = cls.server.port
        assert cls.port

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        finally:
            applog._FH = None
            for p in cls._patches:
                p.stop()
            _rmtree(cls.tmp)

    def setUp(self):
        self.tree = Path(tempfile.mkdtemp(prefix="tree_", dir=str(self.tmp)))

    # ── helpers ─────────────────────────────────────────────
    def _mk(self, rel: str, data: bytes = b"x") -> Path:
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def _build_tree(self):
        self._mk("a.JPG")
        self._mk("b.PDF", b"%PDF-1.4")
        self._mk("c.txt")
        self._mk("sub/d.png")
        self._mk("sub/deep/e.jpeg")
        self._mk("sub/deep/f.gif")

    def _scan(self, **body) -> dict:
        body.setdefault("path", str(self.tree))
        code, data = _post(self.port, "/api/scan-dir", body)
        self.assertEqual(code, 200)
        return data

    # ── 正常扫描 ────────────────────────────────────────────
    def test_recursive_scan_whitelist_and_sorted(self):
        self._build_tree()
        data = self._scan(recursive=True, maxFiles=100, maxDepth=8)
        self.assertIs(data["ok"], True)
        self.assertFalse(data["truncated"])
        names = [f["name"] for f in data["files"]]
        self.assertEqual(sorted(names), ["a.JPG", "b.PDF", "d.png", "e.jpeg"])
        self.assertNotIn("c.txt", names)
        self.assertNotIn("f.gif", names)
        self.assertEqual(data["count"], 4)
        for f in data["files"]:
            self.assertEqual(set(f), {"path", "name", "size", "ext"},
                             "扫描结果只回元数据，不回传内容")
            self.assertIn(f["ext"], {".jpg", ".jpeg", ".png", ".bmp",
                                     ".webp", ".tif", ".tiff", ".pdf"})
            self.assertTrue(Path(f["path"]).is_file())
        paths = [f["path"] for f in data["files"]]
        self.assertEqual(paths, sorted(paths, key=str.lower),
                         "结果必须按路径稳定排序")

    def test_non_recursive_only_top_level(self):
        self._build_tree()
        data = self._scan(recursive=False)
        names = sorted(f["name"] for f in data["files"])
        self.assertEqual(names, ["a.JPG", "b.PDF"])

    def test_max_depth_limits_traversal(self):
        self._build_tree()
        data = self._scan(recursive=True, maxDepth=1)
        names = sorted(f["name"] for f in data["files"])
        self.assertEqual(names, ["a.JPG", "b.PDF", "d.png"],
                         "maxDepth=1 只到一层子目录")

    def test_max_files_truncates_and_flags(self):
        self._build_tree()
        data = self._scan(recursive=True, maxFiles=2)
        self.assertEqual(data["count"], 2)
        self.assertIs(data["truncated"], True)
        self.assertEqual(len(data["files"]), 2)

    def test_uppercase_extensions_accepted(self):
        self._mk("X.TIFF")
        data = self._scan(recursive=False)
        self.assertIn(".tiff", [f["ext"] for f in data["files"]])

    # ── 非法输入 ────────────────────────────────────────────
    def test_relative_path_rejected(self):
        for bad in ("relative/dir", "", "  "):
            code, data = _post(self.port, "/api/scan-dir", {"path": bad})
            self.assertEqual(code, 200, "校验失败按后端风格走 200 + ok:false")
            self.assertIs(data["ok"], False)
            self.assertTrue(data["error"])

    def test_nonexistent_and_file_path_rejected(self):
        missing = str(self.tree / "no-such-dir")
        code, data = _post(self.port, "/api/scan-dir", {"path": missing})
        self.assertIs(data["ok"], False)
        f = self._mk("plain.png")
        code, data = _post(self.port, "/api/scan-dir", {"path": str(f)})
        self.assertIs(data["ok"], False, "文件不是目录也要拒绝")

    # ── 符号链接 / 目录联接 ─────────────────────────────────
    def test_junction_is_not_followed(self):
        """Windows 目录联接（junction）不是 symlink，但同样不许跟进去。"""
        self._build_tree()
        outside = Path(tempfile.mkdtemp(prefix="outside_", dir=str(self.tmp)))
        (outside / "outside.png").write_bytes(b"png")
        junction = self.tree / "junc"
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"New-Item -ItemType Junction -Path '{junction}' "
             f"-Target '{outside}' | Out-Null"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        if r.returncode != 0 or not junction.exists():
            self.skipTest(f"当前环境无法创建目录联接：{r.stderr[:120]}")
        self.assertTrue(os.path.isjunction(junction),
                        "测试前提：对象确实是 junction")
        data = self._scan(recursive=True, maxDepth=8)
        paths = [f["path"] for f in data["files"]]
        self.assertFalse(any("outside" in p for p in paths),
                         "不能顺着目录联接走出去")
        self.assertNotIn("outside.png", [f["name"] for f in data["files"]])
        code, data = _post(self.port, "/api/scan-dir", {"path": str(junction)})
        self.assertIs(data["ok"], False, "根目录是联接应拒绝（不跟随边界）")

    def test_symlink_branch_is_skipped(self):
        """符号链接分支的逻辑覆盖（当前环境未授予 SeCreateSymbolicLink）。"""
        self._build_tree()
        target = str(self.tree / "a.JPG")
        real = wb._is_link_like
        with mock.patch.object(wb, "_is_link_like",
                               side_effect=lambda p: str(p) == target):
            data = self._scan(recursive=True, maxDepth=8)
        self.assertNotIn("a.JPG", [f["name"] for f in data["files"]],
                         "被判为链接的条目必须跳过")
        self.assertIn("b.PDF", [f["name"] for f in data["files"]])
        self.assertIs(wb._is_link_like, real, "mock 已还原")

    def test_symlinks_are_not_followed(self):
        self._build_tree()
        outside = Path(tempfile.mkdtemp(prefix="outside_", dir=str(self.tmp)))
        target = outside / "outside.png"
        target.write_bytes(b"png")
        try:
            link_dir = self.tree / "linkdir"
            os.symlink(str(outside), str(link_dir), target_is_directory=True)
            link_file = self.tree / "link.png"
            os.symlink(str(target), str(link_file))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"当前环境无法创建符号链接（{e}）")
        data = self._scan(recursive=True, maxDepth=8)
        paths = [f["path"] for f in data["files"]]
        self.assertNotIn(str(target), paths, "不能跟随文件符号链接")
        self.assertFalse(any("linkdir" in p for p in paths),
                         "不能跟随目录符号链接/联接")
        self.assertNotIn("link.png", [f["name"] for f in data["files"]])
        # 根本身是符号链接 → 直接拒绝
        code, data = _post(self.port, "/api/scan-dir",
                           {"path": str(self.tree / "linkdir")})
        self.assertIs(data["ok"], False, "根目录是链接应拒绝（不跟随边界）")

    # ── /api/recognize 路径直读 ────────────────────────────
    def test_recognize_paths_direct(self):
        p1 = self._mk("p1.png")
        p2 = self._mk("sub/p2.jpg")
        captured = {}

        def fake_recognize(opts, progress_cb=None):
            captured.update(opts)
            return {"text": "ok", "pages": ["ok"], "outPath": "x",
                    "truncated": False}

        with mock.patch.object(self.server.backend, "recognize",
                               side_effect=fake_recognize):
            code, data = _post(self.port, "/api/recognize",
                               {"paths": [str(p1), str(p2)],
                                "mode": "batch"})
        self.assertEqual(code, 200)
        self.assertEqual(data["text"], "ok")
        self.assertEqual(captured["paths"], [str(p1), str(p2)])
        self.assertEqual(captured["names"], ["p1.png", "p2.jpg"])
        self.assertEqual(captured["mode"], "batch")
        self.assertIn("等2个", captured["name"])

    def test_recognize_paths_validation(self):
        good = self._mk("good.png")
        txt = self._mk("note.txt")
        cases = [
            ({"paths": []}, 400),
            ({"paths": [str(self.tree / "missing.png")]}, 400),
            ({"paths": ["relative.png"]}, 400),
            ({"paths": [str(txt)]}, 400),
            ({"paths": ["C:/" + "/".join(["x"] * 260)]}, 400),
        ]
        for body, want in cases:
            code, data = _post(self.port, "/api/recognize", body)
            self.assertEqual(code, want, f"{body} 应被拒绝")
            self.assertIs(data["ok"], False)
            self.assertTrue(data["error"])
        # 超过路径数量上限
        too_many = {"paths": [str(good)] * (wb.MAX_RECOGNIZE_PATHS + 1)}
        code, data = _post(self.port, "/api/recognize", too_many)
        self.assertEqual(code, 400)
        self.assertIs(data["ok"], False)

    def test_recognize_multipart_still_works(self):
        """multipart 链路不因新增 JSON 分支而改变。"""
        body, ctype = _multipart({"prompt": "x"}, [])
        code, data = _post_multipart(self.port, "/api/recognize", body, ctype)
        self.assertEqual(code, 400)
        self.assertIn("files", data["error"]["message"])

        # 有文件、但模型未加载 → 仍是原来的 500 语义（说明走的是上传分支）
        img = self.tree / "upload.png"
        img.write_bytes(b"PNG")
        body, ctype = _multipart({}, [("files", "upload.png", b"PNG")])
        code, data = _post_multipart(self.port, "/api/recognize", body, ctype)
        self.assertEqual(code, 500)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
