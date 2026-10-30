"""webview_server.py — OCRMiniTool 的本机 HTTP 服务器（纯标准库）

与 STTMiniTool 同一架构：webview/ 静态前端 + /api/* 业务端点由同一个
stdlib HTTP 服务器提供，进度/状态以 SSE（/api/events）主动推给前端。
零第三方依赖，方便 PyInstaller 打包。

安全：只绑 127.0.0.1（loopback），外部连不到、不触发防火墙提示；
Host/Origin 校验挡 DNS rebinding / 跨站 POST。

路由：
  GET  /                    → webview/index.html
  GET  /css/* /js/* …       → webview/ 静态档
  GET  /health              → {"status","modelReady"}
  GET  /api/status          → 后端状态（模型状态机 + 设备信息）
  GET  /api/settings        → 设置；POST 改设置（json patch）
  GET  /api/history         → outputs/ 最近识别结果
  GET  /api/events          → SSE：data: {"event","payload"} 进度/状态推送
  POST /api/download        → 开始下载模型（进度走 SSE dl-progress）
  POST /api/load            → 加载模型（进度走 SSE load-progress）
  POST /api/recognize       → multipart 多文件上传识别，回 {text,pages,outPath}
  POST /api/cancel          → 请求取消当前任务
  POST /api/history-read    → 读取一条历史结果的文本
  POST /api/history-delete  → 删除一条历史结果（移入回收站）
  POST /api/browse-file     → 原生「选择文件」多选对话框
  POST /api/browse-folder   → 原生「选择文件夹」对话框
  POST /api/open-output     → 打开输出文件夹
  POST /api/shutdown        → 结束进程
"""
from __future__ import annotations

import json
import queue
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from i18n import tr
from webview_backend import WebBackend


