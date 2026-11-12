# -*- coding: utf-8 -*-
"""tests/test_i18n.py — 后端多语言层

重点：语言回退链不能把界面变成一屏 key；缺参时保留 {name} 便于发现漏传；
语言包键集必须对齐（新增语言=丢一份 JSON，不能靠人肉对键）。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import i18n  # noqa: E402


class I18nTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_lang = i18n.get_lang()

    @classmethod
    def tearDownClass(cls):
        i18n.set_lang(cls._orig_lang)

    def setUp(self):
        i18n.set_lang("zh")

    def test_set_and_get_lang(self):
        self.assertEqual(i18n.set_lang("en"), "en")
        self.assertEqual(i18n.get_lang(), "en")
        self.assertEqual(i18n.set_lang("zh"), "zh")

    def test_unknown_lang_falls_back(self):
        self.assertEqual(i18n.set_lang("tlh"), i18n.FALLBACK_LANG)

    def test_auto_resolves_to_known(self):
        self.assertIn(i18n.set_lang("auto"), i18n.known_langs())

    def test_tr_returns_localized_text(self):
        self.assertEqual(i18n.tr("app.name"), "OCR 小工具")

    def test_tr_missing_key_returns_key(self):
        self.assertEqual(i18n.tr("no.such.key"), "no.such.key")

    def test_tr_with_params(self):
        msg = i18n.tr("eng.loadingWeights", device="cuda")
        self.assertIn("cuda", msg)
        self.assertNotIn("{device}", msg)

    def test_tr_missing_param_keeps_placeholder(self):
        msg = i18n.tr("eng.loadingWeights")
        self.assertIn("{device}", msg, "缺参时应保留占位符而不是抛 KeyError")

    def test_unexpected_param_ignored(self):
        msg = i18n.tr("eng.loadingWeights", nosuch="x")
        self.assertIn("{device}", msg)

    def test_known_langs_contains_builtin(self):
        self.assertTrue({"zh", "en"} <= i18n.known_langs())

    def test_available_languages_shape(self):
        langs = i18n.available_languages()
        self.assertTrue(langs)
        self.assertEqual(langs[0]["code"], i18n.FALLBACK_LANG,
                         "默认语言应排最前")
        for item in langs:
            self.assertTrue(item["code"])
            self.assertTrue(item["name"])


class LocaleFilesTest(unittest.TestCase):
    """语言包自身的一致性（与 tools/check.py 同源的断言，防止单点失效）。"""

    def _load(self, code: str) -> dict:
        p = ROOT / "webview" / "locales" / f"{code}.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_key_sets_align_with_zh(self):
        base = set(self._load("zh"))
        for p in sorted((ROOT / "webview" / "locales").glob("*.json")):
            keys = set(json.loads(p.read_text(encoding="utf-8")))
            self.assertEqual(keys, base, f"{p.name} 与 zh.json 键集不一致")

    def test_every_language_has_native_name(self):
        for p in sorted((ROOT / "webview" / "locales").glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertTrue(data.get("meta.nativeName"),
                            f"{p.name} 缺 meta.nativeName")

    def test_no_empty_translations(self):
        for p in sorted((ROOT / "webview" / "locales").glob("*.json")):
            for k, v in json.loads(p.read_text(encoding="utf-8")).items():
                self.assertTrue(str(v).strip(), f"{p.name} 的 {k} 是空串")


if __name__ == "__main__":
    unittest.main()
