"""settings_store.py — settings.json 的统一读写层

旧实现里 settings.json 的「读 → 改 → 写」散落在 webview_backend 各处
（_persist_backend / set_model / set_settings / _persist_setting 各写一遍），
无锁也无原子性：
  • 两处并发写会互相覆盖（读改写序列丢更新）；
  • 进程中途被杀可能留下截断的 JSON，且无备份可恢复。
SettingsStore 收敛为单点：线程锁 + mtime 失效的内存缓存 + 临时文件
os.replace 原子替换（替换本身原子，进程任何时刻被杀都不会留半截文件）。

抗损坏（v1.2.x 加固）：
  • 解析失败：把坏文件改名 `settings.json.corrupt-<时间戳>` 留证（只留最近
    2 份）再返回 {}——原路径随即不存在，下次写入会正常重建，不再是「静默丢弃」。
  • 覆盖已有文件前先复制一份 `settings.json.bak`（只留 1 份）。
  • 落盘时补 `settingsVersion`（当前 1，纯增量键）。首次创建的新文件不写，
    保持既有文件内容口径不变；settingsVersion 是**落盘元数据**，read() 返回
    业务视图时会被剔除，调用方（webview_backend 等）语义完全不变。

cli_mode 的临时设置副本机制不受影响：它先改 core.SETTINGS_FILE 再构造
WebBackend，Store 在构造时取路径，读写自然落在副本上。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import threading
from pathlib import Path

SCHEMA_VERSION = 1
_VERSION_KEY = "settingsVersion"
_CORRUPT_KEEP = 2


class SettingsStore:
    """单文件 JSON 设置的线程安全读写。"""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._cache: dict | None = None
        self._sig: tuple | None = None     # (mtime_ns, size)：外部修改检测信号
        self._existed = False              # 见过已存在文件 → 后续写入补 settingsVersion

    # ── 读 ──────────────────────────────────────────────────────
    def read(self) -> dict:
        """读业务设置（内存缓存；文件被外部修改时按 mtime+size 重新加载）。

        返回视图不含 settingsVersion（落盘元数据），与加版本号之前完全一致。
        """
        with self._lock:
            raw = self._read_raw()
            return {k: v for k, v in raw.items() if k != _VERSION_KEY}

    def read_raw(self) -> dict:
        """读完整落盘内容（含 settingsVersion；迁移/诊断用）。"""
        with self._lock:
            return dict(self._read_raw())

    def schema_version(self) -> int:
        """落盘的设置版本号；无版本键/损坏时回 0。"""
        try:
            return int(self._read_raw().get(_VERSION_KEY) or 0)
        except (TypeError, ValueError):
            return 0

    def _read_raw(self) -> dict:
        """读原始字典（含元数据键）；损坏 → 隔离现场并返回 {}（不缓存）。"""
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._cache, self._sig, self._existed = {}, None, False
            return {}
        # 仅 mtime 不够：Windows 同一计时 tick 内的两次写 mtime 相同，
        # 会漏检「外部改坏 + 大小恰好不变」之外的全部变更；补上 size。
        if self._cache is not None and sig == self._sig:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            # 损坏/半截文件：留证后再返回空，不缓存 —— 外部修复后下次能读到
            self._quarantine_corrupt()
            self._cache, self._sig = None, None
            return {}
        self._cache = data
        self._sig = sig
        self._existed = True
        return data

    # ── 损坏隔离 ────────────────────────────────────────────────
    def _quarantine_corrupt(self) -> None:
        """把无法解析的设置文件改名留证；失败静默（读路径绝不抛异常）。"""
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            dst = self.path.with_name(f"{self.path.name}.corrupt-{ts}")
            os.replace(self.path, dst)
        except OSError:
            return
        try:
            olds = sorted(self.path.parent.glob(f"{self.path.name}.corrupt-*"))
            for p in olds[: max(0, len(olds) - _CORRUPT_KEEP)]:
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # ── 写 ──────────────────────────────────────────────────────
    def update(self, patch: dict) -> dict:
        """合并写入（浅合并）并返回更新后的完整设置（业务视图）。"""
        with self._lock:
            cur = dict(self.read())
            cur.update(patch or {})
            self._write(cur)
            return cur

    def remove(self, *keys) -> dict:
        """删除键并返回更新后的完整设置（业务视图；不存在的键忽略）。"""
        with self._lock:
            cur = dict(self.read())
            changed = False
            for k in keys:
                if k in cur:
                    cur.pop(k)
                    changed = True
            if changed:
                self._write(cur)
            return cur

    def _write(self, cur: dict):
        """原子落盘 + 刷新缓存（调用方须已持锁）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # 覆盖前留一份上一版（单份，best-effort）
            try:
                shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
            except OSError:
                pass
            self._existed = True
        data = dict(cur)
        if self._existed:
            data[_VERSION_KEY] = SCHEMA_VERSION
        tmp = self.path.with_name(self.path.name + ".tmp")
        # newline="\n"：Windows 上 write_text 默认把 \n 翻成 \r\n，会让
        # settings.json 在 LF 仓库里恒显示为"被改写"（真机跑一次即复现）。
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, self.path)
        try:
            st = self.path.stat()
            self._sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._sig = None
        self._cache = data          # 缓存含元数据键，read() 再过滤
        self._existed = True