def resolve_web_dir() -> Path:
    """前端资产目录：源码用项目 webview/；PyInstaller 冻结时用 _MEIPASS。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for name in ("webview_assets", "webview"):
            d = base / name
            if (d / "index.html").is_file():
                return d
        return base / "webview_assets"
    return Path(__file__).resolve().parent / "webview"


WEB_DIR = resolve_web_dir()


def _parse_multipart_stream(rfile, boundary: bytes, out_dir: Path,
                            content_length: int, chunk_size: int = 1 << 20):
    """流式解析 multipart/form-data，文件部件直接落盘（不全量进内存）。

    与 STTMiniTool 同名函数同源；唯一差别：同名部件收集成列表
    （files: dict[name, list[(filename, Path)]]），支持「files」多文件上传。
    """
    fields: dict[str, str] = {}
    files: dict[str, list] = {}
    marker = b"--" + boundary
    delim = b"\r\n" + marker
    remaining = content_length

    def _read(n: int) -> bytes:
        nonlocal remaining
        if remaining <= 0:
            return b""
        n = min(n, remaining)
        data = rfile.read(n)
        remaining -= len(data)
        return data

    buf = b""
    keep = len(marker) - 1
    while True:
        i = buf.find(marker)
        if i >= 0:
            buf = buf[i + len(marker):]
            break
        if len(buf) > keep:
            buf = buf[-keep:]
        chunk = _read(chunk_size)
        if not chunk:
            return fields, files
        buf += chunk

    while True:
        while len(buf) < 2:
            chunk = _read(chunk_size)
            if not chunk:
                return fields, files
            buf += chunk
        if buf[:2] == b"--":
            return fields, files
        buf = buf[2:]

        while b"\r\n\r\n" not in buf:
            chunk = _read(chunk_size)
            if not chunk:
                return fields, files
            buf += chunk
        header_blob, _, buf = buf.partition(b"\r\n\r\n")
        name = filename = None
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "content-disposition":
                for tok in val.split(";"):
                    tok = tok.strip()
                    if tok.startswith("name="):
                        name = tok[5:].strip('"')
                    elif tok.startswith("filename="):
                        filename = tok[9:].strip('"')
        if name is None:
            name = ""

        sink = None
        acc: bytearray | None = None
        if filename is not None:
            ext = "".join(c for c in Path(filename).suffix
                          if c.isalnum() or c == ".")[:16]
            dest = out_dir / f"part_{sum(len(v) for v in files.values())}_upload{ext or '.bin'}"
            sink = open(dest, "wb")
            files.setdefault(name, []).append((filename, dest))
        else:
            acc = bytearray()

        def _emit(piece: bytes):
            if sink is not None:
                sink.write(piece)
            elif acc is not None:
                acc.extend(piece)

        keep = len(delim) - 1
        search_from = 0
        try:
            while True:
                i = buf.find(delim, search_from)
                if i >= 0:
                    nxt = buf[i + len(delim): i + len(delim) + 2]
                    if len(nxt) < 2:
                        chunk = _read(chunk_size)
                        if chunk:
                            buf += chunk
                            continue
                    if nxt[:2] in (b"\r\n", b"--"):
                        _emit(buf[:i])
                        buf = buf[i + len(delim):]
                        break
                    search_from = i + 1
                    continue
                if len(buf) > keep:
                    _emit(buf[:-keep])
                    buf = buf[-keep:]
                    search_from = 0
                chunk = _read(chunk_size)
                if not chunk:
                    _emit(buf)
                    buf = b""
                    break
                buf += chunk
        finally:
            if sink is not None:
                sink.close()
        if acc is not None:
            fields[name] = bytes(acc).decode("utf-8", errors="replace")


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class _EventHub:
    """SSE 广播：每个连接一个 Queue，publish() 推给全部订阅者。"""

    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, payload: dict):
        msg = {"event": event, "payload": payload}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                pass


class WebViewServer:
    """本机前端服务器。持有 WebBackend + EventHub。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.hub = _EventHub()
        self.backend = WebBackend(on_event=self.hub.publish)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._want_port = port
        self.port = port

    def start(self):
        if self._httpd:
            return
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _err(self, code, msg):
                self._json({"error": {"message": msg}}, code)

            def _read_json_body(self) -> dict:
                n = int(self.headers.get("Content-Length", 0))
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return {}

            def _check_host(self) -> bool:
                allowed = {f"127.0.0.1:{server.port}", f"localhost:{server.port}"}
                if self.headers.get("Host", "") not in allowed:
                    self._err(403, "bad host")
                    return False
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://{a}" for a in allowed}:
                    self._err(403, "bad origin")
                    return False
                return True

            # ── GET ───────────────────────────────────────────
            def do_GET(self):
                path = urlparse(self.path).path
                if (path.startswith("/api/") or path == "/health") and not self._check_host():
                    return
                if path == "/health":
                    return self._json({"status": "ok",
                                       "modelReady": bool(server.backend.engine.ready)})
                if path == "/api/status":
                    return self._json(server.backend.get_status())
                if path == "/api/settings":
                    return self._json(server.backend.get_settings())
                if path == "/api/history":
                    return self._json(server.backend.list_history())
                if path == "/api/events":
                    return self._sse()
                if path.startswith("/api/"):
                    return self._err(404, "unknown api")
                return self._static(path)

            # ── POST ──────────────────────────────────────────
            def do_POST(self):
                path = urlparse(self.path).path
                if not self._check_host():
                    return
                try:
                    if path == "/api/settings":
                        return self._json(server.backend.set_settings(self._read_json_body()))
                    if path == "/api/download":
                        return self._json(server.backend.start_download())
                    if path == "/api/load":
                        return self._json(server.backend.start_load())
                    if path == "/api/recognize":
                        return self._recognize()
                    if path == "/api/cancel":
                        return self._json({"ok": server.backend.cancel()})
                    if path == "/api/history-read":
                        body = self._read_json_body()
                        return self._json(server.backend.history_read(body.get("name", "")))
                    if path == "/api/history-delete":
                        body = self._read_json_body()
                        return self._json(server.backend.history_delete(body.get("name", "")))
                    if path == "/api/browse-folder":
                        body = self._read_json_body()
                        # 标题空 → 后端按当前语言兜底（i18n.tr）
                        return self._json(server.backend.browse_folder(
                            body.get("start", ""), body.get("title", "")))
                    if path == "/api/open-output":
                        return self._json({"ok": server.backend.open_output_dir()})
                    if path == "/api/shutdown":
                        return self._shutdown()
                    return self._err(404, "unknown api")
                except Exception as e:
                    return self._err(500, str(e))

            def _shutdown(self):
                import threading as _t

                def _kill():
                    try:
                        self._json({"ok": True})
                    except Exception:
                        pass
                    try:
                        server.stop()
                    except Exception:
                        pass
                    import os
                    os._exit(0)

                _t.Thread(target=_kill, daemon=True).start()
                return None

            def _static(self, path):
                if path in ("", "/"):
                    path = "/index.html"
                target = (WEB_DIR / path.lstrip("/")).resolve()
                if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
                    return self._err(403, "forbidden")
                if not target.is_file():
                    return self._err(404, "not found")
                data = target.read_bytes()
                ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = server.hub.subscribe()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    init = {"event": "status",
                            "payload": server.backend.get_status()}
                    self.wfile.write(f"data: {json.dumps(init, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server.hub.unsubscribe(q)

            # ── 识别（multipart 多文件上传）────────────────────
            def _recognize(self):
                ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype or "boundary=" not in ctype:
                    return self._err(400, tr("err.needMultipart"))
                boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    return self._err(400, tr("err.noContentLength"))
                if n <= 0:
                    return self._err(400, tr("err.emptyBody"))

                tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_web_"))
                # 解析、校验、识别任何一步退出都要清掉临时目录
                try:
                    fields, files = _parse_multipart_stream(
                        self.rfile, boundary, tmp_dir, n)
                    uploads = files.get("files") or files.get("file") or []
                    if not uploads:
                        return self._err(400, tr("err.noFilesPart"))
                    for _fname, p in uploads:
                        if not p.is_file() or p.stat().st_size == 0:
                            return self._err(400, tr("err.emptyUpload"))

                    # 保持前端给出的页序
                    paths = [str(p) for _fn, p in uploads]
                    primary_name = Path(uploads[0][0] or "result").stem or "result"
                    # 多文件用公共前缀（同名不同扩展）或「前缀 等 N 个文件」
                    if len(uploads) > 1:
                        stems = {Path(fn or "").stem for fn, _ in uploads if fn}
                        primary_name = (sorted(stems)[0] if stems else "result") + \
                                       (f" 等{len(uploads)}个" if len(stems) > 1 else "")
                    opts = {
                        "paths": paths,
                        # 原始文件名（与 paths 一一对应）：批量模式的条目名/产物命名用
                        "names": [fn or "" for fn, _p in uploads],
                        "name": primary_name,
                        "prompt": fields.get("prompt", ""),
                        "mode": (fields.get("mode") or "").strip(),
                    }
                    try:
                        result = server.backend.recognize(
                            opts, progress_cb=lambda pct, status:
                            server.hub.publish("progress", {"pct": pct, "status": status}))
                        return self._json(result)
                    except Exception as e:
                        from applog import log_error
                        log_error(f"识别失败：{primary_name}", exc=e)
                        head = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
                        return self._err(500, head)
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

        self._httpd = ThreadingHTTPServer((self.host, self._want_port), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


if __name__ == "__main__":
    # 独立启动（除错用）：起 server，打印网址，不自动开窗口。
    srv = WebViewServer(port=8770)
    srv.start()
    print(f"WebView server: {srv.url}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
